#!/bin/sh
# usage: valiso.sh TESTS_JSON OUT_DIR SHARDS -- value map, one fresh pytest process per test (lab image br-lab)
TJ=$(realpath "$1"); OD=$(realpath "$2"); N=${3:-4}; T=$(realpath "$(dirname "$0")")
mkdir -p "$OD/res"
python3 - "$TJ" "$N" "$OD" <<'P'
import json, sys
ts = json.load(open(sys.argv[1]))["tests"]; n = int(sys.argv[2])
sh = [[0, []] for _ in range(n)]
for t in sorted(ts, key=lambda t: -t["seconds"]):
    m = min(sh, key=lambda x: x[0]); m[0] += t["seconds"] * 2 + 1.5; m[1].append([t["number"], t["id"]])
for i, (s, l) in enumerate(sh):
    json.dump(l, open(f"{sys.argv[3]}/shard{i}.json", "w"))
P
i=0
while [ $i -lt $N ]; do
  docker run --rm --memory 1100m -v "$T":/tools:ro -v "$OD":/out br-lab sh -c "python /tools/valrun.py /out/shard$i.json /out/res > /out/val$i.log 2>&1" &
  i=$((i+1))
done
wait
