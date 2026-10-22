#!/usr/bin/env python3
"""Generate the Grafana dashboard of the mine (dashboard.json) from mine.json.

Run after changing sensors or workings: python team/vent/grafana/make_dashboard.py
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "..", "mine.json"), encoding="utf-8"))
NS = CFG["mine"]["namespace"]
DS = {"type": "influxdb", "uid": "opentwins-influxdb"}

_id = 0


def nid():
    global _id
    _id += 1
    return _id


def flux(things, feature, rename=True):
    """Time series of one feature for several twins, one series per twin name."""
    regex = "|".join(t.replace(".", "\\\\.") for t in things)
    q = (f'from(bucket: "default")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
         f'  |> filter(fn: (r) => r._measurement == "mqtt_consumer" and r._field == "value_{feature}_properties_value")\n'
         f'  |> filter(fn: (r) => r.thingId =~ /^{NS}:({regex})$/)\n'
         f'  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)\n')
    if rename:
        q += '  |> map(fn: (r) => ({r with _field: r.thingId}))\n  |> keep(columns: ["_time", "_value", "_field"])'
    return q


def names_override(ids_names):
    return [{"matcher": {"id": "byName", "options": f"{NS}:{i}"},
             "properties": [{"id": "displayName", "value": n}]} for i, n in ids_names]


def stat(title, thing, feature, unit, x, y, w=4, h=4, thresholds=None, decimals=None, mappings=None):
    steps = thresholds or [{"color": "#3fb27f", "value": None}]
    return {
        "id": nid(), "type": "stat", "title": title, "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals, "mappings": mappings or [],
                                     "thresholds": {"mode": "absolute", "steps": steps},
                                     "color": {"mode": "thresholds"}}, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "background" if thresholds else "value", "graphMode": "area",
                    "textMode": "value", "justifyMode": "center"},
        "targets": [{"refId": "A", "datasource": DS, "query": flux([thing], feature, rename=False)
                     + '  |> keep(columns: ["_time", "_value"])'}],
    }


def text_stat(title, thing, feature, x, y, w, h):
    q = (f'from(bucket: "default")\n  |> range(start: -1h)\n'
         f'  |> filter(fn: (r) => r.thingId == "{NS}:{thing}" and r._field == "value_{feature}_properties_value")\n'
         f'  |> last()\n  |> keep(columns: ["_value"])')
    return {
        "id": nid(), "type": "stat", "title": title, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"color": {"mode": "fixed", "fixedColor": "#4aa3df"}}, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "/.*/", "values": False},
                    "colorMode": "value", "graphMode": "none", "textMode": "value", "wideLayout": True,
                    "text": {"valueSize": 18}},
        "targets": [{"refId": "A", "datasource": DS, "query": q}],
    }


def series(title, things, feature, unit, x, y, w=12, h=8, limits=(), names=None, stack=False, decimals=None):
    steps = [{"color": "transparent", "value": None}] + [{"color": c, "value": v} for v, c in limits]
    return {
        "id": nid(), "type": "timeseries", "title": title, "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {
            "defaults": {"unit": unit, "decimals": decimals,
                         "custom": {"lineWidth": 2, "fillOpacity": 10 if stack else 0, "showPoints": "never",
                                    "thresholdsStyle": {"mode": "dashed" if limits else "off"},
                                    "stacking": {"mode": "normal" if stack else "none"}},
                         "thresholds": {"mode": "absolute", "steps": steps}},
            "overrides": names_override(names or []),
        },
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        "targets": [{"refId": "A", "datasource": DS, "query": flux(things, feature)}],
    }


def build():
    S = {s["id"]: s for s in CFG["sensors"]}
    B = {b["id"]: b for b in CFG["branches"]}
    panels = []
    y = 0
    red = [{"color": "#3fb27f", "value": None}]
    panels.append(stat("Воздух в шахту", "mine", "air_total", "none", 0, y, decimals=0,
                       thresholds=[{"color": "#e5484d", "value": None}, {"color": "#e8b33a", "value": 4000},
                                   {"color": "#3fb27f", "value": 7200}]))
    panels[-1]["fieldConfig"]["defaults"]["unit"] = "suffix: м³/мин"
    panels.append(stat("CH₄ исходящая шахты (≤0,75%)", "mine", "ch4_return", "percent", 4, y, decimals=2,
                       thresholds=red + [{"color": "#e8b33a", "value": 0.6}, {"color": "#e5484d", "value": 0.75}]))
    panels.append(stat("CH₄ максимум в выработках", "mine", "max_ch4", "percent", 8, y, decimals=2,
                       thresholds=red + [{"color": "#e8b33a", "value": 1}, {"color": "#e5484d", "value": 2},
                                         {"color": "#b04fe0", "value": 5}]))
    panels.append(stat("Людей под землёй", "mine", "people_underground", "none", 12, y, decimals=0))
    panels.append(stat("Людей в опасности", "mine", "people_at_risk", "none", 16, y, decimals=0,
                       thresholds=red + [{"color": "#e5484d", "value": 1}]))
    panels.append(stat("Уровень тревоги", "advisor", "alert_level", "none", 20, y, decimals=0,
                       thresholds=red + [{"color": "#e8b33a", "value": 1}, {"color": "#e5484d", "value": 2}],
                       mappings=[{"type": "value", "options": {"0": {"text": "Норма"}, "1": {"text": "Внимание"},
                                                               "2": {"text": "Авария"}}}]))
    y += 4
    panels.append(text_stat("Рекомендация советника (прогноз на 60 мин)", "advisor", "recommendation", 0, y, 12, 3))
    panels.append(text_stat("Главная тревога", "advisor", "top_alert", 12, y, 12, 3))
    y += 3
    panels.append({
        "id": nid(), "type": "text", "title": "Пульт диспетчера: разрез шахты, сценарии, советник",
        "gridPos": {"x": 0, "y": y, "w": 24, "h": 30},
        "options": {"mode": "html", "content":
                    '<iframe src="${console_url}" style="width:100%;height:100%;border:0;min-height:860px"></iframe>'},
    })
    y += 30
    ch4 = [s for s in S if S[s]["type"] == "CH4"]
    co = [s for s in S if S[s]["type"] == "CO"]
    panels.append(series("Метан на датчиках, %", ch4, "reading", "percent", 0, y, limits=[(1.0, "#e8b33a"), (2.0, "#e5484d")],
                         names=[(s, S[s]["name"].replace("Метан, ", "")) for s in ch4], decimals=2))
    panels.append(series("CO на датчиках, ppm", co, "reading", "ppm", 12, y, limits=[(17, "#e8b33a"), (50, "#e5484d")],
                         names=[(s, S[s]["name"].replace("CO, ", "")) for s in co], decimals=0))
    y += 8
    work = [b for b in B if B[b]["kind"] != "shaft"]
    panels.append(series("Расход воздуха по выработкам, м³/с (знак = направление)", work, "airflow", "none", 0, y,
                         names=[(b, B[b]["name"]) for b in work], decimals=1))
    panels.append(series("Вентиляторы: подача, м³/с", ["fan-1", "fan-2"], "airflow", "none", 12, y, w=6,
                         names=[(f["id"], f["name"]) for f in CFG["fan_station"]["units"]]))
    panels.append(series("Вентиляторы: давление, Па", ["fan-1", "fan-2"], "pressure", "pressurepa", 18, y, w=6,
                         names=[(f["id"], f["name"]) for f in CFG["fan_station"]["units"]]))
    y += 8
    crews = [c["id"] for c in CFG["brigades"]]
    panels.append(series("Доза CO у бригад (FED, 1 = потеря способности к самоспасению)", crews, "fed_co", "none",
                         0, y, w=8, limits=[(0.3, "#e8b33a"), (1.0, "#e5484d")],
                         names=[(c["id"], c["name"]) for c in CFG["brigades"]], decimals=2))
    panels.append(series("Расстояние бригад до выхода, м", crews, "distance_to_exit", "lengthm", 8, y, w=8,
                         names=[(c["id"], c["name"]) for c in CFG["brigades"]]))
    dists = list(CFG["districts"])
    panels.append(series("Электроэнергия участков (1 = есть, 0 = отключена)", dists, "power", "none", 16, y, w=8,
                         names=[(d, v["name"]) for d, v in CFG["districts"].items()]))
    return {
        "uid": "arqa-vent",
        "title": "Шахта «Арқа-1»: вентиляция и газ",
        "tags": ["openegiz", "mine", "ventilation", "methane"],
        "timezone": "browser", "schemaVersion": 39, "version": 1, "refresh": "5s",
        "time": {"from": "now-30m", "to": "now"},
        "description": "Цифровой двойник вентиляции и газовой безопасности угольной шахты. Данные: team/vent/simulator.py "
                       "→ MQTT → Ditto → Telegraf → InfluxDB.",
        "templating": {"list": [{
            "name": "console_url", "label": "Адрес пульта", "type": "textbox",
            "query": "http://localhost:8095/", "current": {"text": "http://localhost:8095/", "value": "http://localhost:8095/"},
        }]},
        "panels": panels,
    }


if __name__ == "__main__":
    out = os.path.join(HERE, "dashboard.json")
    json.dump(build(), open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("written", out)
