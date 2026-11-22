#!/bin/sh
# usage: label_t.sh PATCH_FILE OUT_JSON TEST_IDS_FILE   (inside br-lab) -- label only the listed tests
P="$1"; O="$2"; L="$3"
cd /opt/sqlglot
python /tools/applypatch.py "$P"
LAB_OUT="$O" PYTHONPATH=/opt/sqlglot:/tools exec python -m pytest -p labelplug -q --no-header -p no:cacheprovider --timeout=120 $(cat "$L") > "$O.log" 2>&1
