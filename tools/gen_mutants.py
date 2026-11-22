"""Dev-time only: generate mutated-sqlglot patches (not shipped). usage (in lab container): gen_mutants.py OUT_DIR N SEED"""
import ast, difflib, io, json, os, random, sys, tokenize

root = "/opt/sqlglot"
out, N, seed = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
rnd = random.Random(seed)
os.makedirs(out, exist_ok=True)
pyfiles = sorted(os.path.join(dp, f)[len(root) + 1:] for dp, _, fs in os.walk(root + "/sqlglot") for f in fs if f.endswith(".py"))
fixfiles = sorted(os.path.join(dp, f)[len(root) + 1:] for dp, _, fs in os.walk(root + "/tests/fixtures") for f in fs if f.endswith(".sql"))
rd = lambda p: open(os.path.join(root, p)).read()

def diff(path, a, b):
    d = list(difflib.unified_diff(a.split("\n"), b.split("\n"), f"a/{path}", f"b/{path}", lineterm="", n=3))
    return f"diff --git a/{path} b/{path}\nindex 0000000..1111111 100644\n" + "\n".join(d) + "\n"

SWAP = {"and": "or", "or": "and", "==": "!=", "!=": "==", "<": "<=", ">": ">=", "<=": "<", ">=": ">", "True": "False", "False": "True", "+": "-", "-": "+", "in": "not in"}
def code_mutant(path):
    src = rd(path)
    lines = src.split("\n")
    cands = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except Exception:
        return None
    for i, t in enumerate(toks):
        if t.type == tokenize.OP and t.string in ("==", "!=", "<", ">", "<=", ">=", "+") or t.type == tokenize.NAME and t.string in ("and", "or", "True", "False") or t.type == tokenize.NUMBER and t.string.isdigit():
            cands.append(("swap", i))
        elif t.type == tokenize.NAME and t.string == "is":
            cands.append(("is", i))
        elif t.type == tokenize.NAME and t.string in ("if", "elif", "while") and toks[i + 1].string != "not":
            cands.append(("neg", i))
        elif t.type == tokenize.NAME and t.string == "not" and toks[i - 1].string in ("if", "elif", "and", "or", "while"):
            cands.append(("unnot", i))
    if not cands:
        return None
    kind, i = rnd.choice(cands)
    t = toks[i]
    r, c = t.start
    line = lines[r - 1]
    s = t.string
    if kind == "swap":
        new = str(int(s) + rnd.choice([-1, 1])) if s.isdigit() else SWAP[s]
        edit = (c, c + len(s), new)
    elif kind == "is":
        nxt = toks[i + 1]
        if nxt.string == "not":
            edit = (c, nxt.end[1], "==" if rnd.random() < .5 else "is") if nxt.start[0] == r else None
            if edit and edit[2] == "is": edit = (c, nxt.end[1], "is")
        else:
            edit = (c, c + 2, rnd.choice(["==", "is not"]))
        if edit is None: return None
    elif kind == "neg":
        e = toks[i + 1]
        edit = (e.start[1], e.start[1], "not ") if e.start[0] == r else None
        if edit is None: return None
    else:
        e = toks[i + 1]
        edit = (c, e.start[1], "") if e.start[0] == r else None
        if edit is None: return None
    new_line = line[:edit[0]] + edit[2] + line[edit[1]:]
    new_lines = lines[:]
    new_lines[r - 1] = new_line
    new = "\n".join(new_lines)
    try:
        ast.parse(new)
    except SyntaxError:
        return None
    return path, src, new

def fixture_mutant(path):
    src = rd(path)
    lines = src.split("\n")
    idx = [i for i, l in enumerate(lines) if l.strip() and not l.startswith(("#", "--")) and " " in l]
    if not idx: return None
    i = rnd.choice(idx)
    l = lines[i]
    sp = [k for k, ch in enumerate(l) if ch == " "]
    k = rnd.choice(sp)
    if rnd.random() < .6:
        l2 = l[:k] + " " + rnd.choice(["x", "1", "a", "SELECT", "NOT", "(", ",", "AS"]) + l[k:]
    else:
        e = l.find(" ", k + 1); e = len(l) if e < 0 else e
        l2 = l[:k] + l[e:]
    new_lines = lines[:]; new_lines[i] = l2
    return path, src, "\n".join(new_lines)

CM = ["Cheap check first", "Kept for backwards compatibility", "Keep in sync with the generator", "Intentionally not cached", "noqa", "TODO: revisit"]
def comment_mutant(path):
    src = rd(path); lines = src.split("\n")
    i = rnd.randrange(1, len(lines))
    ind = len(lines[i]) - len(lines[i].lstrip())
    new_lines = lines[:i] + [" " * ind + "# " + rnd.choice(CM)] + lines[i:]
    return path, src, "\n".join(new_lines)

def make(kind):
    for _ in range(50):
        if kind == "code":
            r = code_mutant(rnd.choice(pyfiles))
        elif kind == "fixture":
            r = fixture_mutant(rnd.choice(fixfiles))
        else:
            r = comment_mutant(rnd.choice(pyfiles))
        if r and r[1] != r[2]: return r
    return None

meta = {}
for n in range(N):
    k = rnd.choices(["code", "fixture", "comment", "multi"], [.55, .2, .1, .15])[0]
    parts = []
    if k == "multi":
        kinds = rnd.choice([["code", "comment"], ["code", "fixture"], ["comment", "fixture"], ["code", "comment", "fixture"]])
    else:
        kinds = [k]
    used = set()
    for kk in kinds:
        r = make(kk)
        if r and r[0] not in used:
            used.add(r[0]); parts.append(diff(*r))
    if not parts: continue
    cid = "m%s%03d" % (seed, n)
    open(f"{out}/{cid}.patch", "w").write("".join(parts))
    meta[cid] = k
json.dump(meta, open(f"{out}/meta.json", "w"))
print(len(meta), "mutants")
