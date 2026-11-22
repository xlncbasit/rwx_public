"""Compare labeling output (OUT_DIR/<id>.json) with answers.json. usage: cmp_labels.py TESTS_JSON ANSWERS_JSON OUT_DIR"""
import json, sys, os
tests = json.load(open(sys.argv[1]))["tests"]
ans = json.load(open(sys.argv[2]))["commits"]
by_file = {}
by_id = {}
for t in tests:
    by_id[t["id"]] = t["number"]; by_file.setdefault(t["file"], []).append(t["number"])
ok = bad = 0
for c, a in sorted(ans.items()):
    p = f"{sys.argv[3]}/{c}.json"
    if not os.path.exists(p): print(c, "pending"); continue
    d = json.load(open(p))
    got = {by_id[i] for i in d["failed"] if i in by_id}
    for f in d["filefail"]:
        got |= set(by_file.get(f, []))
    exp = set(a["failing"])
    if got == exp: ok += 1
    else:
        bad += 1
        print(c, a["kind"], "MISMATCH missing", sorted(exp - got), "extra", sorted(got - exp), [i for i in d["failed"] if i not in by_id][:3])
print("match", ok, "mismatch", bad)
