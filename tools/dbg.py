import sys, json, gzip
sys.path.insert(0, "/app")
import selector as S, patchlib
tests = json.load(open("/k/tests.json"))["tests"]
cx = S.Ctx(tests, json.load(gzip.open("/app/data/map.json.gz", "rt")))
ans = json.load(open("/k/answers.json"))["commits"]
T = {t["number"]: t["id"] for t in tests}
for c in sys.argv[1:]:
    p = open(f"/k/commits/{c}.patch").read()
    got = set()
    for fp in patchlib.parse(p):
        r = S.impact_file(cx, fp); print(c, fp.path, "->", "ALL" if r is None else len(r))
        got |= cx.all if r is None else r
        if fp.path.startswith("sqlglot/"):
            for g in S.groups(fp):
                print("  group", g)
                for l in g[0]:
                    print("   line", l, "cov", len(cx.cov.get(fp.path, {}).get(l, [])))
    miss = set(ans[c]["failing"]) - got
    print(" missing", [(n, T[n]) for n in miss])
