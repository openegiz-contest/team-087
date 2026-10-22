#!/usr/bin/env python3
"""Create or update the mine's twins in Ditto from mine.json (idempotent PUTs).

Hierarchy (attributes._parents, used by the OpenEgiz app and by Telegraf's
`parent` tag):

    mine
    ├── advisor, dispatch
    ├── fan-station ── fan-1, fan-2
    ├── d-main, d-l1, d-l2 (districts) ── workings ── sensors
    └── crews
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "mine.json"), encoding="utf-8"))
NS = CFG["mine"]["namespace"]
DITTO_URL = os.environ.get("DITTO_URL", "http://localhost:8080").rstrip("/")
DITTO_USER = os.environ.get("DITTO_USER", "ditto")
DITTO_PASSWORD = os.environ["DITTO_PASSWORD"]
AUTH = "Basic " + base64.b64encode(f"{DITTO_USER}:{DITTO_PASSWORD}".encode()).decode()


def tid(name):
    return f"{NS}:{name}"


def thing(name, kind, parent=None, features=(), **attrs):
    a = {"name": name, "_isType": False, "kind": kind}
    if parent:
        a["_parents"] = tid(parent)
    a.update(attrs)
    return {"policyId": "default:basic_policy", "attributes": a,
            "features": {f: {"properties": {"value": 0}} for f in features}}


def build():
    twins = {}
    m = CFG["mine"]
    twins["mine"] = thing(m["name"], "mine", description=m["description"], sources=m["sources"])
    twins["advisor"] = thing("Советник диспетчера", "advisor", "mine",
                             rules=CFG["limits"]["source"])
    twins["dispatch"] = thing("Пульт диспетчера (команды)", "dispatch", "mine",
                              usage="Команда в feature command, properties.value: fan_trip, fan_start:fan-2, "
                                    "fan_reverse, fan_normal, fire:fire-main, methane_rise:d-l1, co_rise, "
                                    "power_off:d-l1, evacuate:all, extinguish, reset")
    twins["dispatch"]["features"] = {"command": {"properties": {"value": "", "id": "init"}}}
    fs = CFG["fan_station"]
    twins["fan-station"] = thing("Главная вентиляторная установка", "fan-station", "mine",
                                 shaft=fs["branch"], reverse_factor=fs["reverse_factor"], source=fs["source"])
    for u in fs["units"]:
        twins[u["id"]] = thing(u["name"], "fan", "fan-station", model=u["model"], h0_pa=u["h0_pa"],
                               power_rated_kw=u["power_rated_kw"], efficiency=u["efficiency"], role=u["role"])
    for d, v in CFG["districts"].items():
        twins[d] = thing(v["name"], "district", "mine")
    for b in CFG["branches"]:
        twins[b["id"]] = thing(b["name"], f"working/{b['kind']}", b["district"], length_m=b["length"],
                               area_m2=b["area"], resistance_ns2m8=b["R"], from_node=b["from"], to_node=b["to"])
    for s in CFG["sensors"]:
        twins[s["id"]] = thing(s["name"], f"sensor/{s['type']}", s["branch"], position=s["pos"])
    for c in CFG["brigades"]:
        twins[c["id"]] = thing(c["name"], "crew", "mine", people=c["people"], home=c["branch"])
    return twins


def put(thing_id, body):
    req = urllib.request.Request(f"{DITTO_URL}/api/2/things/{thing_id}", data=json.dumps(body).encode(),
                                 method="PUT", headers={"Authorization": AUTH, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def main():
    twins = build()
    for name, body in twins.items():  # parents come first in build order
        for attempt in range(20):
            try:
                status = put(tid(name), body)
                print(f"{'created' if status == 201 else 'updated'}  {tid(name)}")
                break
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == 19:
                    print(f"FAILED   {tid(name)}: {exc}", file=sys.stderr)
                    return 1
                time.sleep(3)
    print(f"{len(twins)} twins ready in {DITTO_URL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
