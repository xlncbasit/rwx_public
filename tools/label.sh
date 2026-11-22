#!/bin/sh
# usage: label.sh PATCH_FILE|- OUT_JSON [pytest args...]   (runs inside the br-lab container)
P="$1"; O="$2"; shift 2
cd /opt/sqlglot
[ "$P" != "-" ] && python /tools/applypatch.py "$P"
LAB_OUT="$O" PYTHONPATH=/opt/sqlglot:/tools exec python -m pytest -p labelplug -q --no-header -p no:cacheprovider --timeout=120 "$@" tests > "$O.log" 2>&1
