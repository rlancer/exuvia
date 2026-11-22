#!/bin/sh
# Usage: run.sh WORKDIR PORT SERVER_SCRIPT [server args...] -- CLIENT_SCRIPT [client args...]
# Starts the server on a fresh catalog in WORKDIR, runs the client, stops the server.
set -e
Q="$(cd "$(dirname "$0")" && pwd)"
WORK="$1"; PORT="$2"; SERVER="$3"; shift 3
SARGS=""; while [ "$1" != "--" ]; do SARGS="$SARGS $1"; shift; done; shift
CLIENT="$1"; shift
if [ -e "$WORK" ]; then echo "workdir exists, pick a new one: $WORK"; exit 1; fi
mkdir -p "$WORK/data"
export PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1
UV="mise exec uv -- uv run --no-project --with duckdb==2.0.0 --with duckdb-extension-quack==2.0.0 --with duckdb-extension-httpfs==2.0.0 --with duckdb-extension-ducklake==2.0.0 --with numpy --with pytz"
cd "$WORK"
$UV python -u "$Q/$SERVER" "$WORK/catalog.duckdb" "$PORT" $SARGS > "$WORK/server.out" 2>&1 &
SPID=$!
for i in $(seq 1 60); do grep -q serving "$WORK/server.out" 2>/dev/null && break; sleep 1; done
set +e
$UV python -u "$Q/$CLIENT" "$PORT" "$WORK/data/" "$@" 2>&1 | grep -vE "leaked|nanobind|skipped remainder"
kill $SPID 2>/dev/null
# the Python server is a grandchild of $SPID on Windows: stop whatever listens on our test port
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort $PORT -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id \$_.OwningProcess -Force }" >/dev/null 2>&1
echo "--- server output:"; grep -vE "leaked|nanobind|skipped remainder" "$WORK/server.out" | tail -15
