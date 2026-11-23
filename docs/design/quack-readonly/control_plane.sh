#!/bin/sh
# Usage: control_plane.sh WORKDIR HTTP_PORT QUACK_PORT MODE [NATIVE_EXT]
# Runs exuvia's server with Quack inside it (control_plane.py serve), loads it
# (control_plane.py load), stops it. MODE: none | python | native. WORKDIR must not exist.
set -e
Q="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$Q/../../.." && pwd)"
WORK="$1"; HTTP="$2"; QUACK="$3"; MODE="$4"; EXT="$5"
if [ -e "$WORK" ]; then echo "workdir exists, pick a new one: $WORK"; exit 1; fi
mkdir -p "$WORK"
export PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1
UV="mise exec uv -- uv run --project $REPO --with duckdb-extension-quack==2.0.0 --with duckdb-extension-httpfs==2.0.0 --with duckdb-extension-ducklake==2.0.0"
$UV python -u "$Q/control_plane.py" serve "$WORK/project" "$HTTP" "$QUACK" "$MODE" $EXT > "$WORK/server.out" 2>&1 &
for i in $(seq 1 120); do
  curl -sf "http://127.0.0.1:$HTTP/_test/stats" >/dev/null 2>&1 && break; sleep 1
done
set +e
$UV python -u "$Q/control_plane.py" load "$HTTP" "$QUACK" "$WORK/project/.exuvia/lake-data/" "$WORK/results.json" 2>&1 \
  | grep -vE "leaked|nanobind"
for p in "$HTTP" "$QUACK"; do
  powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id \$_.OwningProcess -Force }" >/dev/null 2>&1
done
echo "--- server output:"; grep -vE "leaked|nanobind" "$WORK/server.out" | tail -15
