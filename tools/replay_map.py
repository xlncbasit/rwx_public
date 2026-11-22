"""Per-query map of the replayable TPC tests (run in the lab image, PYTHONHASHSEED=0): per test, query and phase
(q: parse + transpile, o: optimizer, e: rest of the execution) the sqlglot lines reached and the class constants read;
expected hashes; CPU seconds of a full replay and of replays cut at the parse / optimizer checkpoints.
Contexts are named "<test key>:<phase><query>", and "setup:<bench>" for building the tables.
usage: PYTHONHASHSEED=0 python tools/replay_map.py OUT.json.gz"""
import gzip, json, os, sys, time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))
import mapplug  # records class-constant reads (must come before sqlglot)
import coverage
import replay as R

ROOT = R.ROOT
cov = coverage.Coverage(data_file=None, include=[ROOT + "/sqlglot/*"])
cov.start()
import sqlglot.dialects.duckdb, sqlglot.executor, sqlglot.executor.python, sqlglot.optimizer, sqlglot.planner  # noqa
acc_by = {}
cur = ["none"]


def phase(name):
    acc_by.setdefault(cur[0], set()).update(mapplug.acc)
    mapplug.acc.clear()
    cur[0] = name
    mapplug._swc(name)


mapplug.acc.clear()
plain, tables, qs, sch = {}, {}, {}, {}
for b in R.BENCH:
    sch[b], qs[b] = R.schema(b), R.queries(b)
    plain[b] = R.read_tables(b)
    phase(f"setup:{b}")
    from sqlglot.executor.table import Table
    tables[b] = {k: Table(columns=c, rows=r) for k, (c, r) in plain[b].items()}
want = {}
for test, (key, kind, b) in R.TESTS.items():
    want[test] = {}
    for i, q in enumerate(qs[b]):
        want[test][i] = R.run_query(kind, q, tables[b], sch[b], rec=True, phase=lambda p, i=i, key=key: phase(f"{key}:{p}{i}"))
phase("end")
cov.stop()
data = cov.get_data()
lines = {}
for f in data.measured_files():
    rel = os.path.relpath(f, ROOT)
    for ln, cs in data.contexts_by_lineno(f).items():
        for c in cs:
            if ":" in c:
                lines.setdefault(c, {}).setdefault(rel, set()).add(ln)


def qn(T, n):
    return f"{T.__module__}.{T.__qualname__}|{n}"


secs = {}
for test, (key, kind, b) in R.TESTS.items():
    secs[test] = {}
    for i, q in enumerate(qs[b]):
        row = []
        for mode in (None, "o", "p"):
            t = time.process_time()
            r = R.run_query(kind, q, tables[b], sch[b], mode, want[test][i], early=True)
            row.append(round(time.process_time() - t, 4))
            assert R.same(kind, r, want[test][i]), ("unstable", test, i, mode)
        secs[test][i] = row
from tests.test_executor import TestExecutor  # our table parsing must equal the test's own setUpClass
TestExecutor.setUpClass()
for b, mine in (("tpch", TestExecutor.tpch_tables), ("tpcds", TestExecutor.tpcds_tables)):
    for k, tb in mine.items():
        assert tuple(tb.columns) == tuple(plain[b][k][0]) and tb.rows == plain[b][k][1], (b, k)
names = sorted({qn(T, n) for c, s in acc_by.items() if ":" in c for T, n in s})
ix = {n: j for j, n in enumerate(names)}
ctxs = sorted(c for c in set(lines) | set(acc_by) if ":" in c)
out = {"want": want, "secs": secs, "acc_names": names,
       "lines": {c: {f: sorted(v) for f, v in lines.get(c, {}).items()} for c in ctxs},
       "acc": {c: sorted(ix[qn(T, n)] for T, n in acc_by.get(c, ())) for c in ctxs}}
with gzip.open(sys.argv[1], "wt") as fh:
    json.dump(out, fh)
for test in R.TESTS:
    print(test, len(secs[test]), "full/o/p s", [round(sum(r[k] for r in secs[test].values()), 2) for k in range(3)])
