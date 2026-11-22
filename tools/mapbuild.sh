#!/bin/sh
# usage: mapbuild.sh TESTS_JSON OUT_DIR SHARDS  -- per-test coverage/fixtures/modules for the pristine tree
TJ=$(realpath "$1"); OD=$(realpath "$2"); N=${3:-4}; T=$(realpath "$(dirname "$0")")
mkdir -p "$OD"
python3 - "$TJ" "$N" "$OD" <<'P'
import json, sys
ts = json.load(open(sys.argv[1]))["tests"]; n = int(sys.argv[2])
w = {}
for t in ts: w[t["file"]] = w.get(t["file"], 0) + t["seconds"] + 0.01
sh = [[0, []] for _ in range(n)]
for f, s in sorted(w.items(), key=lambda x: -x[1]):
    m = min(sh, key=lambda x: x[0]); m[0] += s; m[1].append(f)
for i, (s, fs) in enumerate(sh):
    open(f"{sys.argv[3]}/shard{i}.files", "w").write(" ".join(fs))
P
i=0
while [ $i -lt $N ]; do
  docker run --rm --memory 1800m -v "$T":/tools:ro -v "$OD":/out -e COVERAGE_FILE=/out/cov$i -e COVERAGE_PROCESS_START=/tools/cov.rc -e MAP_CHILD_DIR=/out -e MAP_OUT=/out/map$i.json br-lab sh -c \
    "PYTHONPATH=/opt/sqlglot:/tools python -m coverage run --rcfile=/tools/cov.rc -m pytest -p mapplug -p no:cacheprovider -q --no-header --timeout=900 \$(cat /out/shard$i.files) > /out/shard$i.log 2>&1" &
  i=$((i+1))
done
wait
