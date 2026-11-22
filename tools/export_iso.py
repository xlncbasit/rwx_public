"""Merge isolated per-test results into full.json (same shape pack_map.py expects). usage: export_iso.py RES_DIR OUT_JSON"""
import collections, glob, json, sys
cov = collections.defaultdict(lambda: collections.defaultdict(list))
fix, mods, used, bad = collections.defaultdict(list), {}, {}, []
acc, mro = collections.defaultdict(list), {}
acci = collections.defaultdict(set)
imp = collections.defaultdict(lambda: collections.defaultdict(set))
for p in glob.glob(sys.argv[1] + "/*.json"):
    n = int(p.rsplit("/", 1)[1][:-5]); d = json.load(open(p))
    if d["rc"] not in (0, 5): bad.append(n)
    for f, ls in d["cov"].items():
        for l in ls: cov[f][l].append(n)
    for f, m in d["imp"].items():
        for l, ms in m.items(): imp[f][l].update(ms)
    for f in d["fix"]: fix[f].append(n)
    mods[n] = d["mods"]; used[n] = d["used"]; mro.update(d["mro"])
    for T, a in d["acc"]: acc[T + "|" + a].append(n)
    for T, a, m_ in d["acci"]: acci[T + "|" + a].add(m_)
json.dump({"cov": {f: {str(l): sorted(s) for l, s in m.items()} for f, m in cov.items()}, "glob": {}, "imp": {f: {l: sorted(s) for l, s in m.items()} for f, m in imp.items()}, "fix": fix, "mods": mods, "used": used, "acc": acc, "mro": mro, "acci": {k: sorted(v) for k, v in acci.items()}},
          open(sys.argv[2], "w"))
print("tests", len(mods), "files", len(cov), "nonzero rc", bad[:10], len(bad))
