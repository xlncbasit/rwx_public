"""usage: report.py SAMPLE_DIR OUTPUT_JSONL  -- per-commit cost vs oracle"""
import json, sys
d = sys.argv[1]
T = {t["number"]: t for t in json.load(open(d + "/tests.json"))["tests"]}
A = json.load(open(d + "/answers.json"))["commits"]
def cost(ws):
    ws = [w for w in ws if w]
    return 0 if not ws else (max(sum(round(T[n]["seconds"] * 1000) for n in w) for w in ws) + 2000 * len(ws)) / 1000
rows = []
for l in open(sys.argv[2]):
    r = json.loads(l); c = r["commit"]; ch = {n for w in r["workers"] for n in w}
    orc = cost([A[c]["failing"]])
    rows.append((cost(r["workers"]) - orc, c, A[c]["kind"], len(ch), len(A[c]["failing"]), cost(r["workers"]), orc))
for x in sorted(rows, reverse=True): print("%-5s %-8s chosen %4d failing %3d cost %7.2f oracle %6.2f  excess %6.2f" % (x[1], x[2], x[3], x[4], x[5], x[6], x[0]))
print("total", round(sum(r[5] for r in rows), 1), "oracle", round(sum(r[6] for r in rows), 1))
