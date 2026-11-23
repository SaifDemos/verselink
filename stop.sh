#!/usr/bin/env bash
# Stop the VerseLink background service started by start.sh.
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

RUN_DIR=".run"
PID_FILE="$RUN_DIR/verselink.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "VerseLink is not running (no pid file)."
  exit 0
fi

pid="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -z "$pid" ]]; then
  rm -f "$PID_FILE"
  echo "VerseLink is not running (empty pid file)."
  exit 0
fi

if ! kill -0 "$pid" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "VerseLink is not running (stale pid $pid)."
  exit 0
fi

cmdline="$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)"
if [[ "$cmdline" != *"verselink.py"* ]]; then
  echo "Refusing to stop pid $pid: it is not verselink.py." >&2
  exit 1
fi

kill "$pid"
for _ in $(seq 1 50); do
  if ! kill -0 "$pid" 2>/dev/null; then
    break
  fi
  sleep 0.1
done

if kill -0 "$pid" 2>/dev/null; then
  echo "VerseLink did not stop in time; sending SIGKILL." >&2
  kill -9 "$pid" 2>/dev/null || true
fi

rm -f "$PID_FILE"
echo "VerseLink stopped (pid $pid)."
