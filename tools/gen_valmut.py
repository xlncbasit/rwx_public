"""Dev-time: mutants of the shapes the value map judges (ordering-operator swaps, and/or swaps in boolean context)
on lines that light, non-pooled tests run. usage (lab container): gen_valmut.py MAP.json.gz TESTS.json OUT_DIR N SEED
Writes OUT_DIR/commits/<id>.patch and OUT_DIR/targets.json {id: [test ids that run the line]}."""
import ast, difflib, gzip, json, os, random, sys

root = "/opt/sqlglot"
mp, tj, out, N, seed = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
rnd = random.Random(seed)
m = json.load(gzip.open(mp, "rt"))
tests = {t["number"]: t for t in json.load(open(tj))["tests"]}
POOLED = ("tests/test_optimizer.py", "tests/test_executor.py")
SW = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}


def diff(path, a, b):
    d = list(difflib.unified_diff(a.split("\n"), b.split("\n"), f"a/{path}", f"b/{path}", lineterm="", n=3))
    return f"diff --git a/{path} b/{path}\nindex 0000000..1111111 100644\n" + "\n".join(d) + "\n"


cands = []
for dp, _, fs in os.walk(root + "/sqlglot"):
    for f in fs:
        if not f.endswith(".py"):
            continue
        rel = os.path.join(dp, f)[len(root) + 1:]
        cov = m["cov"].get(rel, {})
        src = open(os.path.join(root, rel)).read()
        for fn in ast.walk(ast.parse(src)):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or not fn.body:
                continue
            for n in ast.walk(fn):
                if isinstance(n, ast.If):
                    n = n.test
                    if not isinstance(n, ast.BoolOp):
                        continue
                if getattr(n, "lineno", 0) < fn.body[0].lineno or getattr(n, "lineno", 0) != getattr(n, "end_lineno", -1):
                    continue
                sid = cov.get(str(n.lineno))
                if sid is None:
                    continue
                ts = [x for x in m["sets"][sid] if not tests[x]["file"].startswith(POOLED)]
                if not ts:
                    continue
                if isinstance(n, ast.Compare) and len(n.ops) == 1 and type(n.ops[0]) in SW:
                    cands.append(("cmp", rel, n, ts))
                elif isinstance(n, ast.BoolOp) and len(n.values) == 2:
                    cands.append(("bool", rel, n, ts))
if len(sys.argv) > 6:  # only this kind ("cmp" or "bool")
    cands = [c for c in cands if c[0] == sys.argv[6]]
rnd.shuffle(cands)
os.makedirs(out + "/commits", exist_ok=True)
targets, k = {}, 0
for kind, rel, n, ts in cands:
    if k >= N:
        break
    src = open(os.path.join(root, rel)).read()
    lines = src.split("\n")
    line = lines[n.lineno - 1]
    if kind == "cmp":
        a, b = n.left.end_col_offset, n.comparators[0].col_offset
        old = SW[type(n.ops[0])]
        new = rnd.choice([x for x in SW.values() if x != old])
    else:
        a, b = n.values[0].end_col_offset, n.values[1].col_offset
        old = "and" if isinstance(n.op, ast.And) else "or"
        new = "or" if old == "and" else "and"
    seg = line[a:b]
    if seg.strip() != old:
        continue
    lines[n.lineno - 1] = line[:a] + seg.replace(old, new) + line[b:]
    cid = "v%s%03d" % (seed, k)
    open(f"{out}/commits/{cid}.patch", "w").write(diff(rel, src, "\n".join(lines)))
    targets[cid] = sorted({tests[x]["id"] for x in ts})
    k += 1
json.dump(targets, open(f"{out}/targets.json", "w"))
print(k, "mutants of", len(cands), "candidates")
