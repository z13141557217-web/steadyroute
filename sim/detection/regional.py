"""v0.6.0 vs v0.6.1 on a line like the 10-21 Taiwan record (24 h per run, 1 s fast lane).

Calibrated on the 10-21 22:40-22:45 detection record:
- hy2 nodes: no connection setup per probe; relay nodes: 0.35-0.8 s of setup that Mihomo's
  reported delay leaves out but its timeout includes (a relay reported 140-470 ms while 8 of 9
  probes took longer than 0.5 s).
- isolated probe losses: hy2 2%, relays 5% (evening record: one miss in 10 probes on hy2).
- regional disturbances: every node of the line stalls for 1-6 s about every 15 s, and each
  node has a 30% chance to go down for 30-300 s (record: 22:41-22:42, nine of fifteen nodes
  failed more than 30% of their checks; one hy2 node went down for minutes).
- qualification: "Q0" no candidate meets 95% / 90% (the 10-20 diagnostics: all fifteen Taiwan
  nodes under 90% over 24 h), "Q2" two hy2 nodes meet it.

Policies follow the code: v0.6.0 (flat 0.5 s suspicion, 0.7 s confirmation probes, best score
among qualified nodes else among answering ones, switch on confirmation) and v0.6.1 (waits
from each node's 75th-percentile real answer time, 5 s of silence when no backup meets the
targets or in a storm, clean-last-minute targets first).
"""
import bisect, json, math, random, statistics, sys

DAY = 86400.0
ACTIVE = (9 * 3600, 23 * 3600)


def poisson(rng, lam):
    if lam > 50:
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


def rtt(rng):
    r = rng.random()
    if r < 0.004: return rng.uniform(1.3, 2.6)
    if r < 0.016: return rng.uniform(0.7, 1.3)
    if r < 0.076: return rng.uniform(0.15, 0.7)
    return math.exp(rng.gauss(math.log(0.08), 0.25))


class Node:
    def __init__(self, rng, kind, qualified, disturbances):
        self.kind, self.qualified = kind, qualified
        self.loss = 0.02 if kind == "hy2" else 0.05
        self.setup = (0.0, 0.0) if kind == "hy2" else (0.35, 0.8)
        rates = ([("blip", 100, 0.2, 1.5), ("stall", 20, 2, 8), ("outage", 6, 30, 300)] if kind == "hy2"
                 else [("blip", 150, 0.2, 1.5), ("stall", 40, 2, 8), ("outage", 10, 30, 300)])
        ep = []
        for _kind, per_day, lo, hi in rates:
            for _ in range(poisson(rng, per_day)):
                s = rng.uniform(0, DAY); ep.append((s, s + rng.uniform(lo, hi)))
        for start, end in disturbances:
            t = start
            while True:
                t += rng.expovariate(1 / 15.0)
                if t >= end: break
                ep.append((t, t + rng.uniform(1, 6)))
            if rng.random() < 0.3:
                s = rng.uniform(start, end); ep.append((s, s + rng.uniform(30, 300)))
        ep.sort()
        merged = []
        for s, e in ep:
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        self.ep = merged
        self.starts = [e[0] for e in merged]
        self.score = (180 if kind == "hy2" else 320) + rng.uniform(-40, 40)

    def down(self, t):
        i = bisect.bisect_right(self.starts, t) - 1
        return self.ep[i] if i >= 0 and self.ep[i][0] <= t < self.ep[i][1] else None

    def answer(self, rng, t, timeout, probe=True, rounds=1):
        """Real seconds until the answer (setup included), or None past timeout."""
        if probe and rng.random() < self.loss:
            return None
        d = self.down(t)
        start = d[1] if d else t
        lat = rng.uniform(*self.setup) + sum(rtt(rng) for _ in range(rounds))
        nxt = bisect.bisect_right(self.starts, start)
        if nxt < len(self.ep) and self.ep[nxt][0] < start + lat:
            start = self.ep[nxt][1]
        done = start + lat - t
        return done if done <= timeout else None


POLICIES = {
    "v0.6.0": dict(adaptive=False, clean=False, hold=0.0),
    "只改起疑时间与选目标": dict(adaptive=True, clean=True, hold=0.0),
    "原方案：不达标或频繁切换时等 5 秒": dict(adaptive=True, clean=True, hold=5.0),
    "v0.6.1：只在频繁切换时等 5 秒": dict(adaptive=True, clean=True, hold=5.0, storm_only=True),
}


def run(policy, qualified_count, seed, disturbances_per_day=6):
    spec = POLICIES[policy]
    rng = random.Random(seed)
    disturbances = []
    for _ in range(poisson(rng, disturbances_per_day)):
        s = rng.uniform(18 * 3600, 24 * 3600 - 300)    # evenings
        disturbances.append((s, s + rng.uniform(60, 180)))
    kinds = ["hy2"] * 3 + ["relay"] * 5
    nodes = [Node(rng, kind, index < qualified_count, disturbances) for index, kind in enumerate(kinds)]
    n = len(nodes)
    window = [[] for _ in range(n)]         # full-check results, 1 ok / 0 fail
    streak = [5] * n
    pause_need = [0] * n
    elapsed = [[] for _ in range(n)]        # real answer times of fast-URL probes
    failover_times = []
    switches = []                           # (time, from, to)
    outage_since = None
    cur = 0
    next_full = 0.0

    def suspect(i):
        if not spec["adaptive"] or len(elapsed[i]) < 10:
            return 0.5
        e = sorted(elapsed[i][-60:]); p75 = e[min(len(e) - 1, int(len(e) * 0.75))]
        return min(1.5, max(0.5, p75 + 0.3))

    def full_check(t):
        for i, node in enumerate(nodes):
            a = node.answer(rng, t, 3.0, rounds=2)
            if a is None:
                a = node.answer(rng, t + 3.0, 3.0, rounds=2)    # same-cycle retry
                ok = a is not None
                if not ok and not pause_need[i]:
                    pause_need[i] = 5
            else:
                ok = True
            window[i] = (window[i] + [1 if ok else 0])[-360:]
            streak[i] = streak[i] + 1 if ok else 0
            if ok and pause_need[i] and streak[i] >= pause_need[i]:
                pause_need[i] = 0
            if ok and i != cur:
                b = node.answer(rng, t, 1.0)                 # standby / fast-URL sample
                if b is not None:
                    elapsed[i] = (elapsed[i] + [b])[-60:]

    def answering(i):
        return streak[i] >= 2 and not pause_need[i]

    def qualified(i):
        return nodes[i].qualified and answering(i) and streak[i] >= 3

    def clean(i):
        last = window[i][-12:]
        return len(last) >= 6 and all(last)

    def target(t):
        others = [i for i in range(n) if i != cur]
        pool = [i for i in others if qualified(i)] or [i for i in others if answering(i)]
        if not pool:
            return None
        if not spec["clean"]:
            return min(pool, key=lambda i: nodes[i].score)
        return min(pool, key=lambda i: (not clean(i), nodes[i].score))

    t = 0.0
    while t < DAY:
        if t >= next_full:
            full_check(t); next_full += 5.0
        node = nodes[cur]
        limit = suspect(cur)
        a = node.answer(rng, t, 2.5 if spec["adaptive"] else 1.5)
        if a is not None and a <= limit:
            elapsed[cur] = (elapsed[cur] + [a])[-60:]
            outage_since = None
            t += 1.0; continue
        ctimeout = max(0.7, limit + 0.2) if spec["adaptive"] else 0.7
        answered = a is not None and a <= limit + 0.45 + ctimeout
        if a is not None:
            elapsed[cur] = (elapsed[cur] + [a])[-60:]
        if not answered:
            for k in range(4):
                b = node.answer(rng, t + limit + 0.15 * k, ctimeout)
                if b is not None:
                    answered = True; elapsed[cur] = (elapsed[cur] + [b])[-60:]; break
        decided = t + limit + 0.45 + ctimeout
        if answered:
            outage_since = None
            t = max(t + 1.0, decided); continue
        if outage_since is None:
            outage_since = t
        window_s, count = spec.get("storm", (600, 3))
        storm = sum(1 for x in failover_times if decided - x < window_s) >= count
        weak = not any(qualified(i) for i in range(n) if i != cur)
        need_long = spec["hold"] > 0 and (storm or (weak and not spec.get("storm_only")))
        if need_long and decided - outage_since < spec["hold"]:
            t = max(t + 1.0, decided); continue
        to = target(decided)
        if to is None:
            t = max(t + 1.0, decided); continue
        switches.append((decided, cur, to)); failover_times.append(decided)
        cur, outage_since = to, None
        t = max(t + 1.0, decided)

    # ---- pages against the switch log
    sw_times = [s[0] for s in switches]
    path = [0] + [s[2] for s in switches]
    t, pages = ACTIVE[0], []
    while True:
        t += rng.expovariate(1 / 12.0)
        if t >= ACTIVE[1]: break
        pages.append(t)
    slow3 = slow10 = 0; wait = 0.0
    for tp in pages:
        k = bisect.bisect_right(sw_times, tp)
        a = nodes[path[k]].answer(rng, tp, 30.0, probe=False)
        done = None if a is None else tp + a
        if k < len(sw_times) and (done is None or sw_times[k] < done):
            b = nodes[path[k + 1]].answer(rng, sw_times[k], 30.0, probe=False)
            done2 = sw_times[k] + (b if b is not None else 30.0)
            done = done2 if done is None else min(done, done2)
        total = ((done - tp) if done is not None else 30.0) + 0.3
        wait += max(0.0, total - 1.5)
        slow3 += total > 3; slow10 += total > 10
    false = sum(1 for at, frm, _to in switches
                if nodes[frm].down(at) is None or nodes[frm].down(at)[1] - at < 3.0)
    stuck = []
    for at, frm, _to in switches:
        d = nodes[frm].down(at)
        if d and d[1] - d[0] >= 30:
            stuck.append(at - d[0])
    return dict(switches=len(switches), false=false, slow3=slow3, slow10=slow10, wait=round(wait, 1),
                stuck=stuck, pages=len(pages))


if __name__ == "__main__":
    seeds = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    out = {}
    for scenario, q, per_day in (("无达标备用，晚间地区抖动 6 次", 0, 6), ("两个达标 hy2，晚间地区抖动 6 次", 2, 6),
                                 ("无达标备用，没有地区抖动", 0, 0)):
        out[scenario] = {}
        for policy in POLICIES:
            agg = [run(policy, q, 4000 + s, per_day) for s in range(seeds)]
            stuck = sorted(x for a in agg for x in a["stuck"])
            row = {k: round(statistics.mean(a[k] for a in agg), 1) for k in ("switches", "false", "slow3", "slow10", "wait", "pages")}
            row["stuck_median"] = round(statistics.median(stuck), 1) if stuck else None
            row["stuck_p90"] = round(stuck[int(len(stuck) * 0.9) - 1], 1) if stuck else None
            out[scenario][policy] = row
            print(scenario, "|", policy, "|", row, flush=True)
    json.dump(out, open(sys.argv[2] if len(sys.argv) > 2 else "/dev/null", "w"), ensure_ascii=False, indent=1)
