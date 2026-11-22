"""In the lab container: merge shard coverage + fixture/module records into one JSON keyed by test number.
usage: export_map.py TESTS_JSON MAP_DIR OUT_JSON"""
import json, sys, glob, collections
from coverage import CoverageData
tests = json.load(open(sys.argv[1]))["tests"]
d = sys.argv[2]
byid = {t["id"]: t["number"] for t in tests}
num = byid
grp = collections.defaultdict(list)  # "S:<class or file nodeid>" -> tests under it
for t in tests:
    i = t["id"]
    while "::" in i:
        i = i.rsplit("::", 1)[0]; grp["S:" + i].append(t["number"])
lines = collections.defaultdict(lambda: collections.defaultdict(set))  # file -> line -> {test}
glob_lines = collections.defaultdict(set)  # lines executed outside any test context
unk = collections.Counter()
for p in sorted(glob.glob(d + "/cov[0-9].*")):
    try:
        cd = CoverageData(p); cd.read()
    except Exception:
        continue
    for f in cd.measured_files():
        rel = f.replace("/opt/sqlglot/", "")
        for ln, ctxs in cd.contexts_by_lineno(f).items():
            for c in ctxs:
                if c == "": glob_lines[rel].add(ln)
                elif c in num: lines[rel][ln].add(num[c])
                elif c in grp: lines[rel][ln].update(grp[c])
                else: unk[c] += 1
fix, mods, used = collections.defaultdict(list), {}, {}
childused = collections.defaultdict(set)
for p in glob.glob(d + "/child.*.jsonl"):
    for line in open(p):
        r = json.loads(line)
        for n in ([byid[r["t"]]] if r["t"] in byid else grp.get(r["t"], [])):
            childused[n].update(r["u"])
for p in sorted(glob.glob(d + "/map[0-9].json")):
    for nid, v in json.load(open(p)).items():
        ns = [byid[nid]] if nid in byid else grp.get(nid, [])
        for n in ns:
            mods[n] = sorted(set(mods.get(n, [])) | set(v["mods"])); used[n] = sorted(set(used.get(n, [])) | set(v["used"]) | childused.get(n, set()))
            for f in v["fix"]:
                if n not in fix[f]: fix[f].append(n)
out = {"cov": {f: {str(l): sorted(s) for l, s in m.items()} for f, m in lines.items()},
       "glob": {f: sorted(s) for f, s in glob_lines.items()},
       "fix": fix, "mods": mods, "used": used}
json.dump(out, open(sys.argv[3], "w"))
print("files", len(lines), "unknown ctx", len(unk), list(unk)[:5], "fixtures", len(fix), "tests with cov", len({t for m in lines.values() for s in m.values() for t in s}))
