#!/bin/bash
# Entrypoint for Cloud Run + Litestream.
# 1. Restore the SQLite DB from GCS (if a replica exists).
# 2. Start Litestream replication in the background.
# 3. Exec the application.
set -e

DB_PATH="/data/syncbot.db"
DB_DIR="$(dirname "$DB_PATH")"
mkdir -p "$DB_DIR"

# --- Litestream restore -----------------------------------------------
# If a GCS replica exists, restore it. If this is a fresh deploy with no
# replica yet, the restore will fail gracefully (-if-replica-exists).
echo "litestream: restoring from GCS (if replica exists)..."
litestream restore -if-replica-exists -config /etc/litestream.yml "$DB_PATH"

# --- Litestream replicate (background) ---------------------------------
echo "litestream: starting continuous replication..."
litestream replicate -config /etc/litestream.yml &
LITESTREAM_PID=$!

# --- Shutdown handler ---------------------------------------------------
cleanup() {
  echo "litestream: shutting down replicator (pid $LITESTREAM_PID)..."
  kill "$LITESTREAM_PID" 2>/dev/null || true
  wait "$LITESTREAM_PID" 2>/dev/null || true
  echo "litestream: done."
}
trap cleanup EXIT INT TERM

# --- Start the app ------------------------------------------------------
cd /app/syncbot
exec python app.py
