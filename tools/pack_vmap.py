"""Pack value-map results: usage: pack_vmap.py RES_DIR OUT.json.gz
cmp: node -> {relation codes joined: [tests]};  bool: node -> {"T": all operands truthy every time, "F": all falsy,
"X": otherwise: [tests]};  bad: tests whose instrumented run did not pass (never judged by the map)."""
import glob, gzip, json, os, sys

cmp, boo, bad = {}, {}, []
for p in glob.glob(os.path.join(sys.argv[1], "*.json")):
    n = int(os.path.basename(p)[:-5])
    d = json.load(open(p))
    if d.get("rc") != 0 or d.get("exit") != 0:
        bad.append(n)
        continue
    for node, codes in d["cmp"].items():
        cmp.setdefault(node, {}).setdefault("".join(sorted(codes)), []).append(n)
    for node, (e, t, f, u) in d["bool"].items():
        k = "T" if u == 0 and t == e else "F" if u == 0 and f == e else "X"
        boo.setdefault(node, {}).setdefault(k, []).append(n)
for d in (cmp, boo):
    for v in d.values():
        for k in v:
            v[k].sort()
with gzip.open(sys.argv[2], "wt") as fh:
    json.dump({"cmp": cmp, "bool": boo, "bad": sorted(bad)}, fh, separators=(",", ":"))
print("nodes", len(cmp), len(boo), "bad tests", len(bad), "size", os.path.getsize(sys.argv[2]))
