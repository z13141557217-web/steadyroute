"""Stability switch comparison: does leaving a node that keeps stalling prevent stalls?

Six nodes of one line with different stall rates (the 10-20 diagnostics: one Taiwan node was
left 35 times in a week, others 18, 13, 12, 8). Optional "bad spells": a node has a stretch of
30-90 minutes in which it stalls six times as often. Same round-trip model and the same
detection as model.py (design A: 1 s probe, 0.5 s suspicion, 4 confirmations of 0.7 s).

    python3 sim/detection/stability.py [runs] [out.json]
"""
import bisect, json, math, statistics, sys
import model

DAY = model.DAY
BASE = [("blip", 100, 0.2, 1.5), ("stall", 15, 2, 8), ("outage", 8, 30, 300)]   # per day, a node of multiplier 1
MULT = [3.0, 1.6, 1.0, 0.8, 0.5, 0.3]
STALL_WINDOW, STALL_MIN, COOLDOWN, CAP = 1800, 2, 1800, 6


def node_episodes(rng, mult, spells):
    out = []
    for kind, per_day, lo, hi in BASE:
        for _ in range(rng.poisson(per_day * mult)):
            s = rng.uniform(0, DAY); out.append((s, s + rng.uniform(lo, hi), kind))
    for _ in range(rng.poisson(spells)):
        a = rng.uniform(0, DAY); b = a + rng.uniform(1800, 5400)
        for kind, per_day, lo, hi in BASE:
            for _ in range(rng.poisson(per_day * mult * 5 * (b - a) / DAY)):
                s = rng.uniform(a, b); out.append((s, s + rng.uniform(lo, hi), kind))
    out.sort()
    merged = []
    for s, e, k in out:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e), merged[-1][2])
        else:
            merged.append((s, e, k))
    return merged


TAIL = [1.0] * 6        # per-node multiplier on the share of slow answers (0.15 s and up)


def rtt_tail(rng, tail):
    r = rng.random() / tail
    if r < 0.004: return rng.uniform(1.3, 2.6)
    if r < 0.016: return rng.uniform(0.7, 1.3)
    if r < 0.076: return rng.uniform(0.15, 0.7)
    return math.exp(rng.gauss(math.log(0.08), 0.25))


class Node(model.Node):
    def __init__(self, rng, mult, spells, tail=1.0):
        self.ep = node_episodes(rng, mult, spells); self.starts = [e[0] for e in self.ep]; self.loss = 0.003
        self.tail = tail

    def answer(self, rng, t, timeout, probe=True):
        if probe and rng.random() < self.loss: return None
        d = self.down_at(t)
        start = d[1] if d else t
        lat = rtt_tail(rng, self.tail)
        nxt = bisect.bisect_right(self.starts, start)
        if nxt < len(self.ep) and self.ep[nxt][0] < start + lat:
            start = self.ep[nxt][1]
        done = start + lat - t
        return done if done <= timeout else None


def run_full(policy, spells, seed, fast_counts=False, history=7200, need=STALL_MIN, slow_s=1.5, margin=None,
             tails=None, slow_weight=0.0, tail_exit=None):
    """Simulate one day of one line, then score page loads.
    slow_weight: how much an answer over 1 s counts against a node when choosing a target.
    tail_exit: (min extra slow answers in 30 min) for a lossless switch away from a node whose
    answers are slow more often than the best other node's."""
    rng = model.R(seed)
    tails = tails or TAIL
    nodes = [Node(rng, m, spells, tails[i]) for i, m in enumerate(MULT)]
    slows = [[] for _ in MULT]
    order = list(range(len(nodes))); rng.shuffle(order)
    cur = order[0]
    stalls = [[] for _ in nodes]
    log = []               # (time, from, to, kind)
    last_switch = -1e9

    def note(i, t):
        if not stalls[i] or t - stalls[i][-1] >= 10:
            stalls[i].append(t)

    def recent(i, t):
        return sum(1 for x in stalls[i] if t - x < STALL_WINDOW)

    fast = policy != "v0.5.4"
    interval = 1.0 if fast else 5.0
    t, next_cycle = 0.0, 0.0
    while t < DAY:
        node = nodes[cur]
        if t >= next_cycle:
            for i in range(len(nodes)):
                if True:
                    a = nodes[i].answer(rng, t, 3.0)
                    if i != cur or not fast_counts:
                        if a is None or (slow_s is not None and a > slow_s):
                            note(i, t)
                    if a is not None and a > 1.0:
                        slows[i].append(t)
            next_cycle += 5.0
            if tail_exit is not None and t - last_switch >= COOLDOWN \
                    and sum(1 for s_ in log if s_[3] == "stability" and t - s_[0] < DAY) < CAP:
                recent_slow = lambda i: sum(1 for x in slows[i] if t - x < STALL_WINDOW)
                best = min((i for i in range(len(nodes)) if i != cur), key=lambda i: (recent_slow(i), recent(i, t)))
                if recent_slow(cur) - recent_slow(best) >= tail_exit and recent_slow(best) * 2 <= recent_slow(cur) \
                        and recent(best, t) == 0 and nodes[best].down_at(t) is None:
                    log.append((t, cur, best, "stability")); last_switch = t; cur = best
                    continue
            hist = lambda i: sum(1 for x in stalls[i] if t - x < history)
            if margin is not None:
                best = min((i for i in range(len(nodes)) if i != cur), key=hist)
                trigger = hist(cur) - hist(best) >= margin and hist(best) * 2 <= hist(cur)
                cands = [best] if trigger else []
            else:
                trigger = recent(cur, t) >= need
                cands = [i for i in range(len(nodes)) if i != cur and recent(i, t) == 0]
            if policy == "v0.6 + 稳定性回优" and trigger and t - last_switch >= COOLDOWN \
                    and sum(1 for s in log if s[3] == "stability" and t - s[0] < DAY) < CAP:
                clean = [i for i in cands if nodes[i].down_at(t) is None]
                if clean:
                    target = min(clean, key=lambda i: sum(1 for x in stalls[i] if t - x < history))
                    log.append((t, cur, target, "stability")); last_switch = t; cur = target
                    continue
        if fast:
            a = node.answer(rng, t, 1.5)
            if a is not None and a <= 0.5:
                t += interval; continue
            if fast_counts:
                note(cur, t)
            at = t + 0.5
            answered = a is not None
            for k in range(4):
                if answered:
                    break
                if node.answer(rng, at + 0.15 * k, 0.7) is not None:
                    answered = True
            if answered:
                t += interval; continue
            decided = at + 0.45 + 0.7
        else:
            if node.answer(rng, t, 2.0) is not None:
                t += interval; continue
            note(cur, t)
            if any(node.answer(rng, t + 2.0 + off, 2.0) is not None for off in (0, 0.7)):
                t += interval; continue
            decided = t + 2.0 + 0.7 + 2.0
        others = [i for i in range(len(nodes)) if i != cur and nodes[i].down_at(decided) is None] or \
            [i for i in range(len(nodes)) if i != cur]
        target = min(others, key=lambda i: (sum(1 for x in stalls[i] if decided - x < history)
                                            + slow_weight * sum(1 for x in slows[i] if decided - x < history), i))
        log.append((decided, cur, target, "failover")); last_switch = decided; cur = target
        t = decided + interval
    times = [s[0] for s in log]
    owner = [order[0]] + [s[2] for s in log]
    slow1 = slow3 = 0; wait = 0.0
    for tp in model.pages(rng):
        k = bisect.bisect_right(times, tp)
        n = nodes[owner[k]]
        a = n.answer(rng, tp, 30.0, probe=False)
        done = None if a is None else tp + a
        # a failover after the page started closes its connection: it reloads on the new node
        j = k
        while j < len(log) and log[j][3] != "failover":
            j += 1
        if j < len(log) and log[j][1] == owner[k] and (done is None or log[j][0] < done):
            b = nodes[log[j][2]].answer(rng, log[j][0], 30.0, probe=False)
            done2 = log[j][0] + (b if b is not None else 30.0)
            done = done2 if done is None else min(done, done2)
        total = (done - tp if done is not None else 30.0) + 0.3
        wait += max(0.0, total - 1.5)
        slow1 += total > 1.5
        slow3 += total > 3.0
    return dict(failover=sum(1 for s in log if s[3] == "failover"),
                stability=sum(1 for s in log if s[3] == "stability"),
                slow1=slow1, slow3=slow3, wait=wait)


if __name__ == "__main__":
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    out = {}
    V = "v0.6 + 稳定性回优"
    variants = [("v0.5.4", {"label": "v0.5.4"}),
                ("v0.6", {"label": "v0.6（只在故障时换，选最稳的节点）"}),
                (V, {"label": "按卡顿次数回优：30 分钟卡 2 次、热备 0 次"}),
                ("v0.6", {"label": "采用：评分计入慢回应，慢回应明显多时无损回优", "slow_weight": 0.5, "tail_exit": 4})]
    scenarios = (("各节点卡顿多少不同", 0, [1.0] * 6),
                 ("卡顿和慢回应都因节点而异", 0, [3.0, 2.0, 1.0, 1.0, 0.5, 0.3]),
                 ("同上，另有每天 3 段坏时段", 3, [3.0, 2.0, 1.0, 1.0, 0.5, 0.3]))
    for label, spells, tails in scenarios:
        for policy, opts in variants:
            opts = dict(opts); name = opts.pop("label", policy)
            res = [run_full(policy, spells, 7000 + k, tails=tails, **opts) for k in range(runs)]
            row = {key: round(statistics.mean(r[key] for r in res), 1) for key in res[0]}
            out.setdefault(label, {})[name] = row
            print(label, "|", name, "|", row, flush=True)
    # the floor: no stalls at all, only the round-trip tail of a working node
    BASE[:] = []
    res = [run_full("v0.6", 0, 9000 + k) for k in range(runs)]
    out["floor"] = {key: round(statistics.mean(r[key] for r in res), 1) for key in res[0]}
    print("floor", out["floor"])
    if len(sys.argv) > 2:
        json.dump(out, open(sys.argv[2], "w"), ensure_ascii=False, indent=1)
