"""Checks of the model's physics and of the advisor, without the platform.

    cd team/vent && python -m unittest -v test_model
"""

import json
import os
import unittest

import advisor
from model import Model

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "mine.json"), encoding="utf-8"))
BASE = Model(CFG)


class AirNetwork(unittest.TestCase):
    def test_continuity_at_every_junction(self):
        for node in BASE.nodes:
            if node == "SURF":
                continue
            net = sum(b["Q"] for b in BASE.branches if b["v"] == node) - \
                sum(b["Q"] for b in BASE.branches if b["u"] == node)
            self.assertAlmostEqual(net, 0.0, places=6, msg=node)

    def test_square_law_around_meshes(self):
        nat = CFG["mine"]["natural_draft_pa"]
        for mesh in BASE.meshes:
            total = 0.0
            for i, s in mesh.items():
                b = BASE.branches[i]
                drop = b["R_eff"] * b["Q"] * abs(b["Q"])
                if b["id"] == BASE.fan_branch:
                    drop -= BASE._fan_head(b["Q"])[0] + nat
                total += s * drop
            self.assertLess(abs(total), 1.0)

    def test_design_air_quantity(self):
        # 12 000 m3/min, the order of one upcast shaft in the Karaganda basin (EPA 2013, Table A-2)
        self.assertAlmostEqual(BASE.summary()["mine_air"] * 60, 12000, delta=300)

    def test_fan_within_rated_power(self):
        fan = BASE.fans[0]
        self.assertLess(fan.flow * fan.pressure / fan.eff / 1000, fan.power_rated)

    def test_reversal_gives_at_least_60_percent(self):
        m = BASE.clone()
        m.apply({"cmd": "fan_reverse"})
        m.run(600)  # Order 351, p.136: within 10 minutes
        q = m.br(m.fan_branch)["Q"]
        self.assertLess(q, 0)
        self.assertGreaterEqual(abs(q) / BASE.q_normal, 0.6)

    def test_opening_a_regulator_adds_air(self):
        m = BASE.clone()
        before = m.br("w-l1-face")["Q"]
        m.apply({"cmd": "regulator", "arg": "w-l1-ret", "opening": 1.0})
        m.solve_air()
        self.assertGreater(m.br("w-l1-face")["Q"], before * 1.1)


class Gas(unittest.TestCase):
    def test_normal_state_within_limits(self):
        L = CFG["limits"]
        self.assertLess(BASE.mine_return_ch4(), L["ch4_mine_return_pct"])
        for s in BASE.sensors:
            if s["type"] == "CH4":
                self.assertLess(BASE.sensor_value(s), L["ch4_district_return_pct"], s["id"])

    def test_fan_trip_accumulates_methane_and_trips_power(self):
        m = BASE.clone()
        m.apply({"cmd": "fan_trip"})
        m.run(900)
        face = next(s for s in m.sensors if s["id"] == "s-ch4-l1-face")
        self.assertGreater(m.sensor_value(face), 2.0)
        self.assertFalse(m.districts["d-l1"]["power"])  # gas protection (p.182)

    def test_reserve_fan_clears_methane(self):
        m = BASE.clone()
        m.apply({"cmd": "fan_trip"})
        m.run(600)
        m.apply({"cmd": "fan_start", "arg": "fan-2"})
        m.run(1800)
        self.assertLess(m.summary()["max_ch4"], 1.0)

    def test_fire_co_downstream_only(self):
        m = BASE.clone()
        m.apply({"cmd": "fire", "arg": "fire-l2-ret"})
        m.run(1200)
        self.assertGreater(max(m.br("w-ret-main")["co"]), 50)
        self.assertLess(max(m.br("w-l1-face")["co"]), 1)  # upstream in another district


class Advisor(unittest.TestCase):
    def best(self, scenario, minutes=5):
        m = BASE.clone()
        m.apply(scenario)
        m.run(minutes * 60)
        return advisor.forecast(m)["best"]

    def test_fan_trip_start_reserve(self):
        self.assertIn(self.best({"cmd": "fan_trip"}), ("reserve", "reserve_evac"))

    def test_intake_fire_reverse(self):
        self.assertEqual(self.best({"cmd": "fire", "arg": "fire-main"}, 10), "rev_evac")

    def test_return_fire_do_not_reverse(self):
        self.assertNotEqual(self.best({"cmd": "fire", "arg": "fire-l2-ret"}, 10), "rev_evac")


if __name__ == "__main__":
    unittest.main()
