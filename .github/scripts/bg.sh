#!/usr/bin/env bash
# Runs a slow CI command in the background, so the steps after it overlap
# with it instead of waiting for it.
#
#   bg.sh start <name> <command> [args...]   start it; its output goes to a log
#   bg.sh wait  <name> [timeout-seconds]     wait for it, print the log, exit with its status
#
# A process started in one workflow step keeps running through the next ones
# (the runner cleans up only when the job ends): `start` in an early step,
# `wait` in the step that needs the result.
set -euo pipefail

dir="${RUNNER_TEMP:-/tmp}/bg"
mkdir -p "$dir"
action=$1 name=$2
shift 2

case "$action" in
  start)
    # The status file appears, whole, only once the command has finished.
    # shellcheck disable=SC2016  # the inner shell expands these, not this one
    nohup bash -c '
      "$@" >"$0.log" 2>&1
      status=$?
      echo "($SECONDS s, exit status $status)" >>"$0.log"
      echo "$status" >"$0.tmp" && mv "$0.tmp" "$0.status"
    ' "$dir/$name" "$@" </dev/null >/dev/null 2>&1 &
    echo "Started in the background as '$name': $*"
    ;;
  wait)
    timeout=${1:-300}
    for ((waited = 0; waited < timeout; waited++)); do
      [ -f "$dir/$name.status" ] && break
      sleep 1
    done
    echo "::group::Output of '$name' (waited ${waited}s for it here)"
    cat "$dir/$name.log" 2>/dev/null || true
    echo "::endgroup::"
    if [ ! -f "$dir/$name.status" ]; then
      echo "::error::'$name' was still running after ${timeout}s"
      exit 1
    fi
    status=$(cat "$dir/$name.status")
    [ "$status" = 0 ] || echo "::error::'$name' failed with exit status $status"
    exit "$status"
    ;;
  *)
    echo "usage: bg.sh start <name> <command> [args...] | bg.sh wait <name> [timeout-seconds]" >&2
    exit 2
    ;;
esac
