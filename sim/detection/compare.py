import json, statistics, sys, multiprocessing as mp
import model as sim
D = {
 "v0.5.4 现行（5 秒探测，2 次确认）": dict(interval=5, timeout=2.0, confirm=[0, 0.7], ctimeout=2.0, passive=False),
 "上次的初稿（6 秒确认）": dict(interval=5, timeout=2.0, confirm=[0, 3, 6], ctimeout=3.0, passive=False),
 "每 100 ms 探测，连续 5 次失败即切": dict(interval=0.1, timeout=1.0, consecutive=5, passive=False),
 "每 100 ms 探测，连续 10 次失败即切": dict(interval=0.1, timeout=1.0, consecutive=10, passive=False),
 "A：1 秒探测，0.5 秒无回应即并发确认 4 次": dict(interval=1.0, timeout=1.5, suspect=0.5, confirm=[0, 0.15, 0.3, 0.45], ctimeout=0.7, passive=True),
 "B：0.5 秒探测，0.7 秒无回应即并发确认 3 次": dict(interval=0.5, timeout=1.5, suspect=0.7, confirm=[0, 0.15, 0.3], ctimeout=0.9, passive=True),
 "C：0.25 秒探测，0.5 秒无回应即并发确认 4 次": dict(interval=0.25, timeout=1.5, suspect=0.5, confirm=[0, 0.15, 0.3, 0.45], ctimeout=0.9, passive=True),
 "B 去掉真实流量信号": dict(interval=0.5, timeout=1.5, suspect=0.7, confirm=[0, 0.15, 0.3], ctimeout=0.9, passive=False),
}
def job(a):
    name, s, k = a
    return name, s, sim.run(D[name], sim.SCENARIOS[s], 5000 + k)
if __name__ == "__main__":
    seeds = int(sys.argv[1])
    tasks = [(n, s, k) for n in D for s in sim.SCENARIOS for k in range(seeds)]
    with mp.Pool(2) as p: res = p.map(job, tasks, chunksize=2)
    agg = {}
    for n, s, r in res: agg.setdefault((n, s), []).append(r)
    out = []
    for (n, s), runs in agg.items():
        det = sorted(x for r in runs for x in r["detect"])
        out.append(dict(name=n, scenario=s, **{k: round(statistics.mean(r[k] for r in runs), 1) for k in ("switches", "false", "slow3", "slow10", "stall", "probes", "pages")},
                        detect_median=round(statistics.median(det), 2), detect_p90=round(det[int(len(det) * .9) - 1], 2)))
    json.dump(out, open(sys.argv[2], "w"), ensure_ascii=False, indent=1)
    for o in out: print(o["scenario"], "|", o["name"], "|", {k: o[k] for k in ("switches","false","slow3","stall","probes","detect_median","detect_p90")})
