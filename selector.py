"""Blast Radius selector: for every patch, pick the tests it can break and split them over workers.

Uses only maps built from the pinned sqlglot commit (per-test line coverage, fixture files opened, classes
instantiated, modules loaded); never a table from patches to tests.
"""
import ast, bisect, copy, gzip, json, os, pickle, re, subprocess, sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import patchlib

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DATA_DIR", "/data"))
SRC = Path(os.environ.get("SQLGLOT_DIR", "/opt/sqlglot"))
MAP = Path(os.environ.get("MAP_FILE", HERE / "data" / "map.json.gz"))
REPLAY_MAP = Path(os.environ.get("REPLAY_MAP", HERE / "data" / "replay.json.gz"))
_P = "tests/test_executor.py::TestExecutor::"
REPLAYABLE = {_P + "test_execute_tpcds": ("ds", "exec", "tpcds"), _P + "test_execute_tpch": ("h", "exec", "tpch"),
              _P + "test_optimized_tpch": ("oh", "opt", "tpch")}  # same table as replay.TESTS
REPLAY_BUDGET = float(os.environ.get("REPLAY_BUDGET", "5"))  # most local CPU s of replay spent on one test
REPLAY_SHARE_OF_TEST = float(os.environ.get("REPLAY_SHARE_OF_TEST", "0.25"))  # ... and at most this share of its recorded seconds
REPLAY_SHARE = float(os.environ.get("REPLAY_SHARE", "0.6"))  # replay CPU s allowed per commit of the batch
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
        self.cov = m["_cov"] if "_cov" in m else {f: {int(l): sets[i] for l, i in d.items()} for f, d in m["cov"].items()}
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
        self._sn = None
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
            r = self._snap[key] = self._fork_snapshot(sorted(mods), patched)
        return r

    def _fork_snapshot(self, mods, patched):
        """Import the modules in a forked child of a process that already holds the core of sqlglot (cheaper than a fresh interpreter)."""
        if self._sn is None:
            sys.path.insert(0, str(HERE))
            import snapshot as sn
            sys.path.insert(0, str(SRC))
            try:
                import sqlglot  # noqa: F401
            except Exception:
                sn = False
            self._sn = sn
        patched_mods = {module_of(p) for p in patched}
        if self._sn is False or patched_mods & set(sys.modules):  # a core module is itself patched: needs a clean interpreter
            return self._spawn_snapshot(mods, patched)
        spec = {"mods": mods, "patched": patched}
        rd, wr = os.pipe()
        pid = os.fork()
        if pid == 0:  # child
            try:
                os.close(rd)
                data = json.dumps(self._sn.run(spec)).encode()
            except BaseException:
                data = b'{"ok": false}'
            with os.fdopen(wr, "wb") as fh:
                fh.write(data)
            os._exit(0)
        os.close(wr)
        chunks = []
        while True:
            c = os.read(rd, 1 << 16)
            if not c:
                break
            chunks.append(c)
        os.close(rd)
        os.waitpid(pid, 0)
        try:
            return json.loads(b"".join(chunks))
        except ValueError:
            return self._spawn_snapshot(mods, patched)

    def _spawn_snapshot(self, mods, patched):
        p = subprocess.run([sys.executable, str(HERE / "snapshot.py")], input=json.dumps({"mods": mods, "patched": patched}),
                           capture_output=True, text=True, timeout=90,
                           env={**os.environ, "SQLGLOT_DIR": str(SRC), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0"})
        return json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {"ok": False}

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
_BODIED = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
_norm_old = {}


def _norm(n, eq):
    n = strip_types(n)
    return _SameIdentityEq().visit(n) if eq else n


def _changed_lines(fp):
    """(old line numbers removed, new line numbers added)."""
    rem, add = set(), set()
    for os_, ol, ns, nl, lines in fp.hunks:
        o, nn = os_, ns
        for tag, _ in lines:
            if tag == "-":
                rem.add(o)
                o += 1
            elif tag == "+":
                add.add(nn)
                nn += 1
            else:
                o += 1
                nn += 1
    return rem, add


def same_tree(path, old_text, new_t, fp, eq):
    """Whether old and patched module are the same after normalisation (annotations and docstrings dropped, and
    `is None` written as `== None` if eq). Only statements whose text the patch touches are compared; untouched
    statements are identical text, hence identical trees. new_t is normalised in place where compared."""
    key = (path, eq)
    if key not in _norm_old:
        _norm_old[key] = ast.parse(old_text)  # the pinned file never changes: parse once, normalise nodes when compared
    rem, add = _changed_lines(fp)

    def span(n):
        first = min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", ())])
        return first, n.end_lineno

    def touched(n, lines):
        a, b = span(n)
        return any(a <= l <= b for l in lines)

    def body(n):
        b = n.body
        if b and len(b) > 1 and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) and isinstance(b[0].value.value, str):
            b = b[1:]
        return b

    def same(o, n):
        if type(o) is not type(n):
            return False
        if not isinstance(n, ast.Module) and not touched(o, rem) and not touched(n, add):
            return True
        if isinstance(n, _BODIED):
            nb, ob = body(n), body(o)
            if len(ob) != len(nb):
                return False
            ho, hn = copy.copy(o), copy.copy(n)
            ho.body, hn.body = [], []
            if ast.dump(_norm(ho, eq)) != ast.dump(_norm(hn, eq)):
                return False
            return all(same(x, y) for x, y in zip(ob, nb))
        return ast.dump(_norm(o, eq)) == ast.dump(_norm(n, eq))

    return same(_norm_old[key], new_t)
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
        st["ranges"].append((f, a, b))
        im = cx.imp_mods(f, a, b)  # code that also runs while modules are imported: its effect is baked into their state
        if im:
            st["mods"] |= im
            return SEM
        return True
    # definition-time code at module/class level: defs/classes are judged by their own coverage; everything else
    # (constants, imports, bases, new members) by comparing what the old and the patched module actually define
    if own and isinstance(stmt, FUNCS):  # signature/decorator/default edit: whoever runs the body is affected
        st["header"] = True
        kids = child_stmts(stmt)
        a = kids[0].lineno if kids else stmt.end_lineno
        out.update(cx.cov_range(f, a, stmt.end_lineno))
        st["ranges"].append((f, a, stmt.end_lineno))
        im = cx.imp_mods(f, a, stmt.end_lineno)
        if im:
            st["mods"] |= im
            return SEM
        return True
    return SEM


def sem_tests(cx, f, new_text, extra=(), rep=None):
    """Tests affected by what the patched module defines differently from the old one (None: run everything).
    rep gets "pairs": the (class, attribute) reads that observe the change, or "opaque" if the change is more than that."""
    rep = {} if rep is None else rep
    rep["pairs"], rep["names"], rep["private"] = set(), set(), set()
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
                rep["opaque"] = "import-error"
        for C in set(a["cls"]) | set(b["cls"]):
            sa, sb = a["cls"].get(C), b["cls"].get(C)
            if sa is None or sb is None:
                out |= loaded
                rep["opaque"] = "class-added-removed"
                continue
            for attr in set(sa) | set(sb):
                if sa.get(attr) == sb.get(attr):
                    continue
                if attr in ("<mro>", "<fns>"):
                    out |= loaded if attr == "<mro>" else set(cx.used.get(C, ())) | loaded
                    rep["opaque"] = "mro-or-fns"
                elif attr in sa:
                    changed.append((C, attr))
                    for T in (C, *cx.subs(C)):
                        out |= cx.acc.get(attr, {}).get(T, set())
                        rep["pairs"].add(f"{T}|{attr}")
                        if attr.startswith("_"):
                            rep["private"].add(T)
                            # state derived at class creation, or read while a module was imported, may be cached in objects we cannot see
                            out |= set(cx.used.get(T, ()))
                    if attr not in cx.acc:  # read through something other than a recorded descriptor
                        out |= set(cx.used.get(C, ())) or loaded
                        rep["opaque"] = "unrecorded-attr"
                else:  # new constant: nobody could have read it through a recorded descriptor
                    out |= cx.taint(attr) & (set(cx.used.get(C, ())) or loaded)
                    rep["opaque"] = "new-attr"
        for mod in set(a["glob"]) | set(b["glob"]):
            ga, gb = a["glob"].get(mod, {}), b["glob"].get(mod, {})
            for n in set(ga) | set(gb):
                if ga.get(n) != gb.get(n):
                    out |= cx.taint(n) & set(cx.mods.get(mod, ())) if n in ga else cx.taint(n) & loaded
                    rep["names"].add(n)
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
    if same_tree(path, old_text, new_t, fp, False):
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


EXEC_FIXTURES = {"tests/fixtures/optimizer/tpc-h/tpc-h.sql": "tpch", "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql": "tpcds"}


def executor_unaffected(cx, path, new_text):
    """TestExecutor.setUpClass loads both TPC fixtures into cls.tpch_sqls / cls.tpcds_sqls for every test of the
    class. A test whose own code never mentions that benchmark only needs the file to still load."""
    word = EXEC_FIXTURES.get(path)
    if word is None:
        return set()
    import importlib.util
    spec = importlib.util.spec_from_file_location("_br_helpers", SRC / "tests/helpers.py")
    hp = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(hp)
        statements = hp._filter_comments(new_text).split(";")  # same steps as load_sql_fixture_pairs
        for i in range(0, len(statements), 2):
            if i + 1 < len(statements):
                hp._extract_meta(statements[i].strip())
                statements[i + 1].strip()
    except Exception:
        return set()
    tf = "tests/test_executor.py"
    tree = ast.parse((SRC / tf).read_text())
    out = set()
    for cls in tree.body:
        if isinstance(cls, ast.ClassDef) and cls.name == "TestExecutor":
            for fn in cls.body:
                if isinstance(fn, FUNCS) and fn.name.startswith("test") and word not in ast.get_source_segment((SRC / tf).read_text(), fn):
                    out |= {t["number"] for t in cx.tests if t["id"] == f"{tf}::TestExecutor::{fn.name}"}
    return out


_OPS_RX = re.compile(r"<=|>=|<|>|\band\b|\bor\b")
VMAP = Path(os.environ.get("VMAP_FILE", HERE / "data" / "vmap.json.gz"))
_ORD = {ast.Lt: {"<"}, ast.LtE: {"<", "="}, ast.Gt: {">"}, ast.GtE: {">", "="}}  # relations under which the op is true


def _one_op_change(old_t, new_t):
    """(old op node, new op node, parent in the old tree) if the trees differ only in one comparison operator within
    < <= > >= or one and/or; else None."""
    def bfs(t):  # (node, parent) in breadth-first order; operator nodes are shared singletons, so keep the parent
        out, q = [], [(t, None)]
        while q:
            n, par = q.pop(0)
            out.append((n, par))
            q.extend((c, n) for c in ast.iter_child_nodes(n))
        return out

    pa, pb = bfs(old_t), bfs(new_t)
    if len(pa) != len(pb):
        return None
    diff = None
    for (x, px), (y, _) in zip(pa, pb):
        if type(x) is not type(y):
            if diff is not None:
                return None
            diff = (x, y, px)
            continue
        for (k, v), (k2, w) in zip(ast.iter_fields(x), ast.iter_fields(y)):
            if isinstance(v, list):
                if len(v) != len(w) or any(type(i) is not type(j) and not (isinstance(i, ast.AST) and isinstance(j, ast.AST)) for i, j in zip(v, w)):
                    return None
                if any(not isinstance(i, ast.AST) and i != j for i, j in zip(v, w)):
                    return None
            elif not isinstance(v, ast.AST) and v != w:
                return None
    if diff is None:
        return None
    x, y, n = diff
    if not ({type(x), type(y)} <= set(_ORD) or {type(x), type(y)} == {ast.And, ast.Or}):
        return None
    if isinstance(n, ast.Compare) and len(n.ops) == 1 or isinstance(n, ast.BoolOp):
        return x, y, n
    return None


def value_unaffected(cx, f, old_t, new_t, line, chosen):
    """Tests of `chosen` that provably see no difference from a single operator swap on `line`: on every evaluation of
    the node in the test's own (instrumented, isolated) run, the new operator gave the same result as the old one."""
    fo = next((n for n in reversed(locate(old_t, line)) if isinstance(n, FUNCS)), None)
    fn = next((n for n in reversed(locate(new_t, line)) if isinstance(n, FUNCS)), None)
    if fo is None or fn is None:
        return set()
    ch = _one_op_change(fo, fn)  # the rest of the file is untouched text
    if ch is None:
        return set()
    x, y, node = ch
    if node.lineno != line or node.end_lineno != line:
        return set()
    if not hasattr(cx, "vmap"):
        cx.vmap = None
        if VMAP.exists():
            with gzip.open(VMAP, "rt") as fh:
                cx.vmap = json.load(fh)
            cx.vbad = set(cx.vmap["bad"])
    if cx.vmap is None:
        return set()
    nid = f"{f}:{node.lineno}:{node.col_offset}"
    safe = set()
    if isinstance(node, ast.Compare):
        for codes, ts in cx.vmap["cmp"].get(nid, {}).items():
            if "?" not in codes and all((c in _ORD[type(x)]) == (c in _ORD[type(y)]) for c in codes):
                safe.update(ts)
    else:
        safe.update(cx.vmap["bool"].get(nid, {}).get("T" if isinstance(x, ast.And) else "F", ()))
    out = set()
    for n in chosen & safe:
        path = cx.T[n]["file"]
        if n in cx.vbad:  # its instrumented run did not pass (pool workers and subprocesses are not instrumented)
            continue
        if path not in _pooled:
            p = SRC / path
            _pooled[path] = not p.exists() or bool(_POOL.search(p.read_text()))
        if _pooled[path]:  # work in other processes was not recorded
            continue
        out.add(n)
    dbg("value map", nid, type(x).__name__, "->", type(y).__name__, "drop", len(out), "of", len(chosen))
    return out


def impact_file(cx, fp, info=None):
    """Set of tests, or None meaning 'run everything'. `info` gets the patched source and, when every change is
    plain runtime code inside function bodies, the changed line ranges ("ranges")."""
    info = {} if info is None else info
    path = fp.path
    if fp.binary:
        return set() if path.startswith(INERT_TOP) or path.endswith(INERT_SUFFIX) else None
    if path.startswith("tests/fixtures/"):
        if fp.old is None:
            return set()
        if fp.new is None or fp.old != fp.new or path not in cx.fix:
            return set(cx.fix.get(path, cx.fixfallback))
        new_text = patchlib.apply((SRC / path).read_text(), fp)
        if path in EXEC_FIXTURES:
            info["fixture"] = (EXEC_FIXTURES[path], new_text)
        return set(cx.fix[path]) - executor_unaffected(cx, path, new_text)
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
    info["src"] = new_text
    try:
        old_t = ast.parse(old_text)
    except SyntaxError:
        return None
    try:
        new_t = ast.parse(new_text)
    except SyntaxError:  # the module no longer imports
        return set(cx.mods.get(M, ())) or set(cx.all)
    if same_tree(f, old_text, new_t, fp, True):
        info["ranges"] = []  # comments, docstrings, annotations: nothing runs differently
        return set()
    out, sem, st = set(), False, {"mods": set(), "ranges": []}
    for removed, added, pos in groups(fp):
        rs = [impact_line(cx, f, old_t, l, False, out, st) for l in removed]
        if added and (not removed or added > len(removed)):
            rs.append(impact_line(cx, f, old_t, pos, True, out, st))
        if any(r is False for r in rs):
            return None
        sem = sem or SEM in rs
    info["ranges"] = st["ranges"]
    if st.get("header"):
        info["header"] = True
    if not sem and not st.get("header") and out:
        g = groups(fp)
        if len(g) == 1 and len(g[0][0]) == 1 and g[0][1] == 1:
            l = g[0][0][0]
            a_, b_ = old_text.split("\n")[l - 1], new_text.split("\n")[l - 1]
            if _OPS_RX.sub("@", a_) == _OPS_RX.sub("@", b_):  # cheap pre-check: only an operator differs
                out -= value_unaffected(cx, f, old_t, ast.parse(new_text), l, out)
    if sem:
        rep = {}
        r = sem_tests(cx, f, new_text, st["mods"], rep)
        if r is None:
            return None
        out |= r
        if rep.get("opaque"):
            dbg("opaque-why", f, rep["opaque"])
            del info["ranges"]
            info["sem_opaque"] = True
        else:
            info["sem"] = rep
    return out


def dbg(*a):
    if os.environ.get("TPCDS_DEBUG"):
        print("tpcds:", *a, file=sys.stderr)


def replay_drop(cx, cand, ranges, pairs, patched, soft=(), fixtures=None):
    """The tests of `cand` (replayable TPC tests) that provably still pass: every query whose run reaches a changed
    line or reads a changed class constant gives the same SQL for duckdb and the same executor result as on the
    pinned commit, replayed with the patched sources up to the first checkpoint (parsed tree, optimized tree)
    after which nothing changed is reached."""
    if not hasattr(cx, "rq"):
        load_rq(cx)
    rq = cx.rq
    if rq is None:
        return set()
    hit = set()  # contexts ("ds:q12", "h:e3", "setup:tpch", ...) that observe the change
    pid = {n: j for j, n in enumerate(rq["acc_names"])}
    want_ids = {pid[p] for p in pairs.get("pairs", ()) if p in pid}
    for c, ids in rq["acc"].items():
        if want_ids & ids:
            hit.add(c)
    names = pairs.get("names", set())
    files = {class_file(T) for T in pairs.get("private", ())}
    if None in files:
        return set()
    if names or files:  # a changed module global is read by lines that name it; a class's private derived state by its own code
        rx = re.compile(r"\b(" + "|".join(map(re.escape, sorted(names))) + r")\b") if names else None
        for f, d in rq["by_line"].items():
            src = cx.source(f) if rx else None
            for l, cs in d.items():
                if f in files or rx and l <= len(src) and rx.search(src[l - 1]):
                    hit |= cs
    unmapped = set()  # changed lines some candidate reaches outside the replayed queries (pool, class setup, ...)
    for f, a, b in ranges:
        by = rq["by_line"].get(f, {})
        cv = cx.cov.get(f, {})
        for l in range(a, b + 1):
            cs = by.get(l)
            if cs:
                hit |= cs
            for t in cand:
                if t in cv.get(l, ()) and not any(c.startswith(rq["key"][t] + ":") for c in cs or ()):
                    unmapped.add(t)
    jobs, est, drop = [], 0.6, set()
    for t in sorted(cand - unmapped):
        key, bench = rq["key"][t], rq["bench"][t]
        if f"setup:{bench}" in hit:  # tables built by changed code are also pickled to the pool: not replayed
            continue
        fixtures = fixtures or {}
        mode, cost = {}, 0.0
        for i, row in rq["secs"][t].items():
            if t in soft:  # cannot tell which queries see the change: replay them all to the end
                mode[i], k = None, 0
            elif f"{key}:e{i}" in hit:
                mode[i], k = None, 0
            elif f"{key}:o{i}" in hit:
                mode[i], k = "o", 1
            elif f"{key}:q{i}" in hit:
                mode[i], k = "p", 2
            else:
                continue
            cost += row[k]
        if bench in fixtures:  # the replay also checks every fixture entry that changed (parsed-tree checkpoint)
            cost += sum(row[2] for row in rq["secs"][t].values())
        elif not mode:
            drop.add(t)
            continue
        if cost + 0.3 > REPLAY_SHARE_OF_TEST * cx.ms[t] / 1000 or cost > REPLAY_BUDGET:
            dbg("budget", t, len(mode), round(cost, 1))
            continue
        order = sorted(mode, key=lambda i: ({"p": 0, "o": 1, None: 2}[mode[i]], i))  # cheap checks first: a failure stops early
        jobs.append({"test": rq["id"][t], "n": t, "queries": order, "mode": {str(i): mode[i] for i in mode},
                     "want": {str(i): rq["want"][t][i] for i in (rq["want"][t] if bench in fixtures else mode)}})
        est += cost
    if not jobs:
        return drop
    if cx.replay_left < est:
        dbg("batch budget", round(est, 1))
        return drop
    cx.replay_left -= est
    spec = {"patched": patched, "fixtures": fixtures or {}, "tests": [{k: v for k, v in j.items() if k != "n"} for j in jobs], "cap": est * 2 + 0.5}
    try:
        p = subprocess.run([sys.executable, str(HERE / "replay.py"), "verify"], input=json.dumps(spec), capture_output=True,
                           text=True, timeout=REPLAY_BUDGET * 4 + 10,
                           env={**os.environ, "SQLGLOT_DIR": str(SRC), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0",
                                "REPLAY_ALARM": str(int(REPLAY_BUDGET * 4 + 5))})
        ok = json.loads(p.stdout.strip().splitlines()[-1]).get("ok", {})
    except Exception:
        ok = {}
    for j in jobs:
        dbg("verify", j["test"].rsplit("::", 1)[1], len(j["queries"]), ok.get(j["test"]))
        if ok.get(j["test"]) is True:
            drop.add(j["n"])
    return drop

_POOL = re.compile(r"ProcessPool|multiprocessing|get_context\(")
_pooled = {}


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
    pk = MAP.with_suffix("").with_suffix(".pkl")  # written at image build time: loads faster than the JSON
    if pk.exists():
        with open(pk, "rb") as fh:
            cx = Ctx(tests, pickle.load(fh))
    else:
        with gzip.open(MAP, "rt") as fh:
            cx = Ctx(tests, json.load(fh))
    cx.replayable = {t["number"] for t in tests if t["id"] in REPLAYABLE}
    commits = sorted((DATA / "commits").glob("*.patch"))
    cx.replay_left = REPLAY_SHARE * len(commits)
    cx.T = {t["number"]: t for t in tests}  # keep the whole batch well inside the time limit
    for p in commits:
        run_commit(cx, p)


def class_file(q):
    """sqlglot/x/y.py defining the class 'sqlglot.x.y.Name' (None if unknown)."""
    parts = q.split(".")
    for k in range(len(parts) - 1, 0, -1):
        base = "/".join(parts[:k])
        for f in (base + ".py", base + "/__init__.py"):
            if (SRC / f).exists():
                return f
    return None


class _LineIndex(dict):
    """file -> line -> contexts reaching it, built per file on first use."""

    def __init__(self, lines):
        super().__init__()
        self.lines = lines

    def get(self, f, default=None):
        if f not in self:
            by = {}
            for c, d in self.lines.items():
                for l in d.get(f, ()):
                    by.setdefault(l, set()).add(c)
            self[f] = by
        return dict.get(self, f, default)

    def items(self):
        for f in {f for d in self.lines.values() for f in d}:
            yield f, self.get(f)


def load_rq(cx):
    """The replay map, keyed by test number."""
    cx.rq = None
    if not REPLAY_MAP.exists():
        return
    with gzip.open(REPLAY_MAP, "rt") as fh:
        m = json.load(fh)
    num = {t["id"]: t["number"] for t in cx.tests}
    rq = {"by_line": _LineIndex(m["lines"]), "acc": {c: set(v) for c, v in m["acc"].items()}, "acc_names": m["acc_names"],
          "key": {}, "bench": {}, "id": {}, "secs": {}, "want": {}}
    for tid, (key, kind, bench) in REPLAYABLE.items():
        if tid in num and tid in m["want"]:
            n = num[tid]
            rq["key"][n], rq["bench"][n], rq["id"][n] = key, bench, tid
            rq["secs"][n] = {int(i): v for i, v in m["secs"][tid].items()}
            rq["want"][n] = {int(i): v for i, v in m["want"][tid].items()}
    cx.rq = rq

def run_commit(cx, p):
    chosen = set()
    try:
        opaque, ranges, pairs, patched = set(), [], {}, {}
        soft, fixtures = set(), {}  # tests a change reaches in ways only a replay of every query can judge; edited TPC fixtures
        for fp in patchlib.parse(p.read_text()):
            info = {}
            r = impact_file(cx, fp, info)
            if r is None:
                chosen = set(cx.all)
                opaque = chosen
                break
            chosen |= r
            if "src" in info:
                patched[fp.path] = info["src"]
            if "ranges" in info:
                ranges += info["ranges"]
                for k, v in info.get("sem", {}).items():
                    if k in ("pairs", "names", "private"):
                        pairs.setdefault(k, set()).update(v)
            elif "fixture" in info:
                fixtures[info["fixture"][0]] = info["fixture"][1]
                opaque |= r - cx.replayable
            elif info.get("sem_opaque") and not fp.path.startswith("sqlglot/executor/"):
                soft |= r
            else:
                opaque |= r
        cand = (cx.replayable & chosen) - opaque
        if cand:
            dbg("commit", p.stem)
            chosen -= replay_drop(cx, cand, ranges, pairs, patched, soft, fixtures)
    except Exception as e:  # never miss because of a bug: run everything
        print(f"{p.stem}: {e!r}", file=sys.stderr)
        chosen = set(cx.all)
    print(json.dumps({"commit": p.stem, "workers": split(cx, chosen)}), flush=True)


if __name__ == "__main__":
    main()
