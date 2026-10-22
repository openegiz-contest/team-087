#!/usr/bin/env python3
"""
Digital twin service of the mine's ventilation and gas safety.

- Runs the model (model.py) faster than real time (SIM_SPEEDUP).
- Publishes every twin to MQTT as Ditto protocol "modify" commands
  (telemetry/<twin>), like a field gateway: Ditto -> Telegraf -> InfluxDB ->
  Grafana carry it on.
- Takes commands from the dispatcher's console through the twin
  <namespace>:dispatch in Ditto: the console writes the command into the twin,
  Ditto publishes the change on opentwins/..., this service applies it. The
  same command can be typed into that twin in the OpenEgiz app in Grafana.
- Serves the dispatcher's console (ui/index.html) and its JSON API on
  HTTP_PORT, and runs the advisor's forecast in the background.
"""

import base64
import csv
import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paho.mqtt.client as mqtt

import advisor
from model import Model

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "mine.json"), encoding="utf-8"))
NS = CFG["mine"]["namespace"]

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
DITTO_URL = os.environ.get("DITTO_URL", "http://localhost:8080").rstrip("/")
DITTO_USER = os.environ.get("DITTO_USER", "ditto")
DITTO_PASSWORD = os.environ.get("DITTO_PASSWORD", "")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8095"))
SPEEDUP = float(os.environ.get("SIM_SPEEDUP", "10"))
PUBLISH_S = float(os.environ.get("PUBLISH_INTERVAL", "3"))
FORECAST_S = float(os.environ.get("FORECAST_INTERVAL", "15"))
DT = 5.0

log = logging.getLogger("vent")


def load_profile():
    """Methane emission factor from a real week of a longwall return sensor."""
    path = os.path.join(HERE, "data", "methane_week_2014-03-24.csv")
    try:
        rows = list(csv.DictReader(open(path, encoding="utf-8")))
    except OSError:
        log.warning("no methane profile at %s, using synthetic variation", path)
        return []
    vals = [float(r["MM256"]) for r in rows]
    mean = sum(vals) / len(vals)
    return [(i * 60.0, min(3.0, max(0.4, v / mean))) for i, v in enumerate(vals)]


PROFILE = load_profile()


class Twin:
    """Owns the model and everything around it."""

    def __init__(self):
        self.lock = threading.RLock()
        self.model = Model(CFG, profile=PROFILE)
        self.speed = SPEEDUP
        self.paused = False
        self.forecast = None
        self.forecast_due = 0.0
        self.seen = set()
        self.ditto_ok = None
        self.last_publish = 0.0
        self.commands = []  # recent commands with their path through the platform
        self.history = []  # sensor readings every 15 s of model time, for the console's charts
        self.next_hist = 0.0

    # --------------------------------------------------------- commands
    def handle(self, cmd, via):
        cid = cmd.get("id") or str(uuid.uuid4())
        with self.lock:
            if cid in self.seen:
                return None
            self.seen.add(cid)
            if cmd.get("cmd") == "reset":
                self.model = Model(CFG, profile=PROFILE)
                self.forecast = None
                self.history = []
                self.next_hist = 0.0
                result = "модель сброшена в нормальный режим"
            else:
                result = self.model.apply(cmd, source="диспетчер" if via != "ditto-app" else "OpenEgiz app")
            self.forecast_due = 0.0
            self.commands.append({"t": round(self.model.t), "cmd": cmd.get("cmd"), "arg": cmd.get("arg"),
                                  "via": via, "result": result})
            self.commands = self.commands[-30:]
        log.info("command %s via %s: %s", cmd, via, result)
        return result

    # ----------------------------------------------------------- views
    def twins(self):
        """Features of every twin, as published to Ditto."""
        m = self.model
        L = m.limits
        out = {}
        s = m.summary()
        alerts = advisor.alerts(m)
        level = max((advisor.LEVELS[a["level"]] for a in alerts), default=0)
        fc = self.forecast or {}
        out["mine"] = {
            "sim_time": {"value": round(m.t / 60, 1), "unit": "min"},
            "scenario": {"value": m.scenario},
            "alert_level": {"value": level},
            "air_total": {"value": round(s["mine_air"] * 60), "unit": "m3/min"},
            "ch4_return": {"value": round(m.mine_return_ch4(), 3), "unit": "%"},
            "max_ch4": {"value": round(s["max_ch4"], 2), "unit": "%"},
            "max_co": {"value": round(s["max_co"], 1), "unit": "ppm"},
            "people_underground": {"value": s["people_underground"], "unit": "чел."},
            "people_at_risk": {"value": s["people_at_risk"], "unit": "чел."},
            "people_incapacitated": {"value": s["people_incapacitated"], "unit": "чел."},
        }
        out["advisor"] = {
            "alert_level": {"value": level},
            "alerts_count": {"value": len([a for a in alerts if a["level"] != "info"])},
            "top_alert": {"value": alerts[0]["text"] if alerts else "Норма"},
            "recommendation": {"value": fc.get("best_name", "—")},
            "explanation": {"value": fc.get("explain", "")},
        }
        qf = m.br(m.fan_branch)["Q"]
        h, _ = m._fan_head(qf)
        out["fan-station"] = {
            "mode": {"value": m.fan_mode},
            "airflow": {"value": round(abs(qf), 1), "unit": "m3/s"},
            "depression": {"value": round(abs(h)), "unit": "Pa"},
            "reverse_share": {"value": round(abs(qf) / m.q_normal * 100) if m.q_normal else 0, "unit": "%"},
        }
        for f in m.fans:
            out[f.id] = {
                "state": {"value": f.state},
                "running": {"value": 1 if f.speed > 0.5 else 0},
                "speed": {"value": round(f.speed * 100), "unit": "%"},
                "airflow": {"value": round(f.flow, 1), "unit": "m3/s"},
                "pressure": {"value": round(f.pressure), "unit": "Pa"},
                "power": {"value": round(f.flow * f.pressure / f.eff / 1000), "unit": "kW"},
            }
        for d, dv in m.districts.items():
            st = m.district_stats(d)
            out[d] = {
                "power": {"value": 1 if dv["power"] else 0},
                "power_reason": {"value": dv["power_reason"] or "—"},
                "max_ch4": {"value": round(st["max_ch4"], 2), "unit": "%"},
                "max_co": {"value": round(st["max_co"], 1), "unit": "ppm"},
                "min_visibility": {"value": round(st["min_vis"], 1), "unit": "m"},
                "people": {"value": st["people"], "unit": "чел."},
            }
        for b in m.branches:
            out[b["id"]] = {
                "airflow": {"value": round(b["Q"], 2), "unit": "m3/s"},
                "velocity": {"value": round(b["Q"] / b["area"], 2), "unit": "m/s"},
                "ch4": {"value": round(max(b["ch4"]), 3), "unit": "%"},
                "co": {"value": round(max(b["co"]), 1), "unit": "ppm"},
                "visibility": {"value": round(m.visibility(max(b["smoke"])), 1), "unit": "m"},
                "temp_rise": {"value": round(max(b["heat"]), 1), "unit": "K"},
                "regulator_opening": {"value": round(b["opening"] * 100), "unit": "%"},
            }
        for sn in m.sensors:
            v = m.sensor_value(sn)
            if sn["type"] == "CH4":
                lim = L["ch4_face_pct"] if m.br(sn["branch"])["kind"] == "longwall" else (
                    L["ch4_mine_return_pct"] if sn["id"] == "s-ch4-mine" else L["ch4_district_return_pct"])
                alarm = 2 if v >= lim else 1 if v >= 0.8 * lim else 0
                unit = "%"
            elif sn["type"] == "CO":
                alarm = 2 if v >= L["co_alarm_ppm"] else 1 if v >= L["co_mac_ppm"] else 0
                unit = "ppm"
            else:
                alarm = 2 if v < L["air_velocity_min_face_ms"] else 1 if v > L["air_velocity_max_face_ms"] else 0
                unit = "m/s"
            out[sn["id"]] = {"reading": {"value": round(v, 3), "unit": unit}, "alarm": {"value": alarm}}
        for c in m.crews:
            out[c.id] = {
                "status": {"value": c.status},
                "people": {"value": c.people, "unit": "чел."},
                "location": {"value": m.br(c.branch)["name"]},
                "ch4_here": {"value": round(c.ch4_here, 2), "unit": "%"},
                "co_here": {"value": round(c.co_here, 1), "unit": "ppm"},
                "fed_co": {"value": round(c.fed, 3)},
                "self_rescuer_left": {"value": round(c.rescuer_left_s / 60, 1), "unit": "min"},
                "distance_to_exit": {"value": round(c.dist_to_exit), "unit": "m"},
            }
        return out

    def state(self):
        with self.lock:
            m = self.model
            branches = [{
                "id": b["id"], "Q": round(b["Q"], 2), "v": round(b["Q"] / b["area"], 2),
                "ch4": [round(x, 3) for x in b["ch4"]], "co": [round(x, 1) for x in b["co"]],
                "vis": [round(m.visibility(x), 1) for x in b["smoke"]],
                "heat": [round(x, 1) for x in b["heat"]], "opening": b["opening"],
            } for b in m.branches]
            crews = [{
                "id": c.id, "name": c.name, "people": c.people, "status": c.status, "branch": c.branch,
                "s": round(c.s, 1), "fed": round(c.fed, 3), "rescuer_on": c.rescuer_on,
                "rescuer_left_min": round(c.rescuer_left_s / 60, 1), "co": round(c.co_here, 1),
                "ch4": round(c.ch4_here, 2), "vis": round(c.vis_here, 1), "exit": c.target_exit,
                "dist": round(c.dist_to_exit), "location": m.br(c.branch)["name"],
            } for c in m.crews]
            qf = m.br(m.fan_branch)["Q"]
            h, _ = m._fan_head(qf)
            return {
                "t": m.t, "speed": self.speed, "paused": self.paused, "scenario": m.scenario,
                "fan_mode": m.fan_mode, "mode_target": m.mode_target,
                "fan_station": {"Q": round(qf, 1), "h": round(h), "share": round(abs(qf) / m.q_normal * 100)},
                "fans": [{"id": f.id, "name": f.name, "model": f.model, "state": f.state, "speed": round(f.speed, 2),
                          "failed": f.failed, "flow": round(f.flow, 1), "pressure": round(f.pressure),
                          "power_kw": round(f.flow * f.pressure / f.eff / 1000)} for f in m.fans],
                "districts": {d: {**v, **{k: round(x, 2) for k, x in m.district_stats(d).items()}}
                              for d, v in m.districts.items()},
                "branches": branches,
                "sensors": [{"id": s["id"], "value": round(m.sensor_value(s), 3)} for s in m.sensors],
                "crews": crews,
                "fire": {"site": m.fire.site_id, "name": m.fire.name, "hrr_mw": round(m.fire.hrr / 1e6, 2),
                         "out": m.fire.out, "suppress": m.fire.suppress} if m.fire else None,
                "summary": {k: round(v, 2) for k, v in m.summary().items()},
                "alerts": advisor.alerts(m),
                "events": m.events[-40:],
                "forecast": self.forecast,
                "commands": self.commands[-10:],
                "ditto_ok": self.ditto_ok,
                "last_publish": self.last_publish,
            }


TWIN = Twin()


# ----------------------------------------------------------------- Ditto
def ditto_request(method, path, body=None):
    auth = "Basic " + base64.b64encode(f"{DITTO_USER}:{DITTO_PASSWORD}".encode()).decode()
    req = urllib.request.Request(f"{DITTO_URL}/api/2{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": auth, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = r.read()
        return json.loads(data) if data else None


def send_command(cmd):
    """Console -> Ditto twin <ns>:dispatch -> MQTT event -> this service."""
    cmd = dict(cmd)
    cmd["id"] = str(uuid.uuid4())
    text = cmd["cmd"] + (f":{cmd['arg']}" if cmd.get("arg") else "")
    props = {"value": text, "id": cmd["id"], "params": json.dumps(cmd, ensure_ascii=False),
             "issued_by": "пульт диспетчера", "issued_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        ditto_request("PUT", f"/things/{NS}:dispatch/features/command/properties", props)
        TWIN.ditto_ok = True
    except (urllib.error.URLError, OSError) as exc:
        TWIN.ditto_ok = False
        log.warning("Ditto unreachable (%s): applying the command directly", exc)
        return TWIN.handle(cmd, "direct")
    # The command comes back through MQTT; wait briefly so the reply is useful.
    for _ in range(30):
        if cmd["id"] in TWIN.seen:
            return next((c["result"] for c in reversed(TWIN.commands) if c["cmd"] == cmd["cmd"]), "ok")
        time.sleep(0.1)
    log.warning("no event from Ditto for %s in 3 s: applying directly", cmd["id"])
    return TWIN.handle(cmd, "direct")


def parse_dispatch_event(payload):
    """Pull the command out of a Ditto event on the dispatch twin."""
    path = payload.get("path", "")
    value = payload.get("value")
    props = None
    if path.endswith("/features/command/properties"):
        props = value
    elif path.endswith("/features/command/properties/value"):
        props = {"value": value}
    elif path.endswith("/features/command"):
        props = (value or {}).get("properties")
    elif path.endswith("/features"):
        props = (value or {}).get("command", {}).get("properties")
    elif path in ("/", ""):
        props = ((value or {}).get("features") or {}).get("command", {}).get("properties")
    if not isinstance(props, dict) or not props.get("value"):
        return None
    if props.get("params"):
        try:
            cmd = json.loads(props["params"])
        except ValueError:
            cmd = {}
    else:
        name, _, arg = str(props["value"]).partition(":")
        cmd = {"cmd": name.strip(), "arg": arg.strip() or None}
    cmd["id"] = props.get("id") or f"rev-{payload.get('revision')}"
    return cmd, "ditto" if props.get("issued_by") == "пульт диспетчера" else "ditto-app"


# ------------------------------------------------------------------ MQTT
def start_mqtt():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"arqa-vent-{os.getpid()}")
    topic = f"opentwins/+/+/{NS}/dispatch"

    def on_connect(c, userdata, flags, rc, props=None):
        c.subscribe(topic, qos=1)
        log.info("MQTT connected, listening for dispatcher commands on %s", topic)

    def on_message(c, userdata, msg):
        try:
            payload = json.loads(msg.payload)
        except ValueError:
            return
        parsed = parse_dispatch_event(payload)
        if parsed:
            TWIN.handle(parsed[0], parsed[1])

    client.on_connect = on_connect
    client.on_message = on_message
    for _ in range(60):
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
            break
        except OSError as exc:
            log.warning("MQTT %s:%s not reachable (%s), retrying", MQTT_HOST, MQTT_PORT, exc)
            time.sleep(2)
    client.loop_start()
    return client


def publish_loop(client, stop):
    while not stop.is_set():
        with TWIN.lock:
            twins = TWIN.twins()
        for name, features in twins.items():
            msg = {"topic": f"{NS}/{name}/things/twin/commands/modify", "path": "/features",
                   "value": {f: {"properties": p} for f, p in features.items()}}
            client.publish(f"telemetry/{name}", json.dumps(msg, ensure_ascii=False), qos=0)
        TWIN.last_publish = time.time()
        stop.wait(PUBLISH_S)


def sim_loop(stop):
    last = time.time()
    debt = 0.0
    while not stop.is_set():
        now = time.time()
        if not TWIN.paused:
            debt += (now - last) * TWIN.speed
        last = now
        steps = 0
        while debt >= DT and steps < 40:
            with TWIN.lock:
                m = TWIN.model
                m.step(DT)
                if m.t >= TWIN.next_hist:
                    TWIN.history.append({"t": m.t, "s": {s["id"]: round(m.sensor_value(s), 3) for s in m.sensors}})
                    TWIN.history = TWIN.history[-720:]
                    TWIN.next_hist = m.t + 15.0
            debt -= DT
            steps += 1
        debt = min(debt, DT * 40)
        stop.wait(0.25)


def forecast_loop(stop):
    while not stop.is_set():
        if time.time() >= TWIN.forecast_due:
            with TWIN.lock:
                snapshot = TWIN.model.clone()
                snapshot.events = []
            try:
                fc = advisor.forecast(snapshot)
                with TWIN.lock:
                    TWIN.forecast = fc
            except Exception:  # never let a forecast bug stop the twin
                log.exception("forecast failed")
            TWIN.forecast_due = time.time() + FORECAST_S
        stop.wait(0.5)


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, open(os.path.join(HERE, "ui", "index.html"), "rb").read(), "text/html; charset=utf-8")
        elif path == "/api/layout":
            self._send(200, layout())
        elif path == "/api/state":
            self._send(200, TWIN.state())
        elif path == "/api/history":
            with TWIN.lock:
                self._send(200, TWIN.history)
        elif path == "/api/twins":
            try:
                res = ditto_request("GET", f"/search/things?filter=like(thingId,%22{NS}:*%22)&option=size(200)")
                self._send(200, res)
            except (urllib.error.URLError, OSError) as exc:
                self._send(502, {"error": str(exc)})
        elif path == "/healthz":
            self._send(200, {"ok": True, "t": TWIN.model.t})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        path = self.path.split("?")[0]
        if path == "/api/command":
            if not body.get("cmd"):
                return self._send(400, {"error": "cmd required"})
            self._send(200, {"result": send_command(body)})
        elif path == "/api/sim":
            if "speed" in body:
                TWIN.speed = max(0.0, min(120.0, float(body["speed"])))
            if "paused" in body:
                TWIN.paused = bool(body["paused"])
            self._send(200, {"speed": TWIN.speed, "paused": TWIN.paused})
        elif path == "/api/forecast":
            TWIN.forecast_due = 0.0
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})


def layout():
    return {
        "mine": CFG["mine"],
        "nodes": CFG["nodes"],
        "branches": [{k: b[k] for k in b if k not in ("R",)} for b in CFG["branches"]],
        "districts": CFG["districts"],
        "sensors": CFG["sensors"],
        "fire_sites": CFG["fire_sites"],
        "fans": CFG["fan_station"]["units"],
        "limits": CFG["limits"],
        "crews": CFG["brigades"],
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("profile: %d minutes of real methane data", len(PROFILE))
    stop = threading.Event()
    client = start_mqtt()
    for target in (lambda: sim_loop(stop), lambda: publish_loop(client, stop), lambda: forecast_loop(stop)):
        threading.Thread(target=target, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    log.info("dispatcher console on http://0.0.0.0:%d, speed x%.0f", HTTP_PORT, TWIN.speed)

    def on_signal(*_):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    server.serve_forever()
    client.loop_stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
