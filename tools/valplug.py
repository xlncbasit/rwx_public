"""pytest plugin for the value map: instruments sqlglot at import and records, per node, what the test saw.

- `L op R` with one op in < <= > >= : the 3-way relation of the operands on every evaluation ("<", "=", ">", "?").
- `and` / `or` whose value is only used for its truthiness: per evaluation whether all operands were truthy / falsy.
Node ids are "<path>:<lineno>:<col_offset>" of the original node. Written to $VAL_OUT at the end of the session.
"""
import ast, builtins, json, operator, os

ROOT = os.environ.get("VAL_ROOT", "/opt/sqlglot")
OPS = {"Lt": operator.lt, "LtE": operator.le, "Gt": operator.gt, "GtE": operator.ge}
CMP = {}  # id -> set of relation codes
BOOL = {}  # id -> [entered, last truthy, last falsy, unknown]


def _rel(a, b):
    try:
        lt, eq, gt = a < b, a == b, a > b
    except Exception:
        return "?"
    if not all(type(x) is bool for x in (lt, eq, gt)) or lt + eq + gt != 1:
        return "?"
    return "<" if lt else "=" if eq else ">"


def _brv_cmp(i, a, b, op):
    try:
        r = OPS[op](a, b)
    except BaseException:
        CMP.setdefault(i, set()).add("?")
        raise
    CMP.setdefault(i, set()).add(_rel(a, b) if type(r) is bool else "?")
    return r


def _brv_first(i, v):
    BOOL.setdefault(i, [0, 0, 0, 0])[0] += 1
    return v


def _brv_last(i, v):
    c = BOOL.setdefault(i, [0, 0, 0, 0])
    try:
        c[1 if bool(v) else 2] += 1
    except Exception:
        c[3] += 1
    return v


builtins._brv_cmp, builtins._brv_first, builtins._brv_last = _brv_cmp, _brv_first, _brv_last


def _call(name, *args):
    return ast.Call(func=ast.Name(id=name, ctx=ast.Load()), args=list(args), keywords=[])


class Inst(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def nid(self, n):
        return ast.Constant(f"{self.rel}:{n.lineno}:{n.col_offset}")

    def visit_Compare(self, n):
        self.generic_visit(n)
        if len(n.ops) == 1 and type(n.ops[0]).__name__ in OPS:
            return ast.copy_location(_call("_brv_cmp", self.nid(n), n.left, n.comparators[0], ast.Constant(type(n.ops[0]).__name__)), n)
        return n

    def boolctx(self, e):
        """e is used only for its truthiness: record and/or there (operands are judged by truthiness too)."""
        if isinstance(e, ast.BoolOp):
            vals = [self.boolctx(v) for v in e.values]
            vals[0] = ast.copy_location(_call("_brv_first", self.nid(e), vals[0]), vals[0])
            vals[-1] = ast.copy_location(_call("_brv_last", self.nid(e), vals[-1]), vals[-1])
            e.values = vals
            return e
        if isinstance(e, ast.UnaryOp) and isinstance(e.op, ast.Not):
            e.operand = self.boolctx(e.operand)
            return e
        return self.visit(e)

    def visit_UnaryOp(self, n):
        return self.boolctx(n) if isinstance(n.op, ast.Not) else self.generic_visit(n)

    def _stmts(self, body):
        out = []
        for s_ in body:
            r = self.visit(s_)
            out.extend(r if isinstance(r, list) else [r] if r is not None else [])
        return out

    def visit_If(self, n):
        n.test = self.boolctx(n.test)
        n.body, n.orelse = self._stmts(n.body), self._stmts(n.orelse)
        return n

    visit_While = visit_If

    def visit_IfExp(self, n):
        n.test = self.boolctx(n.test)
        n.body, n.orelse = self.visit(n.body), self.visit(n.orelse)
        return n

    def visit_Assert(self, n):
        n.test = self.boolctx(n.test)
        if n.msg is not None:
            n.msg = self.visit(n.msg)
        return n

    def visit_comprehension(self, n):
        n.target, n.iter = self.visit(n.target), self.visit(n.iter)
        n.ifs = [self.boolctx(x) for x in n.ifs]
        return n


def transform(src, path):
    rel = os.path.relpath(path, ROOT)
    tree = Inst(rel).visit(ast.parse(src, path))
    return ast.fix_missing_locations(tree)


def _install():
    import importlib._bootstrap_external as be

    orig = be.SourceLoader.source_to_code
    pref = os.path.join(ROOT, "sqlglot") + os.sep

    def source_to_code(self, data, path, *, _optimize=-1):
        if isinstance(path, str) and path.startswith(pref):
            return compile(transform(data.decode("utf-8") if isinstance(data, bytes) else data, path), path, "exec", dont_inherit=True, optimize=_optimize)
        return orig(self, data, path, _optimize=_optimize)

    be.SourceLoader.source_to_code = source_to_code


_install()


def pytest_sessionfinish(session, exitstatus):
    out = os.environ.get("VAL_OUT")
    if out:
        with open(out, "w") as fh:
            json.dump({"cmp": {k: sorted(v) for k, v in CMP.items()}, "bool": BOOL, "exit": int(exitstatus)}, fh)
