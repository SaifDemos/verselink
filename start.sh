#!/usr/bin/env bash
# Start VerseLink in the background and print the URLs to open on other devices.
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

PYTHON="${PYTHON:-python3}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8765}"
PLAYER="${PLAYER:-}"
RUN_DIR=".run"
PID_FILE="$RUN_DIR/verselink.pid"
LOG_FILE="$RUN_DIR/verselink.log"

mkdir -p "$RUN_DIR"

case "$HOST" in
  0.0.0.0 | :: | "") PING_HOST="127.0.0.1"; LOCAL_HOST="127.0.0.1" ;;
  *) PING_HOST="$HOST"; LOCAL_HOST="$HOST" ;;
esac

lan_ips() {
  if command -v hostname >/dev/null 2>&1; then
    hostname -I 2>/dev/null | tr ' ' '\n' || true
  fi
  if command -v ip >/dev/null 2>&1; then
    ip -4 route get 1.1.1.1 2>/dev/null |
      awk '{for (i = 1; i <= NF; i++) if ($i == "src") print $(i + 1)}' || true
  fi
}

show_urls() {
  echo "  local:   http://${LOCAL_HOST}:${PORT}"
  if [[ "$HOST" == "0.0.0.0" || "$HOST" == "::" || -z "$HOST" ]]; then
    while IFS= read -r ip; do
      if [[ -z "$ip" || "$ip" == 127.* || "$ip" == *:* ]]; then
        continue
      fi
      echo "  network: http://${ip}:${PORT}"
    done < <(lan_ips | sort -u)
  fi
}

if [[ -f "$PID_FILE" ]]; then
  old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
    old_cmd="$(tr '\0' ' ' <"/proc/$old_pid/cmdline" 2>/dev/null || true)"
    if [[ "$old_cmd" == *"verselink.py"* ]]; then
      echo "VerseLink is already running (pid $old_pid)."
      show_urls
      exit 0
    fi
  fi
  rm -f "$PID_FILE"
fi

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "error: '$PYTHON' not found. Install Python 3 and try again." >&2
  exit 1
fi
if ! command -v playerctl >/dev/null 2>&1; then
  echo "warning: playerctl not found; install it to read media players on Linux." >&2
fi
if [[ ! -f verselink.py ]]; then
  echo "error: verselink.py is missing next to this script." >&2
  exit 1
fi

args=(--host "$HOST" --port "$PORT")
if [[ -n "$PLAYER" ]]; then
  args+=(--player "$PLAYER")
fi

nohup "$PYTHON" verselink.py "${args[@]}" >>"$LOG_FILE" 2>&1 &
pid=$!
echo "$pid" >"$PID_FILE"

ready=0
for _ in $(seq 1 60); do
  if ! kill -0 "$pid" 2>/dev/null; then
    break
  fi
  if (exec 3<>"/dev/tcp/${PING_HOST}/${PORT}") 2>/dev/null; then
    ready=1
    break
  fi
  sleep 0.1
done

if [[ "$ready" -ne 1 ]]; then
  echo "VerseLink failed to start. Recent log output:" >&2
  tail -n 30 "$LOG_FILE" >&2 2>/dev/null || true
  kill "$pid" 2>/dev/null || true
  rm -f "$PID_FILE"
  exit 1
fi

echo "VerseLink is running (pid $pid)."
show_urls
echo "  log:     $LOG_FILE"
if [[ "$HOST" == "0.0.0.0" || "$HOST" == "::" || -z "$HOST" ]]; then
  echo "  (anyone on this network can control the player; use HOST=127.0.0.1 to stay local)"
fi
