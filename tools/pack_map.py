"""Compact full.json -> data/map.json.gz (run in the lab container). usage: pack_map.py TESTS_JSON FULL_JSON OUT_GZ"""
import json, sys, gzip, re
tests = json.load(open(sys.argv[1]))["tests"]
d = json.load(open(sys.argv[2]))
sets, idx = [], {}
cov = {}
for f, m in d["cov"].items():
    cov[f] = {}
    for l, s in m.items():
        k = tuple(s)
        if k not in idx:
            idx[k] = len(sets); sets.append(list(k))
        cov[f][l] = idx[k]
acc = {}
for k, v in d.get("acc", {}).items():
    t_ = tuple(sorted(v))
    if t_ not in idx:
        idx[t_] = len(sets); sets.append(list(t_))
    acc[k] = idx[t_]
modnames = {m for v in d["mods"].values() for m in v}
def modof(q):
    parts = q.split(".")
    for i in range(len(parts), 0, -1):
        if ".".join(parts[:i]) in modnames:
            return ".".join(parts[:i])
dep = {}
for T, mro in d.get("mro", {}).items():
    mt = modof(T)
    for c in mro:
        mc = modof(c)
        if mc and mt and mc != mt:
            dep.setdefault(mc, set()).add(mt)
impmods, impidx, impsets, imp = {}, {}, [], {}
for f, m in d.get("imp", {}).items():
    imp[f] = {}
    for l, ms in m.items():
        k = tuple(sorted(impmods.setdefault(x, len(impmods)) for x in ms))
        if k not in impidx:
            impidx[k] = len(impsets); impsets.append(list(k))
        imp[f][l] = impidx[k]
acci = {}
for k, ms in d.get("acci", {}).items():
    key = tuple(sorted(impmods.setdefault(x, len(impmods)) for x in ms))
    if key not in impidx:
        impidx[key] = len(impsets); impsets.append(list(key))
    acci[k] = impidx[key]
files = {t["file"] for t in tests}
fb = []
for f in files:
    src = open("/opt/sqlglot/" + f).read()
    if re.search(r"load_sql_fixture|FIXTURES_DIR|fixtures", src):
        fb += [t["number"] for t in tests if t["file"] == f]
out = {"sets": sets, "cov": cov, "fix": d["fix"], "fixfallback": sorted(fb), "mods": d["mods"], "used": d["used"],
       "eager": sorted(d["glob"]), "glob": {f: sorted(v) for f, v in d["glob"].items()},
       "imp": imp, "impsets": impsets, "impmods": sorted(impmods, key=impmods.get), "acci": acci, "acc": acc, "mro": d.get("mro", {}), "dep": {k: sorted(v) for k, v in dep.items()}}
with gzip.open(sys.argv[3], "wt", compresslevel=9) as fh:
    json.dump(out, fh, separators=(",", ":"))
print("sets", len(sets), "fixfallback", len(fb), "eager", len(out["eager"]))
