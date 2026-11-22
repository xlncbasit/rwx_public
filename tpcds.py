"""Replays single queries of tests/test_executor.py::TestExecutor::test_execute_tpcds.

The test passes on the pinned commit. For each query it parses the SQL, pickles the tree to a worker, executes it
there and compares the result with duckdb running transpile(sql, write="duckdb"). If, with patched sources, the
duckdb SQL and the executor's result of every query that reaches a changed line are identical to the pinned
commit's, the test still passes.

  python tpcds.py tables OUT.pkl         (docker build) the CSV tables as plain tuples, like the test's setUpClass
  python tpcds.py verify < spec.json      (run time) {"patched": {path: src}, "queries": [i, ...], "want": {i: [t, r]}}
"""
import ast, csv, gzip, hashlib, json, os, pickle, sys

ROOT = os.environ.get("SQLGLOT_DIR", "/opt/sqlglot")
DIR = os.path.join(ROOT, "tests/fixtures/optimizer/tpc-ds/")
TABLES = os.environ.get("TPCDS_TABLES", "/app/data/tpcds_tables.pkl")


def schema():
    sys.path.insert(0, ROOT)
    from tests.helpers import TPCDS_SCHEMA  # plain dict literal

    return TPCDS_SCHEMA


def queries():
    from tests.helpers import load_sql_fixture_pairs

    return list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))


def read_tables(sch):
    """Same parsing as TestExecutor.setUpClass (types from the first row via literal_eval)."""
    out = {}
    for table, columns in sch.items():
        with gzip.open(f"{DIR}{table}.csv.gz", "rt", newline="") as fh:
            reader = csv.reader(fh, delimiter="|")
            next(reader)
            rows, ctypes = [], []
            for row in reader:
                if not ctypes:
                    for v in row:
                        try:
                            ctypes.append(type(ast.literal_eval(v)))
                        except (ValueError, SyntaxError):
                            ctypes.append(str)
                rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
        out[table] = (columns, rows)
    return out


def h(s):
    return hashlib.sha1(s.encode()).hexdigest()[:16]


def canon(v):
    """Strict structural dump of an expression tree (args, types, comments, meta)."""
    from sqlglot.expressions import Expr

    if isinstance(v, Expr):
        return (type(v).__name__, tuple((k, canon(x)) for k, x in sorted(v.args.items())),
                canon(v.type) if v.type is not v else None, canon(v.comments), repr(sorted(v.meta.items())) if v.meta else "")
    if isinstance(v, (list, tuple)):
        return tuple(canon(x) for x in v)
    return repr(v)


class Same(Exception):
    pass


def tree_hash(e):
    return h(repr(canon(e)))


def run_query(meta, sql, tables, sch, mode=None, want=None, phase=None, rec=None):
    """Hashes (duckdb SQL, executor result, parsed tree, optimized tree) of one query exactly as the test computes them.
    Phases: "q" parse_one and transpile in the test process, "e" pickling to the pool, find_tables and execute
    (minus the optimizer), "o" the optimizer. mode "p": an identical parsed tree skips "e"/"o" (they are unchanged);
    mode "o": an identical optimized tree skips the rest of "e". rec: compute the tree hashes (map build)."""
    from sqlglot import find_tables, parse_one, transpile
    import sqlglot.executor as E

    phase = phase or (lambda name: None)
    phase("q")
    parsed = parse_one(sql)
    ph = tree_hash(parsed) if rec or mode == "p" else None
    res = oh = None
    if meta.get("execute"):
        if mode == "p" and ph == want[2]:
            res = "same"
        else:
            phase("e")
            expr = pickle.loads(pickle.dumps(parsed))  # pickled to the pool
            used = {t.name: tables[t.name] for t in find_tables(expr)}
            opt = E.optimize

            def hooked(*a, **k):
                nonlocal oh
                phase("o")
                r = opt(*a, **k)
                phase("e")
                if rec or mode == "o":
                    oh = tree_hash(r)
                    if mode == "o" and oh == want[3]:
                        raise Same
                return r

            E.optimize = hooked
            try:
                table = pickle.loads(pickle.dumps(E.execute(expr, schema=sch, tables=used)))
                res = h(repr((table.columns, table.rows)))
            except Same:
                res = "same"
            finally:
                E.optimize = opt
            phase("q")
    return h(transpile(sql, write="duckdb")[0]), res, ph, oh


def make_tables(plain):
    from sqlglot.executor.table import Table

    return {k: Table(columns=c, rows=r) for k, (c, r) in plain.items()}


class LazyTables(dict):
    """Table objects built on first use from {name: pickled (columns, rows)}."""

    def __init__(self, blobs):
        super().__init__()
        self.blobs = blobs

    def __missing__(self, k):
        from sqlglot.executor.table import Table

        c, r = pickle.loads(self.blobs[k])
        t = self[k] = Table(columns=c, rows=r)
        return t


def verify(spec):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import snapshot

    sys.meta_path.insert(0, snapshot._Finder(spec.get("patched", {})))
    # everything tests/test_executor.py imports: a module that no longer imports fails the test
    import sqlglot.errors, sqlglot.executor, sqlglot.executor.python, sqlglot.executor.table, sqlglot.optimizer, sqlglot.planner  # noqa: F401
    sch = schema()
    qs = queries()
    with open(TABLES, "rb") as fh:
        tables = LazyTables(pickle.load(fh))
    import time
    cap = spec.get("cap")
    for i in spec["queries"]:
        if cap is not None and time.process_time() > cap:  # costlier than it is worth: keep the test
            return False
        meta, sql, _ = qs[i]
        want = spec["want"][str(i)]
        t, r, _, _ = run_query(meta, sql, tables, sch, spec.get("mode", {}).get(str(i)), want)
        if t != want[0] or (r != want[1] and r != "same"):
            return False
    return True


if __name__ == "__main__":
    if sys.argv[1] == "tables":
        with open(sys.argv[2], "wb") as fh:
            blobs = {k: pickle.dumps(v, protocol=pickle.HIGHEST_PROTOCOL) for k, v in read_tables(schema()).items()}
            pickle.dump(blobs, fh, protocol=pickle.HIGHEST_PROTOCOL)
    else:
        import signal

        signal.alarm(int(os.environ.get("TPCDS_ALARM", "20")))
        try:
            ok = verify(json.load(sys.stdin))
        except BaseException:
            ok = False
        print(json.dumps({"ok": ok}))
