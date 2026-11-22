"""usage: excess.py KIT OUTPUT [N] -- commits ranked by cost above the oracle, with the first patched file"""
import json, re, sys, collections
d = sys.argv[1]
T = {t['number']: t for t in json.load(open(d + '/tests.json'))['tests']}
A = json.load(open(d + '/answers.json'))['commits']
def cost(ws):
    ws = [w for w in ws if w]
    return 0 if not ws else (max(sum(round(T[n]['seconds'] * 1000) for n in w) for w in ws) + 2000 * len(ws)) / 1000
rows = []
for l in open(sys.argv[2]):
    r = json.loads(l); c = r['commit']; ch = {x for w in r['workers'] for x in w}
    p = open(f'{d}/commits/{c}.patch').read()
    fs = re.findall(r'^diff --git a/(\S+)', p, re.M)
    heavy = sum(1 for n in ch if T[n]['seconds'] >= 3)
    rows.append((cost(r['workers']) - cost([A[c]['failing']]), c, len(ch), len(A[c]['failing']), heavy, fs[0].replace('sqlglot/', ''), A[c]['kind']))
rows.sort(reverse=True)
print('total excess', round(sum(r[0] for r in rows), 1), 'of which commits selecting a >=3s test:', round(sum(r[0] for r in rows if r[4]), 1))
for r in rows[:int(sys.argv[3]) if len(sys.argv) > 3 else 30]: print("%6.1f %s chosen %4d failing %3d heavy %d %s %s" % r)
