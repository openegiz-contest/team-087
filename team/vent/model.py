"""
Ventilation and gas model of an underground coal mine.

Three coupled parts, all driven by mine.json:

1. Air distribution. The mine is a network of workings (branches) between
   junctions (nodes); both shaft collars are the same node, the atmosphere.
   Flows obey Kirchhoff's laws: continuity at every junction and, around every
   closed mesh, sum(R*Q*|Q|) = fan pressure + natural draft (square law of
   resistance, Atkinson). Flows are written as mesh flows, which satisfies
   continuity by construction, and the mesh equations are solved by Newton's
   method (the Hardy Cross idea with a full Jacobian).

2. Transport. Every working is cut into cells of about 50 m. Methane (%),
   CO (ppm), smoke (g/m3) and the air temperature rise from a fire (K) move
   with the air (first-order upwind), disperse a little, and mix by flow at
   junctions. Sources: methane from longwalls and goaf drainage, CO and smoke
   from a fire (t-squared growth), CO from spontaneous heating.

3. People. Crews walk the network to the nearest exit along the cheapest path
   (time, with a penalty for smoke and CO), put on self-rescuers in bad air and
   accumulate a CO dose (Purser's fractional effective dose, FED).

Nothing here talks to the network: simulator.py publishes the state.
"""

import copy
import heapq
import math
import random

RHO_CP = 1.2 * 1005.0  # J/(m3 K), air
CO_DENSITY = 1.165     # kg/m3
SURFACE = "SURF"


class Fan:
    def __init__(self, cfg):
        self.id = cfg["id"]
        self.name = cfg["name"]
        self.model = cfg.get("model", "")
        self.h0 = cfg["h0_pa"]
        self.h2 = cfg["h2_pa_s2m6"]
        self.power_rated = cfg["power_rated_kw"]
        self.eff = cfg.get("efficiency", 0.75)
        self.role = cfg.get("role", "main")
        self.speed = 1.0 if cfg.get("state") == "running" else 0.0
        self.target = self.speed
        self.failed = False
        self.flow = 0.0
        self.pressure = 0.0

    @property
    def state(self):
        if self.failed:
            return "авария" if self.speed > 0.01 else "отказ"
        if self.target > 0.5:
            return "работает" if self.speed > 0.98 else "разгон"
        return "останов" if self.speed > 0.01 else "стоит"


class Fire:
    def __init__(self, site_id, site, cfg):
        self.site_id = site_id
        self.name = site["name"]
        self.branch = site["branch"]
        self.pos = site["pos"]
        self.cfg = cfg
        self.t = 0.0
        self.hrr = 0.0  # W
        self.suppress = 0.0  # 0..1, firefighting progress
        self.out = False

    def step(self, dt):
        if self.out:
            self.hrr = 0.0
            return
        self.t += dt
        hrr_max = self.cfg["hrr_max_mw"] * 1e6
        grown = hrr_max * min(1.0, (self.t / self.cfg["growth_s"]) ** 2)
        self.hrr = grown * (1.0 - self.suppress)
        if self.suppress > 0:
            self.suppress = min(1.0, self.suppress + dt / 900.0)
            if self.suppress >= 1.0:
                self.out = True
                self.hrr = 0.0


class Crew:
    def __init__(self, cfg, branch_len):
        self.id = cfg["id"]
        self.name = cfg["name"]
        self.people = cfg["people"]
        self.home_branch = cfg["branch"]
        self.branch = cfg["branch"]
        self.s = cfg["pos"] * branch_len  # metres from the branch's "from" node
        self.status = "работают"
        self.route = []  # [(branch_id, direction)], first item is the current branch
        self.target_exit = None
        self.wait = 0.0
        self.fed = 0.0
        self.rescuer_left_s = None  # set from config
        self.rescuer_on = False
        self.ch4_here = 0.0
        self.co_here = 0.0
        self.vis_here = 99.0
        self.dist_to_exit = 0.0
        self.replan_in = 0.0

    @property
    def underground(self):
        return self.status not in ("на поверхности",)

    @property
    def at_risk(self):
        return self.underground and (self.fed >= 0.3 or self.co_here >= 50 or self.ch4_here >= 2.0
                                     or self.status == "потеряли способность к самоспасению")


class Model:
    def __init__(self, cfg, seed=7, profile=None):
        self.cfg = cfg
        self.rng = random.Random(seed)
        self.t = 0.0
        self.limits = cfg["limits"]
        self.phys = cfg["physics"]
        self.events = []
        self.scenario = "норма"
        self.auto_gas_protection = True
        self.profile = profile or []  # [(t_s, factor)], cyclic, real methane dynamics
        self._ou = 0.0

        # --- network -----------------------------------------------------
        self.node_cfg = cfg["nodes"]
        self.nodes = sorted({self._node(n) for n in self.node_cfg})
        self.branches = []
        self.bidx = {}
        dx_target = self.phys["cell_length_m"]
        for b in cfg["branches"]:
            n = max(2, int(round(b["length"] / dx_target)))
            br = dict(b)
            br["u"] = self._node(b["from"])
            br["v"] = self._node(b["to"])
            br["n"] = n
            br["dx"] = b["length"] / n
            br["ch4"] = [0.0] * n
            br["co"] = [0.0] * n
            br["smoke"] = [0.0] * n
            br["heat"] = [0.0] * n
            br["Q"] = 0.0
            br["opening"] = b.get("regulator_opening", 1.0)
            self.bidx[b["id"]] = len(self.branches)
            self.branches.append(br)
        self.meshes = self._find_meshes()
        self.mesh_flow = [10.0] * len(self.meshes)

        # --- fans --------------------------------------------------------
        fs = cfg["fan_station"]
        self.fan_branch = fs["branch"]
        self.fans = [Fan(u) for u in fs["units"]]
        self.fan_mode = "нормальный"   # or "реверс"
        self.mode_target = "нормальный"
        self.reverse_factor = fs["reverse_factor"]
        self.start_time = fs["start_time_s"]
        self.rundown = fs["stop_rundown_s"]
        self.stopped_extra_R = fs["stopped_extra_R"]
        self.fan_stop_since = None
        self.q_normal = None

        # --- districts, power ----------------------------------------------
        self.districts = {d: {"name": v["name"], "power": True, "power_reason": ""}
                          for d, v in cfg["districts"].items()}
        self.ch4_mult = {d: 1.0 for d in self.districts}
        self.ch4_mult_target = {d: 1.0 for d in self.districts}

        # --- hazards -------------------------------------------------------
        self.fire = None
        self.spont = None  # seconds since spontaneous heating started

        # --- crews -------------------------------------------------------
        self.crews = []
        for c in cfg["brigades"]:
            crew = Crew(c, self.br(c["branch"])["length"])
            crew.rescuer_left_s = self.phys["self_rescuer_min"] * 60.0
            self.crews.append(crew)

        self.sensors = cfg["sensors"]
        self.solve_air()
        self.q_normal = abs(self.br(self.fan_branch)["Q"])
        # Settle the gas field so the twin starts in a steady state.
        for _ in range(240):
            self._transport(5.0)
        self.events = []
        self.log("info", "Модель запущена: нормальный режим проветривания")

    # ------------------------------------------------------------------ utils
    def _node(self, n):
        return SURFACE if self.node_cfg[n].get("surface") else n

    def br(self, bid):
        return self.branches[self.bidx[bid]]

    def log(self, level, text):
        self.events.append({"t": round(self.t), "level": level, "text": text})
        self.events = self.events[-200:]

    def clone(self):
        m = copy.copy(self)
        m.branches = copy.deepcopy(self.branches)
        m.fans = copy.deepcopy(self.fans)
        m.districts = copy.deepcopy(self.districts)
        m.ch4_mult = dict(self.ch4_mult)
        m.ch4_mult_target = dict(self.ch4_mult_target)
        m.fire = copy.deepcopy(self.fire)
        m.crews = copy.deepcopy(self.crews)
        m.mesh_flow = list(self.mesh_flow)
        m.events = []
        m.rng = random.Random(1)
        return m

    # --------------------------------------------------------- air network
    def _find_meshes(self):
        """Fundamental meshes: one per chord of a spanning tree."""
        adj = {n: [] for n in self.nodes}
        for i, b in enumerate(self.branches):
            adj[b["u"]].append((i, b["v"], +1))
            adj[b["v"]].append((i, b["u"], -1))
        parent = {SURFACE: None}
        order = [SURFACE]
        for n in order:
            for i, m, d in adj[n]:
                if m not in parent:
                    parent[m] = (i, n, d)  # reached m from n via branch i in direction d
                    order.append(m)
        tree = {p[0] for p in parent.values() if p}
        meshes = []
        for i, b in enumerate(self.branches):
            if i in tree:
                continue
            # chord u->v, close the loop through the tree v->...->u
            path_v = self._tree_path(parent, b["v"])
            path_u = self._tree_path(parent, b["u"])
            mesh = {i: 1}
            # walking v -> root along the tree, then root -> u
            for bi, d in path_v:
                mesh[bi] = mesh.get(bi, 0) - d
            for bi, d in path_u:
                mesh[bi] = mesh.get(bi, 0) + d
            meshes.append({k: v for k, v in mesh.items() if v})
        return meshes

    @staticmethod
    def _tree_path(parent, n):
        """Branches from the root to n, with their direction root->n."""
        path = []
        while parent[n]:
            i, prev, d = parent[n]
            path.append((i, d))
            n = prev
        return path

    def _fan_head(self, Q):
        """Fan station head (Pa) in the branch direction and its derivative."""
        best_h, best_dh = 0.0, 0.0
        f = self.reverse_factor
        for fan in self.fans:
            s = fan.speed
            if s <= 0.001:
                continue
            if self.fan_mode == "нормальный":
                if Q >= 0:
                    h, dh = s * s * fan.h0 - fan.h2 * Q * Q, -2 * fan.h2 * Q
                else:
                    h, dh = s * s * fan.h0, 0.0
            else:
                if Q <= 0:
                    h, dh = -(f * f * s * s * fan.h0 - fan.h2 * Q * Q), 2 * fan.h2 * Q
                else:
                    h, dh = -f * f * s * s * fan.h0, 0.0
            if abs(h) > abs(best_h):
                best_h, best_dh = h, dh
        return best_h, best_dh

    def _branch_R(self, b):
        R = b["R"]
        if b.get("regulator"):
            o = max(0.05, b["opening"])
            R += 0.5 * (1.0 / (o * o) - 1.0)
        if b["id"] == self.fan_branch:
            s = max((fan.speed for fan in self.fans), default=0.0)
            R += self.stopped_extra_R * (1.0 - s) ** 2
        if self.fire and self.fire.branch == b["id"]:
            # Throttling by the hot gases: the working's resistance grows with the fire.
            R += b["R"] * self.phys["fire"]["throttle_factor"] * self.fire.hrr / (self.phys["fire"]["hrr_max_mw"] * 1e6)
        return R

    def solve_air(self):
        nat = self.cfg["mine"].get("natural_draft_pa", 0.0)
        Rs = [self._branch_R(b) for b in self.branches]
        m = list(self.mesh_flow)
        k = len(self.meshes)
        for _ in range(60):
            Q = [0.0] * len(self.branches)
            for j, mesh in enumerate(self.meshes):
                for i, s in mesh.items():
                    Q[i] += s * m[j]
            F = [0.0] * k
            dF = [0.0] * len(self.branches)
            res = [0.0] * len(self.branches)
            for i, b in enumerate(self.branches):
                r = Rs[i] * Q[i] * abs(Q[i])
                d = 2 * Rs[i] * abs(Q[i]) + 1e-3
                if b["id"] == self.fan_branch:
                    h, dh = self._fan_head(Q[i])
                    r -= h + nat
                    d -= dh
                res[i], dF[i] = r, d
            J = [[0.0] * k for _ in range(k)]
            for j, mesh in enumerate(self.meshes):
                for i, s in mesh.items():
                    F[j] += s * res[i]
                for l, mesh2 in enumerate(self.meshes):
                    for i, s in mesh.items():
                        if i in mesh2:
                            J[j][l] += s * mesh2[i] * dF[i]
            step = _solve(J, [-f for f in F])
            m = [a + b for a, b in zip(m, step)]
            if max(abs(x) for x in step) < 1e-4:
                break
        self.mesh_flow = m
        Q = [0.0] * len(self.branches)
        for j, mesh in enumerate(self.meshes):
            for i, s in mesh.items():
                Q[i] += s * m[j]
        for i, b in enumerate(self.branches):
            b["Q"] = Q[i]
            b["R_eff"] = Rs[i]
        qf = self.br(self.fan_branch)["Q"]
        h, _ = self._fan_head(qf)
        for fan in self.fans:
            active = fan.speed > 0.001 and abs(h) > 0 and abs(self._single_head(fan, qf)) >= abs(h) - 1e-6
            fan.flow = abs(qf) if active else 0.0
            fan.pressure = abs(h) if active else 0.0

    def _single_head(self, fan, Q):
        saved = [x.speed for x in self.fans]
        for x in self.fans:
            if x is not fan:
                x.speed = 0.0
        h, _ = self._fan_head(Q)
        for x, s in zip(self.fans, saved):
            x.speed = s
        return h

    # ---------------------------------------------------------- transport
    def _node_values(self):
        """Flow-weighted mix of everything arriving at each junction."""
        acc = {n: [0.0, 0.0, 0.0, 0.0, 0.0] for n in self.nodes}
        for b in self.branches:
            Q = b["Q"]
            if abs(Q) < 1e-6:
                continue
            node, cell = (b["v"], -1) if Q > 0 else (b["u"], 0)
            a = acc[node]
            q = abs(Q)
            a[0] += q
            a[1] += q * b["ch4"][cell]
            a[2] += q * b["co"][cell]
            a[3] += q * b["smoke"][cell]
            a[4] += q * b["heat"][cell]
        out = {}
        for n, a in acc.items():
            if n == SURFACE or a[0] < 1e-6:
                out[n] = None if n != SURFACE else (0.0, 0.0, 0.0, 0.0)
            else:
                out[n] = (a[1] / a[0], a[2] / a[0], a[3] / a[0], a[4] / a[0])
        return out

    def _sources(self, dt_total):
        """Per-branch per-cell sources: (ch4 m3/s, co m3/s, smoke kg/s, heat W)."""
        src = {}
        f_prof = self._profile_factor()
        for b in self.branches:
            g = b.get("ch4_m3min", 0.0)
            if g:
                d = b["district"]
                share = b.get("ch4_cutting_share", 0.0)
                cutting = 1.0 if self.districts[d]["power"] else 0.0
                rate = g / 60.0 * ((1 - share) + share * cutting) * self.ch4_mult[d] * f_prof
                if b["kind"] == "longwall":
                    per = [rate / b["n"]] * b["n"]
                else:  # goaf drainage enters the return near the face
                    per = [0.0] * b["n"]
                    per[0] = rate * 0.6
                    per[1] = rate * 0.4
                src.setdefault(b["id"], [[0.0] * 4 for _ in range(b["n"])])
                for c in range(b["n"]):
                    src[b["id"]][c][0] += per[c]
        fcfg = self.phys["fire"]
        if self.fire and self.fire.hrr > 0:
            b = self.br(self.fire.branch)
            c = min(b["n"] - 1, int(self.fire.pos * b["n"]))
            mlr = self.fire.hrr / (fcfg["heat_of_combustion_mj_kg"] * 1e6)
            s = src.setdefault(b["id"], [[0.0] * 4 for _ in range(b["n"])])
            s[c][1] += mlr * fcfg["co_yield_kg_kg"] / CO_DENSITY
            s[c][2] += mlr * fcfg["smoke_yield_kg_kg"]
            s[c][3] += self.fire.hrr * 0.7  # the rest is lost to the rock near the fire
        if self.spont is not None:
            sc = self.phys["co_spontaneous"]
            b = self.br(sc["branch"])
            c = min(b["n"] - 1, int(sc["pos"] * b["n"]))
            rate = sc["co_m3min_max"] / 60.0 * min(1.0, self.spont / sc["ramp_s"])
            s = src.setdefault(b["id"], [[0.0] * 4 for _ in range(b["n"])])
            s[c][1] += rate
        return src

    def _profile_factor(self):
        if self.profile:
            period = self.profile[-1][0] + 60.0
            t = (self.t + self.phys.get("profile_start_min", 0) * 60.0) % period
            lo, hi = 0, len(self.profile) - 1
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if self.profile[mid][0] <= t:
                    lo = mid
                else:
                    hi = mid - 1
            return self.profile[lo][1]
        return max(0.6, 1.0 + self._ou)

    def _transport(self, dt):
        D = self.phys["dispersion_m2s"]
        k_heat = self.phys["fire"]["heat_loss_per_s"]
        umax = max(abs(b["Q"]) / b["area"] / b["dx"] for b in self.branches)
        nsub = max(1, int(math.ceil(dt * umax / 0.8)))
        h = dt / nsub
        src = self._sources(dt)
        decay = math.exp(-k_heat * h)
        for _ in range(nsub):
            nv = self._node_values()
            for b in self.branches:
                n, dx, A = b["n"], b["dx"], b["area"]
                V = A * dx
                Q = b["Q"]
                cu = nv[b["u"]]
                cv = nv[b["v"]]
                s = src.get(b["id"])
                for key, ki in (("ch4", 0), ("co", 1), ("smoke", 2), ("heat", 3)):
                    c = b[key]
                    left = cu[ki] if cu is not None else c[0]
                    right = cv[ki] if cv is not None else c[-1]
                    new = c[:]
                    a = abs(Q) * h / V
                    dd = D * h / (dx * dx)
                    for i in range(n):
                        up = (c[i - 1] if i > 0 else left) if Q >= 0 else (c[i + 1] if i < n - 1 else right)
                        lo = c[i - 1] if i > 0 else left
                        hi = c[i + 1] if i < n - 1 else right
                        new[i] = c[i] + a * (up - c[i]) + dd * (lo - 2 * c[i] + hi)
                    if s:
                        for i in range(n):
                            q = s[i][ki]
                            if q:
                                if ki == 0:
                                    new[i] += q * h / V * 100.0       # % vol
                                elif ki == 1:
                                    new[i] += q * h / V * 1e6         # ppm
                                elif ki == 2:
                                    new[i] += q * h / V * 1000.0      # g/m3
                                else:
                                    new[i] += q * h / (RHO_CP * V)    # K
                    if ki == 3:
                        new = [x * decay for x in new]
                    b[key] = [max(0.0, x) for x in new]

    # ---------------------------------------------------------------- crews
    def _cell(self, b, s):
        return min(b["n"] - 1, max(0, int(s / b["dx"])))

    def _branch_hazard(self, b):
        co = max(b["co"])
        vis = self.visibility(max(b["smoke"]))
        mult = 1.0
        if co >= self.limits["co_alarm_ppm"]:
            mult += 2.0 + co / 200.0
        if vis < 10:
            mult += 1.5
        return mult

    def _plan(self, crew):
        """Dijkstra from the crew's position to the cheapest exit."""
        b0 = self.br(crew.branch)
        walk = self.phys["walk_speed_ms"]
        graph = {}
        for b in self.branches:
            if b["u"] == SURFACE or b["v"] == SURFACE:
                continue  # shafts are left by cage or ladder at an exit node
            cost = b["length"] / walk * self._branch_hazard(b)
            graph.setdefault(b["u"], []).append((b["v"], cost, b["id"], +1))
            graph.setdefault(b["v"], []).append((b["u"], cost, b["id"], -1))
        exits = {n: (self.phys["cage_time_s"] + c.get("exit_penalty_s", 0))
                 for n, c in self.node_cfg.items() if c.get("exit")}
        hz = self._branch_hazard(b0)
        start = [(crew.s / walk * hz, b0["u"], [(b0["id"], -1)]),
                 ((b0["length"] - crew.s) / walk * hz, b0["v"], [(b0["id"], +1)])]
        best = {}
        pq = []
        for c, n, path in start:
            heapq.heappush(pq, (c, n, path))
        result = None
        while pq:
            c, n, path = heapq.heappop(pq)
            if n in best:
                continue
            best[n] = c
            if n in exits:
                total = c + exits[n]
                if result is None or total < result[0]:
                    result = (total, n, path)
            for m, w, bid, d in graph.get(n, []):
                if m not in best:
                    heapq.heappush(pq, (c + w, m, path + [(bid, d)]))
        return result

    def _route_distance(self, crew):
        dist = 0.0
        for k, (bid, d) in enumerate(crew.route):
            b = self.br(bid)
            if k == 0 and bid == crew.branch:
                dist += (b["length"] - crew.s) if d > 0 else crew.s
            else:
                dist += b["length"]
        return dist

    def _step_crews(self, dt):
        walk = self.phys["walk_speed_ms"]
        slow = self.phys["walk_speed_smoke_ms"]
        for crew in self.crews:
            if not crew.underground:
                continue
            b = self.br(crew.branch)
            c = self._cell(b, crew.s)
            crew.ch4_here = b["ch4"][c]
            crew.co_here = b["co"][c]
            crew.vis_here = self.visibility(b["smoke"][c])
            bad_air = crew.co_here >= self.phys["co_mask_on_ppm"] or crew.vis_here < 5
            if bad_air and crew.rescuer_left_s > 0:
                if not crew.rescuer_on:
                    self.log("warn", f"{crew.name}: включились в самоспасатели (CO {crew.co_here:.0f} ppm)")
                crew.rescuer_on = True
            if crew.rescuer_on:
                crew.rescuer_left_s = max(0.0, crew.rescuer_left_s - dt)
                if crew.rescuer_left_s <= 0:
                    crew.rescuer_on = False
                    self.log("crit", f"{crew.name}: ресурс самоспасателей исчерпан")
            if not crew.rescuer_on and crew.co_here > 0:
                # Purser FED for CO, RMV 25 L/min, D = 30 %COHb (ISO 13571)
                crew.fed += 3.317e-5 * crew.co_here ** 1.036 * (25.0 / 30.0) * dt / 60.0
            if crew.status == "потеряли способность к самоспасению":
                continue
            if crew.fed >= 1.0:
                crew.status = "потеряли способность к самоспасению"
                self.log("crit", f"{crew.name}: доза CO достигла уровня потери способности к самоспасению")
                continue
            if crew.status == "в клети":
                crew.wait -= dt
                if crew.wait <= 0:
                    crew.status = "на поверхности"
                    self.log("ok", f"{crew.name} ({crew.people} чел.) выведены на поверхность")
                continue
            if crew.status != "выходят":
                continue
            crew.replan_in -= dt
            if crew.replan_in <= 0 or not crew.route:
                plan = self._plan(crew)
                crew.replan_in = 60.0
                # Turn back only for a clearly better way out: crews do not
                # zig-zag in front of an advancing smoke front.
                if plan and (not crew.route or plan[2][0] == crew.route[0]
                             or plan[0] < 0.7 * getattr(crew, "route_cost", float("inf"))):
                    crew.route = plan[2]
                    crew.target_exit = plan[1]
                    crew.route_cost = plan[0]
                elif plan:
                    crew.route_cost = plan[0]
            speed = slow if crew.vis_here < 5 else walk
            move = speed * dt
            while move > 0 and crew.route:
                bid, d = crew.route[0]
                if bid != crew.branch:
                    crew.branch = bid
                    crew.s = 0.0 if d > 0 else self.br(bid)["length"]
                b = self.br(bid)
                remaining = (b["length"] - crew.s) if d > 0 else crew.s
                if move < remaining:
                    crew.s += move * d
                    move = 0
                else:
                    crew.s = b["length"] if d > 0 else 0.0
                    move -= remaining
                    crew.route.pop(0)
            crew.dist_to_exit = self._route_distance(crew)
            if not crew.route:
                exit_cfg = self.node_cfg.get(crew.target_exit, {})
                crew.status = "в клети"
                crew.wait = self.phys["cage_time_s"] + exit_cfg.get("exit_penalty_s", 0)
                self.log("info", f"{crew.name} у выхода: {exit_cfg.get('name', crew.target_exit)}")

    # --------------------------------------------------------------- fans
    def _step_fans(self, dt):
        for fan in self.fans:
            if fan.failed:
                fan.target = 0.0
            if fan.speed < fan.target:
                fan.speed = min(fan.target, fan.speed + dt / self.start_time)
            elif fan.speed > fan.target:
                fan.speed = max(fan.target, fan.speed - dt / self.rundown)
        if self.mode_target != self.fan_mode:
            # Reversal: run the fans down, switch the reversing doors, run up.
            if all(f.speed <= 0.01 for f in self.fans):
                self.fan_mode = self.mode_target
                for fan in self.fans:
                    if getattr(fan, "resume", False):
                        fan.target = 1.0
                        fan.resume = False
                self.log("info", f"Вентиляционные ляды переключены: режим «{self.fan_mode}»")
        running = any(f.speed > 0.5 for f in self.fans)
        if not running and self.fan_stop_since is None:
            self.fan_stop_since = self.t
        elif running:
            self.fan_stop_since = None

    # ------------------------------------------------------- gas protection
    def _gas_protection(self):
        for sensor in self.sensors:
            if sensor["type"] != "CH4":
                continue
            b = self.br(sensor["branch"])
            d = b["district"]
            v = self.sensor_value(sensor)
            limit = self.limits["ch4_face_pct"] if b["kind"] == "longwall" else self.limits["ch4_district_return_pct"]
            if d == "d-main":
                continue
            if v >= limit and self.districts[d]["power"] and self.auto_gas_protection:
                self.districts[d]["power"] = False
                self.districts[d]["power_reason"] = "АГЗ"
                self.log("crit", f"АГЗ: {sensor['name']} = {v:.2f}% ≥ {limit}%: электроэнергия участка "
                                 f"«{self.districts[d]['name']}» отключена")

    # ------------------------------------------------------------------ step
    def step(self, dt):
        self.t += dt
        self._ou += -self._ou * dt / 900.0 + self.rng.gauss(0, 0.03) * math.sqrt(dt / 60.0)
        for d in self.ch4_mult:
            tgt = self.ch4_mult_target[d]
            cur = self.ch4_mult[d]
            self.ch4_mult[d] = cur + (tgt - cur) * min(1.0, dt / 300.0)
        if self.fire:
            self.fire.step(dt)
        if self.spont is not None:
            self.spont += dt
        self._step_fans(dt)
        self.solve_air()
        self._transport(dt)
        self._gas_protection()
        self._step_crews(dt)

    def run(self, seconds, dt=5.0):
        n = int(seconds / dt)
        for _ in range(n):
            self.step(dt)

    # ------------------------------------------------------------- commands
    def apply(self, cmd, source="диспетчер"):
        """Apply a command dict {"cmd": name, ...}. Returns a short result text."""
        name = cmd.get("cmd")
        arg = cmd.get("arg")
        fan_by_id = {f.id: f for f in self.fans}
        if name == "fan_trip":
            fan = next((f for f in self.fans if f.speed > 0.5), self.fans[0])
            fan.failed = True
            self.scenario = "отказ ГВУ"
            self.log("crit", f"Сценарий: аварийная остановка {fan.name}")
            return f"{fan.name}: отказ"
        if name == "fan_start":
            fan = fan_by_id.get(arg) or next(f for f in self.fans if not f.failed)
            if fan.failed:
                return f"{fan.name} неисправна"
            if self.mode_target != self.fan_mode:
                fan.resume = True
            else:
                fan.target = 1.0
            self.log("info", f"{source}: пуск {fan.name}")
            return f"Пуск {fan.name}"
        if name == "fan_stop":
            fan = fan_by_id.get(arg) or next(f for f in self.fans if f.target > 0)
            fan.target = 0.0
            self.log("warn", f"{source}: остановка {fan.name}")
            return f"Остановка {fan.name}"
        if name == "fan_repair":
            for fan in self.fans:
                if fan.failed:
                    fan.failed = False
                    self.log("ok", f"{fan.name}: неисправность устранена, готова к пуску")
            return "Ремонт выполнен"
        if name in ("fan_reverse", "fan_normal"):
            target = "реверс" if name == "fan_reverse" else "нормальный"
            if self.mode_target == target:
                return f"Режим уже «{target}»"
            self.mode_target = target
            for fan in self.fans:
                if fan.target > 0.5:
                    fan.resume = True
                    fan.target = 0.0
            self.log("warn", f"{source}: перевод ГВУ в режим «{target}»")
            return f"Режим ГВУ → {target}"
        if name in ("power_off", "power_on"):
            targets = list(self.districts) if arg in (None, "all") else [arg]
            for d in targets:
                if d == "d-main" and arg in (None, "all") and name == "power_on":
                    pass
                self.districts[d]["power"] = name == "power_on"
                self.districts[d]["power_reason"] = "" if name == "power_on" else source
            self.log("warn" if name == "power_off" else "info",
                     f"{source}: {'отключение' if name == 'power_off' else 'включение'} электроэнергии: "
                     + ", ".join(self.districts[d]["name"] for d in targets))
            return "ok"
        if name == "evacuate":
            n = 0
            for crew in self.crews:
                if crew.status == "работают" and (arg in (None, "all") or
                                                  self.br(crew.branch)["district"] == arg):
                    crew.status = "выходят"
                    crew.replan_in = 0.0
                    n += crew.people
            self.log("warn", f"{source}: вывод людей ({'вся шахта' if arg in (None, 'all') else self.districts[arg]['name']}), {n} чел.")
            return f"Выводятся {n} чел."
        if name == "methane_rise":
            d = arg or "d-l1"
            self.ch4_mult_target[d] = float(cmd.get("factor", 3.0))
            self.scenario = "рост метана"
            self.log("crit", f"Сценарий: рост газовыделения ×{self.ch4_mult_target[d]:.1f} на участке «{self.districts[d]['name']}»")
            return "ok"
        if name == "co_rise":
            self.spont = 0.0
            self.scenario = "рост CO"
            self.log("crit", "Сценарий: самонагревание угля в выработанном пространстве лавы 2-С (рост CO)")
            return "ok"
        if name == "fire":
            site = arg or "fire-l1-conv"
            self.fire = Fire(site, self.cfg["fire_sites"][site], self.phys["fire"])
            self.scenario = "пожар"
            self.log("crit", f"Сценарий: {self.fire.name}")
            return "ok"
        if name == "extinguish":
            if self.fire and not self.fire.out:
                self.fire.suppress = max(self.fire.suppress, 0.01)
                self.log("info", f"{source}: начато тушение пожара (ВГК)")
            return "ok"
        if name == "regulator":
            b = self.br(arg)
            b["opening"] = max(0.05, min(1.0, float(cmd.get("opening", 1.0))))
            self.log("info", f"{source}: регулятор «{b['name']}» открыт на {b['opening'] * 100:.0f}%")
            return "ok"
        if name == "auto_gas_protection":
            self.auto_gas_protection = bool(cmd.get("on", True))
            return "ok"
        return f"неизвестная команда {name}"

    # ---------------------------------------------------------------- views
    def visibility(self, smoke_gm3):
        k = self.phys["fire"]["mass_extinction_m2_g"] * smoke_gm3
        return min(99.0, 3.0 / k) if k > 1e-9 else 99.0

    def sensor_value(self, s):
        b = self.br(s["branch"])
        c = min(b["n"] - 1, int(s["pos"] * b["n"]))
        if s["type"] == "CH4":
            return b["ch4"][c]
        if s["type"] == "CO":
            return b["co"][c]
        return abs(b["Q"]) / b["area"]

    def district_stats(self, d):
        bs = [b for b in self.branches if b["district"] == d]
        return {
            "max_ch4": max(max(b["ch4"]) for b in bs),
            "max_co": max(max(b["co"]) for b in bs),
            "min_vis": min(self.visibility(max(b["smoke"])) for b in bs),
            "people": sum(c.people for c in self.crews if c.underground and self.br(c.branch)["district"] == d),
        }

    def mine_return_ch4(self):
        return self.sensor_value(next(s for s in self.sensors if s["id"] == "s-ch4-mine"))

    def summary(self):
        return {
            "people_underground": sum(c.people for c in self.crews if c.underground),
            "people_at_risk": sum(c.people for c in self.crews if c.at_risk),
            "people_incapacitated": sum(c.people for c in self.crews
                                        if c.status == "потеряли способность к самоспасению"),
            "max_ch4": max(max(b["ch4"]) for b in self.branches),
            "max_co": max(max(b["co"]) for b in self.branches),
            "mine_air": abs(self.br(self.fan_branch)["Q"]),
        }


def _solve(A, b):
    """Gaussian elimination with partial pivoting (tiny systems)."""
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        if abs(M[c][c]) < 1e-12:
            continue
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] if abs(M[i][i]) > 1e-12 else 0.0 for i in range(n)]
