"""Detection-design comparison on modelled node behaviour (24 h per run).

A node is up except during outage episodes (blip / stall / outage). While up, every request or
probe needs one round trip drawn from a heavy-tailed distribution calibrated on the probe values
in the 10-14..10-20 diagnostics (mostly 60-100 ms, regular 150-700 ms, rare 1-2.5 s).
"""
import bisect, json, math, random, statistics, sys

DAY = 86400.0
ACTIVE = (9 * 3600, 23 * 3600)          # the user is at the Mac 14 h a day

def rtt(rng):
    r = rng.random()
    if r < 0.004: return rng.uniform(1.3, 2.6)
    if r < 0.016: return rng.uniform(0.7, 1.3)
    if r < 0.076: return rng.uniform(0.15, 0.7)
    return math.exp(rng.gauss(math.log(0.08), 0.25))

def episodes(rng, rates):
    out = []
    for kind, per_day, lo, hi in rates:
        n = rng.poisson(per_day) if hasattr(rng, "poisson") else sum(1 for _ in range(int(per_day * 3)) if rng.random() < 1 / 3)
        for _ in range(n):
            s = rng.uniform(0, DAY); out.append((s, s + rng.uniform(lo, hi), kind))
    out.sort()
    merged = []
    for s, e, k in out:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e), merged[-1][2])
        else:
            merged.append((s, e, k))
    return merged

class Node:
    def __init__(self, rng, rates):
        self.ep = episodes(rng, rates); self.starts = [e[0] for e in self.ep]
        self.loss = 0.003                 # an isolated probe that gets no answer
    def down_at(self, t):
        i = bisect.bisect_right(self.starts, t) - 1
        if i >= 0 and self.ep[i][0] <= t < self.ep[i][1]: return self.ep[i]
        return None
    def answer(self, rng, t, timeout, probe=True):
        """Seconds until the answer, or None if it does not arrive within timeout."""
        if probe and rng.random() < self.loss: return None
        d = self.down_at(t)
        start = d[1] if d else t           # packets resent until the node is back
        lat = rtt(rng)
        nxt = bisect.bisect_right(self.starts, start)
        if nxt < len(self.ep) and self.ep[nxt][0] < start + lat:
            start = self.ep[nxt][1]          # went down before the answer came back
        done = start + lat - t
        return done if done <= timeout else None

DESIGNS = {
    # name: probe interval s, probe timeout s, confirm: list of offsets, confirm timeout, passive, consecutive
    "v0.5.4 现行": dict(interval=5, timeout=2.0, confirm=[0, 0.7], ctimeout=2.0, passive=False),
    "v0.6 初稿（确认 6 秒）": dict(interval=5, timeout=2.0, confirm=[0, 3, 6], ctimeout=3.0, passive=False),
    "每 100 ms 探测，连续 5 次失败": dict(interval=0.1, timeout=1.0, consecutive=5, passive=False),
    "每 100 ms 探测，连续 10 次失败": dict(interval=0.1, timeout=1.0, consecutive=10, passive=False),
    "推荐：1 秒探测 + 真实流量 + 并发确认": dict(interval=1.0, timeout=1.5, confirm=[0, 0.15, 0.3], ctimeout=1.0, passive=True),
    "推荐（0.5 秒探测）": dict(interval=0.5, timeout=1.5, confirm=[0, 0.15, 0.3], ctimeout=1.0, passive=True),
    "推荐去掉真实流量信号": dict(interval=1.0, timeout=1.5, confirm=[0, 0.15, 0.3], ctimeout=1.0, passive=False),
}

def pages(rng):
    t, out = ACTIVE[0], []
    while True:
        t += rng.expovariate(1 / 12.0)    # a page every 12 s while in use
        if t >= ACTIVE[1]: return out
        out.append(t)

def run(design, rates, seed):
    rng = random.Random(seed)
    nodes = [Node(rng, rates) for _ in range(4)]
    cur, t = 0, 0.0
    switches = []                          # (time, false?)
    probes = 0
    page_times = pages(rng)
    triggers = []                          # passive triggers: time a page's connections are seen stuck
    # Passive signal: a page's new connections show upload but no download after 0.8 s.
    def page_stuck_at(node, tp):
        wait = design.get("suspect", 0.8)
        a = node.answer(rng, tp, wait, probe=False)
        return None if a is not None else tp + wait
    events = []
    # Walk time: probes on a fixed grid, passive triggers from pages; decide in time order.
    next_probe = 0.0; pi = 0; fails = 0
    def confirm(node, at):
        nonlocal probes
        res = []
        for off in design["confirm"]:
            probes += 1
            a = node.answer(rng, at + off, design["ctimeout"])
            res.append(None if a is None else at + off + a)
        oks = [x for x in res if x is not None]
        if oks: return False, min(oks)
        return True, at + design["confirm"][-1] + design["ctimeout"]
    switch_log = []
    while t < DAY:
        node = nodes[cur]
        # next event: probe or passive page trigger
        tp = page_times[pi] if pi < len(page_times) else DAY
        if pi < len(page_times) and design["passive"] and tp < next_probe:
            pi += 1
            stuck = page_stuck_at(node, tp)
            if stuck is None: continue
            t = stuck
            failed, at = confirm(node, t)
            if failed: switch_log.append((at, cur)); cur = (cur + 1) % len(nodes); next_probe = at + design["interval"]
            continue
        if pi < len(page_times) and not design["passive"] and tp < next_probe:
            pi += 1; continue
        t = next_probe; next_probe = t + design["interval"]
        if t >= DAY: break
        probes += 1
        a = node.answer(rng, t, design["timeout"])
        sus = design.get("suspect")
        if sus is not None:
            if a is not None and a <= sus:
                continue
            failed, at = confirm(node, t + sus)
            if failed:
                switch_log.append((at, cur)); cur = (cur + 1) % len(nodes); next_probe = at + design["interval"]
            continue
        if a is not None:
            fails = 0; continue
        if "consecutive" in design:
            fails += 1
            if fails >= design["consecutive"]:
                at = t + design["timeout"]; switch_log.append((at, cur)); cur = (cur + 1) % len(nodes); fails = 0
                next_probe = at
            continue
        failed, at = confirm(node, t + design["timeout"])
        if failed:
            switch_log.append((at, cur)); cur = (cur + 1) % len(nodes); next_probe = at + design["interval"]
    # ---- score: replay pages against the switch log
    sw_times = [s[0] for s in switch_log]
    def node_at(tq):
        return bisect.bisect_right(sw_times, tq) % len(nodes) if True else 0
    slow3 = slow10 = 0; wait = []
    for tp in page_times:
        k = bisect.bisect_right(sw_times, tp)
        n = nodes[k % len(nodes)]
        a = n.answer(rng, tp, 30.0, probe=False)
        done = None if a is None else tp + a
        # a switch with connection close after tp and before done: the browser retries on the new node
        if k < len(sw_times) and (done is None or sw_times[k] < done):
            retry = sw_times[k]
            b = nodes[(k + 1) % len(nodes)].answer(rng, retry, 30.0, probe=False)
            done2 = retry + (b if b is not None else 30.0)
            done = done2 if done is None else min(done, done2)
        total = (done - tp) if done is not None else 30.0
        total += 0.3                        # server time for the page itself
        wait.append(max(0.0, total - 1.5))
        if total > 3: slow3 += 1
        if total > 10: slow10 += 1
    # false switches: the node it left was answering again within 2 s
    false = 0; detect = []
    for at, idx in switch_log:
        d = nodes[idx].down_at(at)
        if d is None or d[1] - at < 2.0: false += 1
    for n_i, n in enumerate(nodes):
        for s, e, kind in n.ep:
            if e - s < 10: continue
            # was this node current at s, and when did we leave it?
            k = bisect.bisect_right(sw_times, s)
            if k % len(nodes) != n_i: continue
            leave = sw_times[k] if k < len(sw_times) else None
            if leave is not None and leave < e: detect.append(leave - s)
    return dict(switches=len(switch_log), false=false, slow3=slow3, slow10=slow10, pages=len(page_times), stall=sum(wait),
                detect=detect, probes=probes)

class R(random.Random):
    def poisson(self, lam):
        L, k, p = math.exp(-lam), 0, 1.0
        if lam > 50: return max(0, int(round(self.gauss(lam, math.sqrt(lam)))))
        while True:
            p *= self.random()
            if p <= L: return k
            k += 1
random.Random = R  # episodes() uses rng.poisson

SCENARIOS = {
    "多为短暂卡顿": [("blip", 300, 0.2, 1.5), ("stall", 30, 2, 8), ("outage", 3, 30, 300)],
    "两者都有": [("blip", 100, 0.2, 1.5), ("stall", 15, 2, 8), ("outage", 8, 30, 300)],
    "多为真实断线": [("blip", 20, 0.2, 1.5), ("stall", 5, 2, 8), ("outage", 12, 30, 300)],
}
if __name__ == "__main__":
    SEEDS = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    out = {}
    for sname, rates in SCENARIOS.items():
        out[sname] = {}
        for dname, design in DESIGNS.items():
            agg = [run(design, rates, 1000 + s) for s in range(SEEDS)]
            det = sorted(x for a in agg for x in a["detect"])
            row = {k: round(statistics.mean(a[k] for a in agg), 1) for k in ("switches", "false", "slow3", "slow10", "pages", "probes", "stall")}
            row["detect_median"] = round(statistics.median(det), 2) if det else None
            row["detect_p90"] = round(det[int(len(det) * 0.9) - 1], 2) if det else None
            out[sname][dname] = row
            print(sname, "|", dname, "|", row, flush=True)
    json.dump(out, open(sys.argv[2] if len(sys.argv) > 2 else "/dev/null", "w"), ensure_ascii=False, indent=1)
