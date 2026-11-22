"""Blast Radius selector: for every patch, pick the tests it can break and split them over workers.

Uses only maps built from the pinned sqlglot commit (per-test line coverage, fixture files opened, classes
instantiated, modules loaded); never a table from patches to tests.
"""
import ast, bisect, gzip, json, os, re, subprocess, sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import patchlib

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DATA_DIR", "/data"))
SRC = Path(os.environ.get("SQLGLOT_DIR", "/opt/sqlglot"))
MAP = Path(os.environ.get("MAP_FILE", HERE / "data" / "map.json.gz"))
WORKER_MS, MAXW = 2000, 8


class Ctx:
    def __init__(self, tests, m):
        self.tests = tests
        self.ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
        self.all = set(self.ms)
        self.by_file = {}
        for t in tests:
            self.by_file.setdefault(t["file"], set()).add(t["number"])
        sets = m["sets"]
        self.cov = {f: {int(l): sets[i] for l, i in d.items()} for f, d in m["cov"].items()}
        self.fix = {f: set(v) for f, v in m["fix"].items()}
        self.fixfallback = set(m["fixfallback"])
        self.used = {}  # class qualname -> tests that instantiate it (or a subclass)
        self.mods = {}  # module -> tests that loaded it
        for n, v in m["used"].items():
            for q in v:
                self.used.setdefault(q, set()).add(int(n))
        for n, v in m["mods"].items():
            for q in v:
                self.mods.setdefault(q, set()).add(int(n))
        self.eager = set(m["eager"])  # files with import-time lines outside any test
        self.acc = {}  # attribute -> {receiver class: tests that read it}
        for k, sid in m.get("acc", {}).items():
            T, a = k.rsplit("|", 1)
            self.acc.setdefault(a, {})[T] = set(sets[sid])
        self.mro = m.get("mro", {})
        self.acci = {}  # attribute -> {receiver class: modules that read it while being imported}
        for k, sid in m.get("acci", {}).items():
            T, a = k.rsplit("|", 1)
            self.acci.setdefault(a, {})[T] = {m_ for i in m["impsets"][sid] for m_ in [m["impmods"][i]]}
        self.dep = m.get("dep", {})  # module -> modules defining subclasses of its classes
        self._sub = None
        self._snap = {}
        self.imp = {f: {int(l): i for l, i in d.items()} for f, d in m.get("imp", {}).items()}  # file -> line -> id of the set of modules whose import executed that line
        self.impsets = m.get("impsets", [])
        self.impmods = m.get("impmods", [])
        self._src = {}
        self._idx = None

    def source(self, f):
        s = self._src.get(f)
        if s is None:
            p = SRC / f
            s = self._src[f] = p.read_text().split("\n") if p.exists() else []
        return s

    def taint(self, name):
        """Tests that ran any covered line mentioning `name` as a word (source or string)."""
        rx = re.compile(r"\b" + re.escape(name) + r"\b")
        out = set()
        for f, d in self.cov.items():
            src = self.source(f)
            for l, s in d.items():
                if l <= len(src) and rx.search(src[l - 1]):
                    out.update(s)
        return out

    def subs(self, C):
        """Classes (module.qualname) whose MRO contains C."""
        if self._sub is None:
            self._sub = {}
            for T, mro in self.mro.items():
                for c in mro:
                    self._sub.setdefault(c, []).append(T)
        return self._sub.get(C, ())

    def snapshot(self, mods, patched):
        key = (tuple(sorted(mods)), tuple(sorted((k, hash(v)) for k, v in patched.items())))
        r = self._snap.get(key)
        if r is None:
            p = subprocess.run([sys.executable, str(HERE / "snapshot.py")], input=json.dumps({"mods": sorted(mods), "patched": patched}),
                               capture_output=True, text=True, timeout=90, env={**os.environ, "SQLGLOT_DIR": str(SRC), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0"})
            r = self._snap[key] = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {"ok": False}
        return r

    def cov_range(self, f, a, b):
        """Tests that ran lines a..b (outside module imports)."""
        out = set()
        d = self.cov.get(f, {})
        for l in range(a, b + 1):
            s = d.get(l)
            if s:
                out.update(s)
        return out

    def imp_mods(self, f, a, b):
        """Modules whose import ran any of lines a..b."""
        d = self.imp.get(f, {})
        ids = {d[l] for l in range(a, b + 1) if l in d}
        return {self.impmods[i] for s in ids for i in self.impsets[s]}


def module_of(path):
    p = path[:-3].replace("/", ".")
    return p[:-9] if p.endswith(".__init__") else p


def strip_types(tree):
    """Annotations are not evaluated under `from __future__ import annotations`, and no counted test reads docstrings
    (the doctest module is not collected): drop both before comparing."""
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body:
            b = n.body[0]
            if isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant) and isinstance(b.value.value, str) and len(n.body) > 1:
                n.body = n.body[1:]
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            n.returns = None
            a = n.args
            for x in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]:
                if x is not None:
                    x.annotation = None
        elif isinstance(n, ast.AnnAssign):
            n.annotation = ast.Constant(None)
    return tree


def _enum_like(n):
    """`Name.MEMBER` / `a.b.Name.MEMBER`: an enum member, so identity and equality agree."""
    if isinstance(n, ast.Attribute) and len(n.attr) > 1 and n.attr.isupper():
        v = n.value
        last = v.id if isinstance(v, ast.Name) else v.attr if isinstance(v, ast.Attribute) else ""
        return last[:1].isupper()
    return False


class _SameIdentityEq(ast.NodeTransformer):
    """`x is None` and `x == None` (or enum members) behave the same: write both as `==`."""
    def visit_Compare(self, n):
        self.generic_visit(n)
        if len(n.ops) == 1 and isinstance(n.ops[0], (ast.Is, ast.IsNot)):
            for side in (n.left, n.comparators[0]):
                if (isinstance(side, ast.Constant) and side.value is None) or _enum_like(side):
                    n.ops = [ast.Eq() if isinstance(n.ops[0], ast.Is) else ast.NotEq()]
                    break
        return n


FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
SEM = "sem"  # change at definition time: decide by diffing the module state


def has_body(n):
    return isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If, ast.For, ast.AsyncFor, ast.While,
                          ast.With, ast.AsyncWith, ast.Try, ast.Match)) or type(n).__name__ == "TryStar"


def child_stmts(n):
    out = []
    for f in ("body", "orelse", "finalbody", "handlers", "cases"):
        for c in getattr(n, f, []) or []:
            if isinstance(c, ast.stmt):
                out.append(c)
            elif hasattr(c, "body"):  # ExceptHandler / match_case: not statements but hold bodies
                out.append(c)
    return out


def start_of(n):
    d = getattr(n, "decorator_list", None)
    return min([n.lineno] + [x.lineno for x in d]) if d else n.lineno


def locate(tree, line):
    """Chain of nodes (module .. innermost statement) whose own region or body contains `line`."""
    chain, node = [tree], tree
    while True:
        nxt = None
        for c in child_stmts(node):
            if start_of(c) <= line <= c.end_lineno:
                nxt = c
                break
        if nxt is None:
            return chain
        chain.append(nxt)
        node = nxt


def header_end(n):
    kids = child_stmts(n)
    return kids[0].lineno - 1 if kids else n.end_lineno


def bound_names(n):
    names = set()
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(n.name)
    elif isinstance(n, ast.Assign):
        for t in n.targets:
            names.update(x.id for x in ast.walk(t) if isinstance(x, ast.Name))
    elif isinstance(n, (ast.AnnAssign, ast.AugAssign)):
        names.update(x.id for x in ast.walk(n.target) if isinstance(x, ast.Name))
    elif isinstance(n, (ast.Import, ast.ImportFrom)):
        names.update((a.asname or a.name).split(".")[0] for a in n.names)
    return names


def is_type_checking(n):
    return isinstance(n, ast.If) and "TYPE_CHECKING" in ast.dump(n.test)


def impact_line(cx, f, tree, line, insert, out, st):
    """Add to `out` the tests that can observe a change at old line `line` (insert: new code goes before it).
    Returns False if the change cannot be mapped and everything must run."""
    chain = locate(tree, line)
    for n in chain:  # inside the `if TYPE_CHECKING:` body (not its test or an else branch): never runs
        if is_type_checking(n) and line > n.test.end_lineno and n.body[0].lineno <= line <= n.body[-1].end_lineno:
            return True
    stmt = chain[-1]
    if insert:  # new code placed inside `stmt`'s body (or at module level): judged by the enclosing scope
        scope = stmt
        own = False
    else:
        scope = stmt
        own = True
    # innermost enclosing function whose *body* holds the statement
    func = None
    for i in range(len(chain) - 1, -1, -1):
        n = chain[i]
        if isinstance(n, FUNCS):
            body_start = child_stmts(n)[0].lineno if child_stmts(n) else n.end_lineno
            if line >= body_start or not own and line > n.lineno:
                func = n
                break
    if func is not None:
        a, b = (start_of(func), func.end_lineno) if insert else (start_of(stmt), header_end(stmt))
        out.update(cx.cov_range(f, a, b))
        im = cx.imp_mods(f, a, b)  # code that also runs while modules are imported: its effect is baked into their state
        if im:
            st["mods"] |= im
            return SEM
        return True
    # definition-time code at module/class level: defs/classes are judged by their own coverage; everything else
    # (constants, imports, bases, new members) by comparing what the old and the patched module actually define
    if own and isinstance(stmt, FUNCS):  # signature/decorator/default edit: whoever runs the body is affected
        kids = child_stmts(stmt)
        a = kids[0].lineno if kids else stmt.end_lineno
        out.update(cx.cov_range(f, a, stmt.end_lineno))
        im = cx.imp_mods(f, a, stmt.end_lineno)
        if im:
            st["mods"] |= im
            return SEM
        return True
    return SEM


def sem_tests(cx, f, new_text, extra=()):
    """Tests affected by what the patched module defines differently from the old one (None: run everything)."""
    M = module_of(f)
    mods = {M} | set(cx.dep.get(M, ())) | set(extra)
    for _ in range(4):
        if len(mods) > 150:
            return None
        a = cx.snapshot(mods, {})
        b = cx.snapshot(mods, {f: new_text})
        if not a.get("ok") or not b.get("ok"):
            return None
        out, changed = set(), []
        loaded = set(cx.mods.get(M, ()))
        for m, err in b["errs"].items():  # no longer imports: every test that loads it breaks
            if m not in a["errs"]:
                out |= set(cx.mods.get(m, ())) or set(cx.all)
        for C in set(a["cls"]) | set(b["cls"]):
            sa, sb = a["cls"].get(C), b["cls"].get(C)
            if sa is None or sb is None:
                out |= loaded
                continue
            for attr in set(sa) | set(sb):
                if sa.get(attr) == sb.get(attr):
                    continue
                if attr in ("<mro>", "<fns>"):
                    out |= loaded if attr == "<mro>" else set(cx.used.get(C, ())) | loaded
                elif attr in sa:
                    changed.append((C, attr))
                    for T in (C, *cx.subs(C)):
                        out |= cx.acc.get(attr, {}).get(T, set())
                        if cx.acci.get(attr, {}).get(T) or attr.startswith("_"):
                            # state derived at class creation, or read while a module was imported, may be cached in objects we cannot see
                            out |= set(cx.used.get(T, ()))
                    if attr not in cx.acc:  # read through something other than a recorded descriptor
                        out |= set(cx.used.get(C, ())) or loaded
                else:  # new constant: nobody could have read it through a recorded descriptor
                    out |= cx.taint(attr) & (set(cx.used.get(C, ())) or loaded)
        for mod in set(a["glob"]) | set(b["glob"]):
            ga, gb = a["glob"].get(mod, {}), b["glob"].get(mod, {})
            for n in set(ga) | set(gb):
                if ga.get(n) != gb.get(n):
                    out |= cx.taint(n) & set(cx.mods.get(mod, ())) if n in ga else cx.taint(n) & loaded
        # modules that read a changed constant while being imported hold derived state we have not compared yet
        more = {m for C, attr in changed for T in (C, *cx.subs(C)) for m in cx.acci.get(attr, {}).get(T, ())} - mods
        if not more:
            return out
        mods |= more
    return None


def groups(fp):
    """(removed old lines, number of added lines, old position before which additions go) per contiguous change."""
    res = []
    for os_, ol, ns, nl, lines in fp.hunks:
        o = os_
        cur = None
        for tag, text in lines:
            if tag == " ":
                cur = None
                o += 1
            elif tag in "-+":
                if cur is None:
                    cur = [[], 0, o]
                    res.append(cur)
                if tag == "-":
                    cur[0].append(o)
                    o += 1
                else:
                    cur[1] += 1
    return res


INERT_SUFFIX = (".md", ".rst", ".txt", ".png", ".svg", ".jpg", ".gif", ".ico", ".lock", ".yml", ".yaml", ".cfg_", ".typed")
INERT_TOP = ("docs/", "sqlglotrs/", ".github/", "posts/", "benchmarks/", "LICENSE", "CHANGELOG", "README", "Makefile", ".gitignore", ".pre-commit", "MANIFEST")
RUNNER_FILES = ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini", "conftest.py", "tests/conftest.py", "requirements")


def tests_in(cx, path):
    return set(cx.by_file.get(path, ()))


def importers(cx, path):
    """Tests of the test files that import the test module `path` (e.g. the shared Validator helper)."""
    mod = path[:-3].replace("/", ".")
    pkg, _, leaf = mod.rpartition(".")
    rx = re.compile(r"^\s*(?:from\s+%s\s+import\s+(?:.*\b%s\b)|from\s+%s\s+import|import\s+%s\b)" % (re.escape(pkg), re.escape(leaf), re.escape(mod), re.escape(mod)), re.M)
    out = set()
    for f in cx.by_file:
        p = SRC / f
        if f != path and p.exists() and rx.search(p.read_text()):
            out |= tests_in(cx, f)
    return out


def impact_test_file(cx, fp):
    """Edits to files under tests/: the changed test functions, or the whole file (and its importers) for anything else."""
    path = fp.path
    whole = tests_in(cx, path) | importers(cx, path)
    if fp.old is None:  # a new file: no numbered test lives there and nothing imports it yet
        return set()
    if fp.new is None or fp.old != fp.new:
        return whole
    old_text = (SRC / path).read_text()
    new_text = patchlib.apply(old_text, fp)
    try:
        old_t = ast.parse(old_text)
        new_t = ast.parse(new_text)
    except SyntaxError:
        return whole
    if ast.dump(strip_types(ast.parse(old_text))) == ast.dump(strip_types(new_t)):
        return set()
    out = set()
    pending = [(l, False) for removed, added, pos in groups(fp) for l in removed]
    pending += [(pos, True) for removed, added, pos in groups(fp) if added and (not removed or added > len(removed))]
    for line, insert in pending:
        chain = locate(old_t, line)
        func = next((n for n in reversed(chain) if isinstance(n, FUNCS)), None)
        klass = next((n for n in reversed(chain) if isinstance(n, ast.ClassDef)), None)
        kids = child_stmts(func) if func is not None else []
        if func is None or not func.name.startswith("test") or not kids or line < kids[0].lineno or (insert and line > func.end_lineno):
            return whole
        suffix = (f"::{klass.name}" if klass else "") + f"::{func.name}"
        hit = {t["number"] for t in cx.tests if t["id"] == path + suffix}
        if not hit:
            return whole
        out |= hit
    return out


def impact_file(cx, fp):
    """Set of tests, or None meaning 'run everything'."""
    path = fp.path
    if fp.binary:
        return set() if path.startswith(INERT_TOP) or path.endswith(INERT_SUFFIX) else None
    if path.startswith("tests/fixtures/"):
        if fp.old is None:
            return set()
        if fp.new is None or fp.old != fp.new or path not in cx.fix:
            return set(cx.fix.get(path, cx.fixfallback))
        return set(cx.fix[path])
    if path.endswith(RUNNER_FILES) or path.startswith(RUNNER_FILES):
        return None
    if path.startswith("tests/"):
        return impact_test_file(cx, fp) if path.endswith(".py") else (set() if path.endswith(INERT_SUFFIX) else None)
    if not path.startswith("sqlglot/"):
        return set() if path.startswith(INERT_TOP) or path.endswith(INERT_SUFFIX) else None
    if not path.endswith(".py"):
        return set() if path.endswith(INERT_SUFFIX) else None
    f = path
    M = module_of(f)
    if fp.old is None:  # a new module that nothing imports yet (an edit that imports it is analysed on its own)
        return set()
    if fp.new is None or fp.old != fp.new:  # deleted or moved: whoever loaded it breaks
        return set(cx.mods.get(module_of(fp.old), ())) or set(cx.all)
    old_text = (SRC / f).read_text()
    new_text = patchlib.apply(old_text, fp)
    try:
        old_t = ast.parse(old_text)
    except SyntaxError:
        return None
    try:
        new_t = ast.parse(new_text)
    except SyntaxError:  # the module no longer imports
        return set(cx.mods.get(M, ())) or set(cx.all)
    if ast.dump(_SameIdentityEq().visit(strip_types(ast.parse(old_text)))) == ast.dump(_SameIdentityEq().visit(strip_types(new_t))):
        return set()
    out, sem, st = set(), False, {"mods": set()}
    for removed, added, pos in groups(fp):
        rs = [impact_line(cx, f, old_t, l, False, out, st) for l in removed]
        if added and (not removed or added > len(removed)):
            rs.append(impact_line(cx, f, old_t, pos, True, out, st))
        if any(r is False for r in rs):
            return None
        sem = sem or SEM in rs
    if sem:
        r = sem_tests(cx, f, new_text, st["mods"])
        if r is None:
            return None
        out |= r
    return out


def split(cx, chosen):
    if not chosen:
        return []
    ts = sorted(chosen, key=lambda n: -cx.ms[n])
    best = None
    for k in range(1, MAXW + 1):
        loads, ws = [0] * k, [[] for _ in range(k)]
        for n in ts:
            i = loads.index(min(loads))
            ws[i].append(n)
            loads[i] += cx.ms[n]
        cost = max(loads) + WORKER_MS * sum(1 for w in ws if w)
        if best is None or cost < best[0]:
            best = (cost, [w for w in ws if w])
    return best[1]


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    with gzip.open(MAP, "rt") as fh:
        cx = Ctx(tests, json.load(fh))
    for p in sorted((DATA / "commits").glob("*.patch")):
        chosen = set()
        try:
            for fp in patchlib.parse(p.read_text()):
                r = impact_file(cx, fp)
                if r is None:
                    chosen = set(cx.all)
                    break
                chosen |= r
        except Exception as e:  # never miss because of a bug: run everything
            print(f"{p.stem}: {e!r}", file=sys.stderr)
            chosen = set(cx.all)
        print(json.dumps({"commit": p.stem, "workers": split(cx, chosen)}), flush=True)


if __name__ == "__main__":
    main()
