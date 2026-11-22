"""usage: misses.py KIT OUTPUT -- list commits where a failing test was not selected"""
import json, sys
d = sys.argv[1]
A = json.load(open(d + "/answers.json"))["commits"]
n = 0
for l in open(sys.argv[2]):
    r = json.loads(l); c = r["commit"]; ch = {x for w in r["workers"] for x in w}
    miss = set(A[c]["failing"]) - ch
    if miss:
        n += 1
        print(c, A[c]["kind"], "chosen", len(ch), "failing", len(A[c]["failing"]), "missed", len(miss), sorted(miss)[:6])
print("commits with misses:", n)
