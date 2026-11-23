#!/bin/sh
# Build exuvia_policy.duckdb_extension for this machine. Usage: build.sh OUTDIR
#
# Needs only uv: the compiler is zig (`zig cc`, from the ziglang wheel on PyPI), so no MSVC,
# gcc or cmake. Fetches duckdb.h and duckdb_extension.h at the DuckDB tag the extension targets,
# compiles a shared library and appends the metadata footer DuckDB checks on LOAD.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$1"
DUCKDB_TAG=v2.0.0
C_API_VERSION=v1.5.6   # DUCKDB_EXTENSION_API_VERSION_* in that tag's duckdb_extension.h
mkdir -p "$OUT/include"
for f in duckdb.h duckdb_extension.h; do
  [ -f "$OUT/include/$f" ] || curl -sSfL -o "$OUT/include/$f" \
    "https://raw.githubusercontent.com/duckdb/duckdb/$DUCKDB_TAG/src/include/$f"
done
UV="mise exec uv -- uv run --no-project --with ziglang --with duckdb==2.0.0"
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) TARGET=x86_64-windows-gnu; LIB=exuvia_policy.dll ;;
  Darwin) TARGET=native; LIB=exuvia_policy.dylib ;;
  *) TARGET=native; LIB=exuvia_policy.so ;;
esac
$UV python -m ziglang cc -shared -O2 -fPIC -target "$TARGET" -I "$OUT/include" \
  "$HERE/exuvia_policy.c" -o "$OUT/$LIB"
$UV python "$HERE/append_footer.py" "$OUT/$LIB" "$OUT/exuvia_policy.duckdb_extension" "$C_API_VERSION"
echo "built $OUT/exuvia_policy.duckdb_extension"
