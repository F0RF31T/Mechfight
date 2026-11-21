"""Survival-first mechfight bot.

Placement is what scores, so the plan is: stay inside the zone, grab a good weapon and cubes early,
fight only when it's cheap, and never stand in a bullet's path. Standing still / walking slowly is
silent (protocol §8), so when nothing is going on the bot sneaks.
"""
import array
import json
import math
import os
import random
import sys

import mechfight as mf

CELL = 2.0

# ---- experiment switches: set one to False to test without it
FLEE_HP, FLEE_N = 0.0, 99   # never flee (2.79 was 0.3, 4)
JUMP_FIGHT = False         # jump while fighting/fleeing
LIGHTNING_FIRST = False    # prefer lightning over laser, and close in to ~22 m when holding it
CUBE_FARM = False          # chase cubes, drones and crates from further away
CUBE_RANGE = 140 if CUBE_FARM else 80
FARM_RANGE = 90 if CUBE_FARM else 55
TAU = math.tau
WEAPON_VALUE = {"gun": 0.0, "mortar": 1.5, "lightning": 3.0 if LIGHTNING_FIRST else 2.5, "laser": 2.5 if LIGHTNING_FIRST else 3.0}
UPGRADE_PRIORITY = ["armor", "plating", "nanites", "servos", "gyros", "gun_damage", "gun_rate",
                    "laser_damage", "laser_charge", "lightning_chain", "capacitor", "reactor", "jets",
                    "gun_mag", "gun_velocity", "mortar_splash", "sensors"]


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def unit(x, y):
    n = math.hypot(x, y)
    return (x / n, y / n) if n > 1e-9 else (0.0, 0.0)


# ------------------------------------------------------------------ map: buildings -> a coarse grid
class Grid:
    def __init__(self, arena):
        w = json.load(open(os.path.join(arena, "v2", "world.json")))
        T = w["terrain"]
        self.x0, self.y0 = T["minX"], T["minY"]
        self.gw = int(T["nx"] * T["step"] / CELL) + 2
        self.gh = int(T["ny"] * T["step"] / CELL) + 2
        ter = array.array("h")
        with open(os.path.join(arena, "v2", T["file"]), "rb") as f:
            ter.frombytes(f.read())
        if sys.byteorder == "big":
            ter.byteswap()

        def ground(x, y):
            i = min(T["nx"] - 1, max(0, int(round((x - T["minX"]) / T["step"]))))
            j = min(T["ny"] - 1, max(0, int(round((y - T["minY"]) / T["step"]))))
            return ter[j * T["nx"] + i] * 0.01

        raw = bytearray(self.gw * self.gh)
        for p in w["prisms"]:
            ring = p["ring"]
            if len(ring) < 6:
                continue
            cx, cy = sum(ring[0::2]) / (len(ring) // 2), sum(ring[1::2]) / (len(ring) // 2)
            if p["kind"] == "bridge" and p["base"] - ground(cx, cy) > 9.5:
                continue  # you can walk under it
            edges = []
            for poly in [ring] + list(p.get("holes") or []):
                pts = list(zip(poly[0::2], poly[1::2]))
                for k in range(len(pts)):
                    edges.append((pts[k], pts[(k + 1) % len(pts)]))
            ys = ring[1::2]
            j0 = max(0, int((min(ys) - self.y0) / CELL) - 1)
            j1 = min(self.gh - 1, int((max(ys) - self.y0) / CELL) + 1)
            for j in range(j0, j1 + 1):
                yc = self.y0 + (j + 0.5) * CELL
                xs = []
                for (ax, ay), (bx, by) in edges:
                    if (ay <= yc) != (by <= yc):
                        xs.append(ax + (yc - ay) * (bx - ax) / (by - ay))
                xs.sort()
                for k in range(0, len(xs) - 1, 2):
                    i0 = max(0, int(math.ceil((xs[k] - self.x0) / CELL - 0.5)))
                    i1 = min(self.gw - 1, int(math.floor((xs[k + 1] - self.x0) / CELL - 0.5)))
                    if i1 >= i0:
                        raw[j * self.gw + i0: j * self.gw + i1 + 1] = b"\x01" * (i1 - i0 + 1)
        self.raw = raw  # exact-ish footprints: used for line of sight
        fat = bytearray(raw)  # one cell bigger: used for walking (mech radius is 2.5 m)
        gw = self.gw
        for idx in [i for i, v in enumerate(raw) if v]:
            for d in (-gw - 1, -gw, -gw + 1, -1, 1, gw - 1, gw, gw + 1):
                if 0 <= idx + d < len(fat):
                    fat[idx + d] = 1
        self.fat = fat

    def cell(self, x, y):
        i, j = int((x - self.x0) / CELL), int((y - self.y0) / CELL)
        if 0 <= i < self.gw and 0 <= j < self.gh:
            return j * self.gw + i
        return -1

    def walk_blocked(self, x, y):
        c = self.cell(x, y)
        return c < 0 or self.fat[c] == 1

    def clear_dist(self, x, y, ang, maxd):
        ca, sa = math.cos(ang), math.sin(ang)
        d = CELL
        while d <= maxd:
            if self.walk_blocked(x + ca * d, y + sa * d):
                return d - CELL
            d += CELL
        return maxd

    def los(self, ax, ay, bx, by):
        n = int(math.hypot(bx - ax, by - ay) / CELL)
        for k in range(1, n):
            c = self.cell(ax + (bx - ax) * k / n, ay + (by - ay) * k / n)
            if c >= 0 and self.raw[c]:
                return False
        return True


# ------------------------------------------------------------------ the bot
class Bot:
    name = "survivor-py"

    def __init__(self, hello):
        self.cfg = hello["config"]
        self.rng = random.Random(hello["seed"])
        try:
            self.grid = Grid(hello["map"]["dir"])
        except Exception as e:  # no map: still play, just without wall avoidance
            mf.log("grid failed:", repr(e))
            self.grid = None
        self.strafe = 1
        self.flip_at = 0
        self.prev_ang = None
        self.slow_ticks = 0
        self.unstuck_until = -1
        self.unstuck_ang = 0.0
        self.pk = {}  # remembered weapon pickups id -> (kind, pos)
        self.cb = {}  # remembered cubes id -> pos
        self.hit_bearing = None
        self.hit_tick = -999
        self.walk = self.cfg["mech"]["walk_speed"]
        self.last_mode = None

    # ---- helpers
    def los(self, p, q):
        return self.grid.los(p[0], p[1], q[0], q[1]) if self.grid else True

    def choose_upgrade(self, offer):
        best, bi = 99, 0
        for i, c in enumerate(offer["choices"]):
            r = UPGRADE_PRIORITY.index(c["id"]) if c["id"] in UPGRADE_PRIORITY else 50
            if r < best:
                best, bi = r, i
        return bi

    def remember(self, obs, x, y):
        for ev in obs["events"]:
            k = ev["type"]
            if k == "took" or k == "take_failed":
                self.pk.pop(ev["id"], None)
            elif k == "damaged" and ev.get("bearing") is not None:
                self.hit_bearing, self.hit_tick = ev["bearing"], obs["tick"]
        seen = set()
        for p in obs["pickups"]:
            self.pk[p["id"]] = (p["kind"], p["pos"])
            seen.add(p["id"])
        for i in [i for i, (_, pos) in self.pk.items() if i not in seen and math.hypot(pos[0] - x, pos[1] - y) < 25]:
            del self.pk[i]
        seen = set()
        for c in obs["cubes"]:
            self.cb[c["id"]] = c["pos"]
            seen.add(c["id"])
        for i in [i for i, pos in self.cb.items() if i not in seen and math.hypot(pos[0] - x, pos[1] - y) < 25]:
            del self.cb[i]

    def steer(self, x, y, dx, dy, boosting):
        """Turn a wish (dx,dy) into the best nearby walkable heading."""
        want = math.atan2(dy, dx)
        if not self.grid:
            return want
        look = 22.0 if boosting else 16.0
        best, ba = -9.0, want
        for k in range(-12, 12):
            off = k * math.radians(15)
            a = want + off
            clear = self.grid.clear_dist(x, y, a, look)
            s = math.cos(off) + 1.3 * clear / look - (2.0 if clear < 5 else 0.0)
            if self.prev_ang is not None and abs(wrap(a - self.prev_ang)) < 0.3:
                s += 0.25  # hysteresis: don't dither
            if s > best:
                best, ba = s, a
        return ba

    # ---- aiming
    def solve(self, kind, me, tgt, stats):
        x, y, z = me["pos"]
        mh = me["stats"].get("muzzle_height", 7.0)
        tx, ty, tz = tgt["pos"]
        vx, vy, vz = tgt["vel"]
        tz += 4.5
        if kind == "mortar":
            mh += 2.5
            v = stats.get("speed", 72.0)
            g = stats.get("gravity", 22.0)
            t = math.hypot(tx - x, ty - y) / v
            for _ in range(3):
                px, py, pz = tx + vx * t, ty + vy * t, tz + vz * t
                D = math.hypot(px - x, py - y)
                dz = pz - (z + mh)
                disc = v ** 4 - g * (g * D * D + 2 * dz * v * v)
                if disc < 0 or D < 1:
                    return None
                pitch = math.atan2(v * v - math.sqrt(disc), g * D)
                t = D / (v * math.cos(pitch))
            return math.atan2(py - y, px - x), pitch, D
        speed = stats.get("speed", 1e9) if kind == "gun" else 1e9
        t = math.hypot(tx - x, ty - y) / speed if speed < 1e8 else 0.08
        for _ in range(2):
            px, py, pz = tx + vx * t, ty + vy * t, tz + vz * t
            D = math.hypot(px - x, py - y)
            if speed < 1e8:
                t = D / speed
        dz = pz - (z + mh)
        return math.atan2(py - y, px - x), math.atan2(dz, max(D, 1e-6)), D

    def pick_weapon(self, me, d):
        """Return (slot, kind) of the weapon to use at distance d, or None."""
        ws = me["weapons"]
        have = {w["kind"]: (i, w) for i, w in enumerate(ws) if w}
        if "laser" in have and have["laser"][1].get("charge", 0) > 0:
            return have["laser"][0], "laser"  # finish what we started
        if "lightning" in have and d < 38 and me["energy"] > 12 and have["lightning"][1]["cooldown"] < 0.3:
            return have["lightning"][0], "lightning"
        if "laser" in have and d < 250 and have["laser"][1]["cooldown"] < 0.2:
            return have["laser"][0], "laser"
        if "mortar" in have and 30 < d < 200 and have["mortar"][1]["cooldown"] < 0.2:
            return have["mortar"][0], "mortar"
        if "gun" in have:
            g = have["gun"][1]
            if g.get("ammo", 0) > 0 and g.get("reload", 0) <= 0 and d < g["stats"].get("range", 230) * 0.8:
                return have["gun"][0], "gun"
        return None

    def trace(self, obs, me, mode, foes, dc, safe):
        """One short stderr line on damage, mode change, and every 5 s. The last lines of a match show how we died."""
        try:
            dmg = [f"{e['weapon']}-{e['amount']:.0f}{'R' if e.get('rear') else ''}@{e['by']}" for e in obs['events'] if e['type'] == 'damaged']
            if dmg or mode != self.last_mode or obs['tick'] % 50 == 0:
                wp = ','.join(w['kind'] for w in me['weapons'] if w)
                mf.log(f"t={obs['t']:.1f} alive={obs['alive']} hp={me['hp']:.0f}/{me['max_hp']:.0f} mode={mode} "
                       f"zone={'in' if me['in_zone'] else 'OUT'} dc={dc:.0f}/{safe:.0f} foes={len(foes)} "
                       f"w={wp} cubes={me['cubes']} dmg={dmg}")
            self.last_mode = mode
        except Exception:
            pass

    # ---- main decision
    def act(self, obs):
        me = obs["self"]
        x, y, z = me["pos"]
        vx, vy = me["vel"][0], me["vel"][1]
        tick, t = obs["tick"], obs["t"]
        hpf = me["hp"] / me["max_hp"]
        act = {"tick": tick}
        if obs["offer"]:
            act["pick"] = self.choose_upgrade(obs["offer"])
        self.remember(obs, x, y)

        # -- zone: head for the circle it is *about to* close to, and stay well inside it
        zn = obs["zone"]
        if t >= zn["shrink_start"] - 15:
            zc, zr = zn["next_center"], zn["next_radius"]
        else:
            zc, zr = zn["center"], zn["radius"]
        safe = max(zr * 0.7 - 4, 5.0)
        dcx, dcy = zc[0] - x, zc[1] - y
        dc = math.hypot(dcx, dcy)
        out_zone = not me["in_zone"] or dc > safe

        foes, others = [], []
        for m in obs["mechs"]:
            m["_d"] = math.hypot(m["pos"][0] - x, m["pos"][1] - y)
            (foes if m["kind"] == "mech" else others).append(m)
        foes.sort(key=lambda m: m["_d"])
        near = [m for m in foes if m["_d"] < 130]

        dv = [0.0, 0.0]
        boost = False
        mode = "roam"
        speed = 1.0

        def add(vec, w):
            ux, uy = unit(*vec)
            dv[0] += ux * w
            dv[1] += uy * w

        # -- target for aim and fire (independent of where we walk)
        target, kind_t = None, None
        cand = [m for m in foes if m["_d"] < 230 and self.los(me["pos"], m["pos"])]
        if cand:
            target = min(cand, key=lambda m: m["_d"] + m.get("hp", 1000) * 0.04)
            kind_t = "mech"
        elif foes:
            target, kind_t = foes[0], "mech-nolos"
        elif not out_zone:
            pool = [m for m in others if m["_d"] < FARM_RANGE and self.los(me["pos"], m["pos"])]
            pool += [dict(c, _d=math.hypot(c["pos"][0] - x, c["pos"][1] - y), vel=[0, 0, 0]) for c in obs["crates"]
                     if math.hypot(c["pos"][0] - x, c["pos"][1] - y) < FARM_RANGE - 5 and self.los(me["pos"], c["pos"])]
            if pool:
                target, kind_t = min(pool, key=lambda m: m["_d"]), "farm"

        # -- movement
        if out_zone:
            mode = "ZONE"
            add((dcx, dcy), 3.0)
            boost = me["energy"] > 15
        elif near:
            f = near[0]
            nf = len(near)
            away = (x - f["pos"][0], y - f["pos"][1])
            perp = (-away[1], away[0])
            if tick >= self.flip_at:
                self.strafe = -self.strafe
                self.flip_at = tick + self.rng.randint(6, 14)
            if hpf < FLEE_HP or nf >= FLEE_N:
                mode = "flee"
                add(away, 2.0)
                add((dcx, dcy), 0.8)
                add(perp, 0.8 * self.strafe)
                boost = me["energy"] > 40
            else:
                mode = "fight"
                ideal = 50.0
                have = {w["kind"] for w in me["weapons"] if w}
                if "lightning" in have:
                    ideal = 22.0 if LIGHTNING_FIRST else 28.0
                elif "laser" in have:
                    ideal = 80.0
                if f["_d"] < ideal - 12:
                    add(away, 1.2)
                elif f["_d"] > ideal + 25:
                    add((-away[0], -away[1]), 1.0)
                add(perp, 1.4 * self.strafe)
        else:
            # nothing hostile around: loot, capture drops, or wait inside the circle
            goal, gw = None, 0
            for d in obs["drops"]:
                dd = math.hypot(d["pos"][0] - x, d["pos"][1] - y)
                if dd < 150 and math.hypot(d["pos"][0] - zc[0], d["pos"][1] - zc[1]) < zr + 10:
                    if dd > d["radius"] * 0.5 or d["lands_in"] > 0:
                        goal, mode = d["pos"], "drop"
                    else:
                        mode, speed = "hold-drop", 0.0
                    break
            if goal is None and mode == "roam":
                best = None
                held = {w["kind"]: w for w in me["weapons"] if w}
                slot1 = me["weapons"][1]
                cur = WEAPON_VALUE[slot1["kind"]] if slot1 else -1.0
                for i, (k, pos) in self.pk.items():
                    if k in held or WEAPON_VALUE[k] <= cur:
                        continue
                    dd = math.hypot(pos[0] - x, pos[1] - y)
                    if dd < 160 and math.hypot(pos[0] - zc[0], pos[1] - zc[1]) < zr:
                        s = dd * 0.5
                        if best is None or s < best[0]:
                            best = (s, pos, "loot-" + k)
                for i, pos in self.cb.items():
                    dd = math.hypot(pos[0] - x, pos[1] - y)
                    if dd < CUBE_RANGE and math.hypot(pos[0] - zc[0], pos[1] - zc[1]) < zr:
                        if best is None or dd < best[0]:
                            best = (dd, pos, "cube")
                if best:
                    goal, mode = best[1], best[2]
            if goal is not None:
                add((goal[0] - x, goal[1] - y), 1.0)
            elif mode == "roam":
                if kind_t == "farm" and target["_d"] > 30:
                    add((target["pos"][0] - x, target["pos"][1] - y), 1.0)
                    mode = "farm"
                elif dc > safe * 0.5:
                    add((dcx, dcy), 1.0)
                else:
                    speed = 0.0  # in position: stand still, it is silent and regenerates HP
        # keep drifting inward when near the edge of the safe area
        if mode not in ("ZONE", "hold-drop") and dc > safe * 0.8:
            add((dcx, dcy), 1.5)
        # sneak (under step_speed 5 m/s) when just repositioning and nobody is near
        if mode == "roam" and speed > 0 and dc < safe * 0.9:
            speed = 0.4

        # -- dodge: incoming projectiles and charging lasers beat everything but the zone
        dodge = False
        for pr in obs["projectiles"]:
            px, py = pr["pos"][0] - x, pr["pos"][1] - y
            qx, qy = pr["vel"][0], pr["vel"][1]
            s2 = qx * qx + qy * qy
            if s2 < 1:
                continue
            tca = -(px * qx + py * qy) / s2
            if 0 < tca < 1.3:
                cx, cy = px + qx * tca, py + qy * tca
                if math.hypot(cx, cy) < 6:
                    if math.hypot(cx, cy) < 0.5:
                        cx, cy = -qy, qx
                    add((-cx, -cy), 3.0)
                    dodge = True
        for f in foes:
            if f.get("charging") and f["_d"] < 280:
                bearing = math.atan2(y - f["pos"][1], x - f["pos"][0])
                if abs(wrap(f["aim"][0] - bearing)) < 0.15:
                    add((-math.sin(f["aim"][0]) * self.strafe, math.cos(f["aim"][0]) * self.strafe), 3.0)
                    dodge = True
        if dodge:
            boost = boost or me["energy"] > 30

        # -- walls and getting stuck
        mag = math.hypot(*dv)
        spd = math.hypot(vx, vy)
        if mag > 0.05 and speed > 0.5 and spd < 1.5:
            self.slow_ticks += 1
        else:
            self.slow_ticks = 0
        if self.slow_ticks > 8 and tick > self.unstuck_until:
            self.unstuck_until = tick + 14
            self.unstuck_ang = self.rng.choice([-1, 1]) * self.rng.uniform(1.0, 2.2)
            self.slow_ticks = 0
        if mag > 0.05 and speed > 0:
            base = math.atan2(dv[1], dv[0])
            if tick <= self.unstuck_until:
                base += self.unstuck_ang
                act["jump"] = True
                boost = me["energy"] > 20
            ang = self.steer(x, y, math.cos(base), math.sin(base), boost)
            self.prev_ang = ang
            act["move"] = [round(math.cos(ang) * speed, 3), round(math.sin(ang) * speed, 3)]
        else:
            act["move"] = [0.0, 0.0]
            self.prev_ang = None
        act["boost"] = bool(boost)
        if JUMP_FIGHT and mode in ("fight", "flee") and me["grounded"] and tick % 8 == 0:
            act["jump"] = True

        # -- aim and fire
        aim = None
        fire = [False, False]
        ws = me["weapons"]
        if target is not None:
            d = target["_d"]
            choice = self.pick_weapon(me, d) if kind_t in ("mech", "farm") else None
            if kind_t == "mech-nolos":
                # no clear line: lob a mortar if we have one, otherwise just face them
                choice = self.pick_weapon(me, d)
                if choice and choice[1] != "mortar":
                    choice = None
            use = choice[1] if choice else "gun"
            wstats = ws[choice[0]]["stats"] if choice else (ws[0]["stats"] if ws[0] else {})
            sol = self.solve(use, me, target, wstats)
            if sol is None and use == "mortar":
                choice, sol = None, self.solve("gun", me, target, ws[0]["stats"] if ws[0] else {})
            if sol:
                yaw, pitch, D = sol
                aim = [yaw, max(self.cfg["mech"]["min_pitch"], min(self.cfg["mech"]["max_pitch"], pitch))]
                if choice:
                    slot, k = choice
                    eyaw = abs(wrap(me["aim"][0] - yaw))
                    epit = abs(me["aim"][1] - aim[1])
                    tol = max(0.03, math.atan2(2.0, max(D, 1.0)))
                    ok = eyaw < tol and epit < tol * 1.5
                    if k == "laser":
                        ch = ws[slot].get("charge", 0)
                        if ch >= 0.7:
                            fire[slot] = not ok  # release (fire=False) the moment we're lined up
                        else:
                            fire[slot] = True
                    elif k == "lightning":
                        fire[slot] = eyaw < 0.35
                    else:
                        fire[slot] = ok and (kind_t != "mech-nolos" or k == "mortar") and (k != "mortar" or D > 18)
        if aim is None:
            if tick - self.hit_tick < 12 and self.hit_bearing is not None:
                aim = [self.hit_bearing, 0.0]
            elif "move" in act and (act["move"][0] or act["move"][1]):
                aim = [math.atan2(act["move"][1], act["move"][0]), 0.0]
        if aim:
            act["aim"] = [round(aim[0], 4), round(aim[1], 4)]
        act["fire"] = fire
        g = ws[0]
        if g and g["kind"] == "gun" and not any(fire) and g.get("ammo", 12) < g["stats"].get("mag", 12) and g.get("reload", 0) <= 0 and (g.get("ammo", 12) <= 2 or not foes):
            act["reload"] = True

        # -- weapon pickup
        slot1 = ws[1] if len(ws) > 1 else None
        cur = WEAPON_VALUE[slot1["kind"]] if slot1 else -1.0
        held = {w["kind"] for w in ws if w}
        for p in obs["pickups"]:
            if p["kind"] in held or WEAPON_VALUE[p["kind"]] <= cur:
                continue
            if math.hypot(p["pos"][0] - x, p["pos"][1] - y) < 3.4 and abs(p["pos"][2] - z) < 3.5 and not near:
                act["take"] = p["id"]
                if slot1:
                    act["drop"] = 1
                break

        self.trace(obs, me, mode, foes, dc, safe)
        mf.draw.label(f"{mode} hp{me['hp']:.0f} f{len(foes)}")
        if target is not None:
            mf.draw.line([x, y, z + 7], [target["pos"][0], target["pos"][1], target["pos"][2] + 4.5], "#ff5040")
        mf.draw.point([zc[0], zc[1], z], "#40c0ff", 6)
        return act


mf.run(Bot)
