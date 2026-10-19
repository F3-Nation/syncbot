#!/bin/bash
# Cloud Run entrypoint. SQLite + Litestream when LITESTREAM_GCS_BUCKET is set;
# otherwise start the app only (existing MySQL / TiDB).
#
# Do not `exec python` while Litestream is running: exec replaces this shell
# and the SIGTERM trap would never flush the WAL.
set -euo pipefail

DB_PATH="/data/syncbot.db"
APP_PID=""
LITESTREAM_PID=""

cleanup() {
  if [[ -n "$APP_PID" ]]; then
    kill "$APP_PID" 2>/dev/null || true
    wait "$APP_PID" 2>/dev/null || true
  fi
  if [[ -n "$LITESTREAM_PID" ]]; then
    echo "litestream: shutting down replicator (pid $LITESTREAM_PID)..."
    kill "$LITESTREAM_PID" 2>/dev/null || true
    wait "$LITESTREAM_PID" 2>/dev/null || true
    echo "litestream: done."
  fi
}

cd /app/syncbot

if [[ -z "${LITESTREAM_GCS_BUCKET:-}" ]]; then
  exec python app.py
fi

mkdir -p "$(dirname "$DB_PATH")"

echo "litestream: restoring from GCS (if replica exists)..."
litestream restore -if-replica-exists -config /etc/litestream.yml "$DB_PATH"

echo "litestream: starting continuous replication..."
litestream replicate -config /etc/litestream.yml &
LITESTREAM_PID=$!

trap cleanup EXIT INT TERM

python app.py &
APP_PID=$!
set +e
wait "$APP_PID"
APP_STATUS=$?
set -e
APP_PID=""
trap - EXIT INT TERM
cleanup
exit "$APP_STATUS"
