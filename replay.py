"""Replays single queries of the TPC tests in tests/test_executor.py with patched sources.

Every replayable test passes on the pinned commit and checks each fixture query on its own:
  test_execute_tpcds / test_execute_tpch: parse_one(sql), pickle the tree to a worker, execute it there and
    compare with duckdb running transpile(sql, write="duckdb");
  test_optimized_tpch: duckdb on transpile(sql) vs duckdb on transpile(optimized fixture SQL).
If, for every query that reaches a changed line or reads a changed constant, the SQL handed to duckdb and the
executor result are identical to the pinned commit's, the test still passes.

  python replay.py tables OUT.pkl        (docker build) the CSV tables, parsed like the test's setUpClass
  python replay.py verify < spec.json     (run time) prints {"ok": {test id: bool}}
"""
import ast, csv, gzip, hashlib, json, os, pickle, sys, time

ROOT = os.environ.get("SQLGLOT_DIR", "/opt/sqlglot")
TABLES = os.environ.get("REPLAY_TABLES", "/app/data/replay_tables.pkl")
BENCH = {  # name -> (fixture, schema constant in tests/helpers.py, CSV directory)
    "tpch": ("optimizer/tpc-h/tpc-h.sql", "TPCH_SCHEMA", "tests/fixtures/optimizer/tpc-h/"),
    "tpcds": ("optimizer/tpc-ds/tpc-ds.sql", "TPCDS_SCHEMA", "tests/fixtures/optimizer/tpc-ds/"),
}
P = "tests/test_executor.py::TestExecutor::"
TESTS = {  # test id -> (short key used in map contexts, kind, bench)
    P + "test_execute_tpcds": ("ds", "exec", "tpcds"),
    P + "test_execute_tpch": ("h", "exec", "tpch"),
    P + "test_optimized_tpch": ("oh", "opt", "tpch"),
}


def helpers():
    sys.path.insert(0, ROOT)
    import tests.helpers as hp

    return hp


def schema(bench):
    return getattr(helpers(), BENCH[bench][1])


def queries(bench, text=None):
    """The fixture pairs, from the file or (edited fixture) from `text`, as load_sql_fixture_pairs yields them."""
    hp = helpers()
    if text is None:
        return list(hp.load_sql_fixture_pairs(BENCH[bench][0]))
    statements = hp._filter_comments(text).split(";")
    out = []
    for i in range(0, len(statements), 2):
        if i + 1 < len(statements):
            sql, meta = hp._extract_meta(statements[i].strip())
            out.append((meta, sql, statements[i + 1].strip()))
    return out


def read_tables(bench):
    """Same parsing as TestExecutor.setUpClass (types from the first row via literal_eval)."""
    out = {}
    for table, columns in schema(bench).items():
        with gzip.open(os.path.join(ROOT, BENCH[bench][2], f"{table}.csv.gz"), "rt", newline="") as fh:
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


def tree_hash(e):
    return h(repr(canon(e)))


class Same(Exception):
    pass


def run_query(kind, q, tables, sch, mode=None, want=None, phase=None, rec=False, early=False):
    """Hashes of one query, computed exactly as the test does.
    exec: (duckdb SQL, executor result, parsed tree, optimized tree); opt: (duckdb SQL of sql, of optimized SQL).
    Phases: "q" in the test process (parse_one, transpile), "e" pickling to the pool, find_tables and execute minus the
    optimizer, "o" the optimizer. mode "p": an identical parsed tree skips "e"/"o"; mode "o": an identical optimized
    tree skips the rest of "e"."""
    from sqlglot import find_tables, parse_one, transpile
    import sqlglot.executor as E

    meta, sql, expected = q
    phase = phase or (lambda name: None)
    phase("q")
    if kind == "opt":
        return [h(transpile(sql, write="duckdb")[0]), h(transpile(expected, write="duckdb")[0]), None, None]
    if early:  # a different duckdb SQL already fails the test
        t = h(transpile(sql, write="duckdb")[0])
        if t != want[0]:
            return [t, None, None, None]
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
    return [h(transpile(sql, write="duckdb")[0]), res, ph, oh]


def same(kind, got, want):
    if kind == "opt":
        return got[:2] == want[:2]
    return got[0] == want[0] and got[1] in (want[1], "same")


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
    with open(TABLES, "rb") as fh:
        blobs = pickle.load(fh)
    cap = spec.get("cap")
    out, qcache, tcache = {}, {}, {}
    for job in spec["tests"]:
        out[job["test"]] = False
    fixtures = spec.get("fixtures", {})
    try:  # setUpClass loads both fixtures: one that no longer loads fails every test of the class
        new = {b: queries(b, text) for b, text in fixtures.items()}
    except Exception:
        return out
    for job in spec["tests"]:
        key, kind, bench = TESTS[job["test"]]
        if bench not in qcache:
            qcache[bench] = new.get(bench) or queries(bench)
            tcache[bench] = LazyTables(blobs[bench])
        qs, sch = qcache[bench], schema(bench)
        order, mode = list(job["queries"]), dict(job["mode"])
        if bench in new:
            old = queries(bench)
            if len(old) != len(qs):
                continue
            for i, (a, b) in enumerate(zip(old, qs)):
                if a != b and i not in order:
                    order.insert(0, i)
                    mode[str(i)] = "p"
        ok = True
        for i in order:
            if cap is not None and time.process_time() > cap:  # costlier than it is worth: keep the test
                ok = False
                break
            want = job["want"][str(i)]
            try:
                got = run_query(kind, qs[i], tcache[bench], sch, mode.get(str(i)), want, early=True)
            except Exception:  # the test would fail too
                got = None
            if got is None or not same(kind, got, want):
                ok = False
                break
        out[job["test"]] = ok
    return out


if __name__ == "__main__":
    if sys.argv[1] == "tables":
        with open(sys.argv[2], "wb") as fh:
            pickle.dump({b: {k: pickle.dumps(v, protocol=pickle.HIGHEST_PROTOCOL) for k, v in read_tables(b).items()} for b in BENCH},
                        fh, protocol=pickle.HIGHEST_PROTOCOL)
    else:
        import signal

        signal.alarm(int(os.environ.get("REPLAY_ALARM", "30")))
        try:
            res = verify(json.load(sys.stdin))
        except BaseException:
            res = {}
        print(json.dumps({"ok": res}))
