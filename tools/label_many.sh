#!/bin/sh
# usage: label_many.sh PATCH_DIR OUT_DIR PARALLEL   -- labels every PATCH_DIR/*.patch with the full suite, one serial container each
PD=$(realpath "$1"); OD=$(realpath "$2"); P=${3:-3}; T=$(realpath "$(dirname "$0")")
mkdir -p "$OD"
ls "$PD"/*.patch | xargs -P "$P" -I{} sh -c '
  id=$(basename {} .patch)
  [ -f "'"$OD"'/$id.json" ] && exit 0
  docker run --rm --memory 2g -v "'"$T"'":/tools:ro -v "'"$PD"'":/p:ro -v "'"$OD"'":/out br-lab sh /tools/label.sh /p/$id.patch /out/$id.json
'
