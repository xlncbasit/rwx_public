"""Per-query map for test_execute_tpcds (run in the lab image, PYTHONHASHSEED=0): per query and phase (q: parse +
transpile, o: optimizer, e: rest of the execution) the sqlglot lines reached and the class constants read; expected
hashes; CPU seconds of a full replay and of replays cut at the parse / optimizer checkpoints.
usage: PYTHONHASHSEED=0 python tools/tpcds_map.py OUT.json.gz"""
import gzip, json, os, sys, time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))
import mapplug  # records class-constant reads (must come before sqlglot)
import coverage
import tpcds

ROOT = tpcds.ROOT
cov = coverage.Coverage(data_file=None, include=[ROOT + "/sqlglot/*"])
cov.start()
sch = tpcds.schema()
qs = tpcds.queries()
import sqlglot.dialects.duckdb, sqlglot.executor, sqlglot.executor.python, sqlglot.optimizer, sqlglot.planner  # noqa
plain = tpcds.read_tables(sch)
acc_by = {}
cur = ["setup"]


def phase(name):
    acc_by.setdefault(cur[0], set()).update(mapplug.acc)
    mapplug.acc.clear()
    cur[0] = name
    mapplug._swc(name)


mapplug.acc.clear()
phase("setup")
tables = tpcds.make_tables(plain)
want = {}
for i, (meta, sql, _) in enumerate(qs):
    want[i] = list(tpcds.run_query(meta, sql, tables, sch, rec=True, phase=lambda p, i=i: phase(f"{p}{i}")))
phase("end")
cov.stop()
data = cov.get_data()
lines = {}  # context -> file -> lines
for f in data.measured_files():
    rel = os.path.relpath(f, ROOT)
    for ln, cs in data.contexts_by_lineno(f).items():
        for c in cs:
            lines.setdefault(c, {}).setdefault(rel, set()).add(ln)


def q(T, n):
    return f"{T.__module__}.{T.__qualname__}|{n}"


secs = {}
for i, (meta, sql, _) in enumerate(qs):
    row = []
    for mode in (None, "o", "p"):
        t = time.process_time()
        r = tpcds.run_query(meta, sql, tables, sch, mode, want[i])
        row.append(round(time.process_time() - t, 4))
        assert r[0] == want[i][0] and r[1] in (want[i][1], "same"), ("unstable", i, mode)
    secs[i] = row
from tests.test_executor import TestExecutor  # our table parsing must equal the test's own setUpClass
TestExecutor.setUpClass()
for k, tb in TestExecutor.tpcds_tables.items():
    assert tuple(tb.columns) == tuple(plain[k][0]) and tb.rows == plain[k][1], k
names = sorted({q(T, n) for s in acc_by.values() for T, n in s})
ix = {n: j for j, n in enumerate(names)}
ctxs = ["setup"] + [f"{p}{i}" for i in range(len(qs)) for p in "qoe"]
out = {"want": want, "secs": secs, "acc_names": names,
       "lines": {c: {f: sorted(v) for f, v in lines.get(c, {}).items()} for c in ctxs},
       "acc": {c: sorted(ix[q(T, n)] for T, n in acc_by.get(c, ())) for c in ctxs}}
with gzip.open(sys.argv[1], "wt") as fh:
    json.dump(out, fh)
print("queries", len(qs), "full/o/p s", [round(sum(r[k] for r in secs.values()), 2) for k in range(3)], "acc names", len(names))
