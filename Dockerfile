FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# The pinned sqlglot checkout: selector.py diffs patches against it and snapshot.py imports it.
RUN python - <<'P'
import io, os, tarfile, urllib.request
sha = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
data = urllib.request.urlopen(f"https://github.com/tobymao/sqlglot/archive/{sha}.tar.gz").read()
tarfile.open(fileobj=io.BytesIO(data)).extractall("/opt")
os.rename(f"/opt/sqlglot-{sha}", "/opt/sqlglot")
P
COPY selector.py patchlib.py snapshot.py replay.py /app/
COPY data/map.json.gz data/replay.json.gz /app/data/
# the TPC tests are proven safe by replaying single queries: their CSV tables, parsed at build time like setUpClass
RUN PYTHONHASHSEED=0 python /app/replay.py tables /app/data/replay_tables.pkl
# The container's disk is read-only at run time: ship the byte-code so imports do not compile anything.
RUN python -m compileall -q /opt/sqlglot/sqlglot /app && chmod -R a+rX /app /opt/sqlglot

ENV SQLGLOT_DIR=/opt/sqlglot PYTHONDONTWRITEBYTECODE=1
# PROBE (not for the final): never select the three heaviest tests
ENV PROBE_DROP=tests/test_parser.py::TestParser::test_max_nodes,tests/test_executor.py::TestExecutor::test_execute_tpcds,tests/test_generator.py::TestGenerator::test_generator_memory
CMD ["python", "/app/selector.py"]
