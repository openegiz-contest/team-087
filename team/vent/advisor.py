"""
Dispatcher's advisor: checks the twin against the coal mine safety rules of
Kazakhstan (Order No. 351 of 30.12.2014) and compares what happens in the next
30 minutes under alternative decisions, by running copies of the model ahead.

Answers the Concept's question: how to control the fans and when to withdraw
people.
"""

import time

LEVELS = {"ok": 0, "info": 0, "warn": 1, "crit": 2}


def alerts(m):
    """Rule checks on the current state: [{level, text, rule}]."""
    L = m.limits
    out = []
    running = [f for f in m.fans if f.speed > 0.5]
    if not running:
        stopped = (m.t - m.fan_stop_since) / 60 if m.fan_stop_since is not None else 0
        out.append({"level": "crit", "rule": "п.135, п.139",
                    "text": f"ГВУ не работает {stopped:.0f} мин: шахта без принудительного проветривания"})
    if m.fan_mode == "реверс":
        out.append({"level": "warn", "rule": "п.136-137",
                    "text": "Вентиляция реверсирована: свежий воздух идёт по вентиляционному стволу"})
    for s in m.sensors:
        v = m.sensor_value(s)
        b = m.br(s["branch"])
        if s["type"] == "CH4":
            lim = m.ch4_limit(s)
            what = {L["ch4_face_pct"]: "местное скопление", L["ch4_mine_return_pct"]: "исходящая шахты"}.get(
                lim, "исходящая участка")
            if v >= L["ch4_explosive_lower_pct"]:
                out.append({"level": "crit", "rule": "Прил. 4",
                            "text": f"{s['name']}: {v:.2f}% — взрывоопасная концентрация метана"})
            elif v >= lim:
                out.append({"level": "crit", "rule": "Прил. 4, п.150",
                            "text": f"{s['name']}: {v:.2f}% ≥ {lim}% ({what})"})
            elif v >= 0.8 * lim:
                out.append({"level": "warn", "rule": "Прил. 4",
                            "text": f"{s['name']}: {v:.2f}%, близко к пределу {lim}%"})
        elif s["type"] == "CO":
            if v >= L["co_alarm_ppm"]:
                out.append({"level": "crit", "rule": "Прил. 5",
                            "text": f"{s['name']}: CO {v:.0f} ppm — признак пожара"})
            elif v >= L["co_mac_ppm"]:
                out.append({"level": "warn", "rule": "Прил. 5",
                            "text": f"{s['name']}: CO {v:.0f} ppm > ПДК {L['co_mac_ppm']} ppm"})
        elif s["type"] == "AIR":
            if v > L["air_velocity_max_face_ms"]:
                out.append({"level": "warn", "rule": "Прил. 6",
                            "text": f"{s['name']}: {v:.2f} м/с > {L['air_velocity_max_face_ms']} м/с"})
            elif v < m.air_min(s):
                out.append({"level": "crit", "rule": "Прил. 6",
                            "text": f"{s['name']}: {v:.2f} м/с < {m.air_min(s)} м/с"})
    if m.local_fans and not any(f.speed > 0.5 for f in m.local_fans):
        out.append({"level": "crit", "rule": "п.143",
                    "text": "ВМП не работает: тупиковая выработка штрека 3-Ю без проветривания"})
    if m.degas["branches"] and m.degas_on < 0.5:
        out.append({"level": "warn", "rule": "дегазация",
                    "text": f"Дегазация не работает: в вентиляцию идёт ещё {sum(v['goaf_total_m3min'] * v['efficiency'] for v in m.degas['branches'].values()) * (1 - m.degas_on):.0f} м³/мин метана"})
    for b in m.branches:
        n = m.layering_number(b)
        if n is not None and n < L["layering_number_min"] and max(b["ch4"]) >= 0.5:
            out.append({"level": "warn", "rule": "расслоение",
                        "text": f"{b['name']}: число расслоения {n:.1f} < {L['layering_number_min']} — метан может скопиться слоем у кровли"})
    for c in m.crews:
        if c.underground and c.status == "работают" and (c.ch4_here >= L["ch4_face_pct"] or c.co_here >= L["co_mac_ppm"]):
            out.append({"level": "crit", "rule": "п.116, п.150",
                        "text": f"{c.name} ({c.people} чел.) работает в непригодном воздухе"})
    if m.fire and not m.fire.out and m.fire.hrr > 0:
        out.append({"level": "crit", "rule": "ПЛА",
                    "text": f"{m.fire.name}: {m.fire.hrr / 1e6:.1f} МВт"})
    out.sort(key=lambda a: -LEVELS[a["level"]])
    return out


# ---------------------------------------------------------------- forecast
def options(m):
    """Decisions worth comparing in the current situation."""
    opts = [{"id": "nothing", "name": "Ничего не менять", "cmds": []}]
    running = any(f.target > 0.5 and not f.failed for f in m.fans)
    working_fan = next((f for f in m.fans if not f.failed and f.target < 0.5), None)
    ev = {"cmd": "evacuate", "arg": "all"}
    if not running and working_fan:
        start = {"cmd": "fan_start", "arg": working_fan.id}
        opts.append({"id": "reserve", "name": f"Пустить {working_fan.name}", "cmds": [start]})
        opts.append({"id": "reserve_evac", "name": f"Пустить {working_fan.name} и вывести людей",
                     "cmds": [start, ev]})
    lf_running = any(f.target > 0.5 and not f.failed for f in m.local_fans)
    lf_ready = next((f for f in m.local_fans if not f.failed and f.target < 0.5), None)
    if m.local_fans and not lf_running:
        d = m.br(m.local_fans[0].branch)["district"]
        ev_d = {"cmd": "evacuate", "arg": d}
        if lf_ready:
            opts.append({"id": "vmp_reserve", "name": f"Пустить {lf_ready.name}", "cmds": [{"cmd": "vmp_start", "arg": lf_ready.id}]})
            opts.append({"id": "vmp_reserve_evac", "name": f"Пустить {lf_ready.name} и вывести проходчиков",
                         "cmds": [{"cmd": "vmp_start", "arg": lf_ready.id}, ev_d]})
        opts.append({"id": "vmp_evac", "name": "Вывести проходчиков из тупика", "cmds": [ev_d]})
    if m.degas["branches"] and m.degas_target < 0.5:
        opts.append({"id": "degas", "name": "Перезапустить дегазационную установку", "cmds": [{"cmd": "degas_start"}]})
        regs = [{"cmd": "regulator", "arg": b["id"], "opening": 1.0} for b in m.branches
                if b.get("regulator") and b["opening"] < 0.99 and b["id"] in m.degas["branches"]]
        if regs:
            opts.append({"id": "degas_reg", "name": "Перезапустить дегазацию и открыть регуляторы лав на время разгона",
                         "cmds": [{"cmd": "degas_start"}] + regs})
    fire = m.fire and not m.fire.out
    hot = [] if fire else [d for d in m.districts if d != "d-main"
                           and m.district_stats(d)["max_ch4"] >= 0.8 * m.limits["ch4_district_return_pct"]]
    for d in hot:
        opts.append({"id": f"cut_{d}", "name": f"Обесточить и вывести людей: {m.districts[d]['name']}",
                     "cmds": [{"cmd": "power_off", "arg": d}, {"cmd": "evacuate", "arg": d}]})
        reg = next((b for b in m.branches if b["district"] == d and b.get("regulator") and b["opening"] < 0.99), None)
        if reg and running:
            opts.append({"id": f"reg_{d}", "name": f"Открыть регулятор: {reg['name']} (больше воздуха)",
                         "cmds": [{"cmd": "regulator", "arg": reg["id"], "opening": 1.0}]})
            opts.append({"id": f"reg_cut_{d}", "name": f"Открыть регулятор, обесточить и вывести людей: {m.districts[d]['name']}",
                         "cmds": [{"cmd": "regulator", "arg": reg["id"], "opening": 1.0},
                                  {"cmd": "power_off", "arg": d}, {"cmd": "evacuate", "arg": d}]})
    if fire or m.summary()["max_co"] >= m.limits["co_mac_ppm"]:
        rev = {"cmd": "fan_reverse"} if m.mode_target == "нормальный" else {"cmd": "fan_normal"}
        rev_name = "Реверсировать ГВУ" if m.mode_target == "нормальный" else "Вернуть нормальный режим ГВУ"
        opts.append({"id": "evac", "name": "Вывести всех людей (режим ГВУ без изменений)", "cmds": [ev]})
        opts.append({"id": "rev_evac", "name": f"{rev_name} и вывести всех людей", "cmds": [rev, ev]})
        if fire:
            opts.append({"id": "fight_evac", "name": "Вывести людей и тушить пожар (ВГК)",
                         "cmds": [ev, {"cmd": "extinguish"}]})
    elif not any(o["id"] in ("reserve_evac", "vmp_evac") for o in opts) and any(a["level"] == "crit" for a in alerts(m)):
        opts.append({"id": "evac", "name": "Вывести всех людей", "cmds": [ev]})
    return opts


def _metrics(m, horizon_s, dt=5.0):
    L = m.limits
    worst_ch4 = 0.0
    over_limit_s = 0.0
    explosive_s = None
    smoke_pm = 0.0
    gas_pm = 0.0
    still_pm = 0.0  # people in workings without the minimum air velocity (Appendix 6, p.143)
    all_out_s = None
    t0 = m.t
    while m.t - t0 < horizon_s:
        m.step(dt)
        mx = 0.0
        for s in m.sensors:
            if s["type"] == "CH4":
                v = m.sensor_value(s)
                mx = max(mx, v / m.ch4_limit(s))
        worst_ch4 = max(worst_ch4, m.summary()["max_ch4"])
        if mx >= 1.0:
            over_limit_s += dt
        for c in m.crews:
            if not c.underground:
                continue
            if c.ch4_here >= L["ch4_explosive_lower_pct"] and explosive_s is None:
                explosive_s = m.t - t0
            if c.co_here >= L["co_alarm_ppm"] or c.vis_here < 5:
                smoke_pm += c.people * dt / 60
            if c.ch4_here >= L["ch4_district_return_pct"]:
                gas_pm += c.people * dt / 60
            b = m.br(c.branch)
            if abs(b["Q"]) / b["area"] < 0.25 and b["kind"] not in ("shaft",):
                still_pm += c.people * dt / 60
        if all_out_s is None and not any(c.underground for c in m.crews):
            all_out_s = m.t - t0
    s = m.summary()
    fed_max = max((c.fed for c in m.crews), default=0.0)
    return {
        "people_underground": s["people_underground"],
        "people_incapacitated": s["people_incapacitated"],
        "smoke_person_min": round(smoke_pm),
        "gas_person_min": round(gas_pm),
        "still_air_person_min": round(still_pm),
        "people_in_explosive_gas": explosive_s is not None,
        "fed_max": round(fed_max, 3),
        "max_ch4_pct": round(worst_ch4, 2),
        "ch4_over_limit_min": round(over_limit_s / 60, 1),
        "all_out_min": round(all_out_s / 60) if all_out_s is not None else None,
        "mine_air_m3min": round(s["mine_air"] * 60),
        "max_co_ppm": round(s["max_co"]),
    }


def _score(r):
    # Lives first, then exposure in smoke and gas, then methane above the limit. Withdrawing
    # people is not a goal in itself: it only scores through the exposure it avoids.
    return (r["people_incapacitated"] * 1000 + (500 if r["people_in_explosive_gas"] else 0)
            + r["fed_max"] * 300 + r["smoke_person_min"] * 2 + r["gas_person_min"] * 1
            + r["still_air_person_min"] * 1
            + r["ch4_over_limit_min"] * 3)


def forecast(m, horizon_min=60):
    t0 = time.time()
    results = []
    for opt in options(m):
        c = m.clone()
        for cmd in opt["cmds"]:
            c.apply(cmd, source="прогноз")
        r = _metrics(c, horizon_min * 60)
        r.update({"id": opt["id"], "name": opt["name"], "cmds": opt["cmds"]})
        r["score"] = round(_score(r), 1)
        results.append(r)
    results.sort(key=lambda r: (r["score"], len(r["cmds"])))  # equal outcome: the simpler decision
    best = results[0]
    return {
        "sim_time": m.t,
        "horizon_min": horizon_min,
        "options": results,
        "best": best["id"],
        "best_name": best["name"],
        "explain": explain(best, results, horizon_min),
        "computed_in_s": round(time.time() - t0, 2),
    }


def explain(best, results, base_h=60):
    base = next((r for r in results if r["id"] == "nothing"), None)
    if len(results) == 1:
        return "Отклонений от норм нет: менять режим проветривания и выводить людей не требуется."
    parts = [f"Лучший вариант на {len(results)} рассчитанных: «{best['name']}»."]
    if base and base is not best:
        if base["people_incapacitated"] > best["people_incapacitated"]:
            parts.append(f"Без действий за {base_h} мин потеряют способность к самоспасению "
                         f"{base['people_incapacitated']} чел.")
        if base["people_in_explosive_gas"] and not best["people_in_explosive_gas"]:
            parts.append("Без действий люди окажутся во взрывоопасной концентрации метана.")
        if base["ch4_over_limit_min"] > best["ch4_over_limit_min"]:
            parts.append(f"Метан выше нормы: {base['ch4_over_limit_min']} мин без действий против "
                         f"{best['ch4_over_limit_min']} мин.")
        if base["smoke_person_min"] > best["smoke_person_min"]:
            parts.append(f"Пребывание людей в дыму и CO: {base['smoke_person_min']} чел.-мин без действий против "
                         f"{best['smoke_person_min']}.")
        if base["still_air_person_min"] > best["still_air_person_min"]:
            parts.append(f"Пребывание людей в непроветриваемых выработках: {base['still_air_person_min']} чел.-мин "
                         f"без действий против {best['still_air_person_min']}.")
        if base["gas_person_min"] > best["gas_person_min"]:
            parts.append(f"Пребывание людей в метане выше 1%: {base['gas_person_min']} чел.-мин без действий против "
                         f"{best['gas_person_min']}.")
    if any(c["cmd"] == "evacuate" for c in best["cmds"]):
        if best["all_out_min"] is not None:
            parts.append(f"Все люди выйдут на поверхность через {best['all_out_min']} мин.")
        else:
            parts.append(f"Через {base_h} мин под землёй ещё будут {best['people_underground']} чел. (выход дольше часа).")
    return " ".join(parts)
