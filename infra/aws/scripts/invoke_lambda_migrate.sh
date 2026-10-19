#!/usr/bin/env bash
# Invoke Lambda {"action":"migrate"} then {"action":"ready"} and fail if either errors.
# AWS CLI returns 0 even when FunctionError is set, so callers must check it.
# Ready pulses federation peers and republishes remembered Home tabs.
#
# Usage: invoke_lambda_migrate.sh <function-arn> [region]

set -euo pipefail

FUNCTION_ARN="${1:?function ARN is required}"
REGION="${2:-}"

PAYLOAD=""
META=""
cleanup() { rm -f "${PAYLOAD:-}" "$META"; }
trap cleanup EXIT

invoke_action() {
  local action="$1"
  PAYLOAD="$(mktemp)"
  META="$(mktemp)"

  local args=(
    --function-name "$FUNCTION_ARN"
    --payload "{\"action\":\"${action}\"}"
    --cli-binary-format raw-in-base64-out
    --cli-read-timeout 180
    "$PAYLOAD"
  )
  if [[ -n "$REGION" ]]; then
    args+=(--region "$REGION")
  fi

  aws lambda invoke "${args[@]}" | tee "$META"
  cat "$PAYLOAD"
  echo
  if grep -q '"FunctionError"' "$META"; then
    echo "Error: Lambda ${action} failed." >&2
    python3 - "$PAYLOAD" <<'PY' >&2 || true
import json
import sys

try:
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(0)
msg = payload.get("errorMessage") if isinstance(payload, dict) else None
if msg:
    print(msg)
PY
    exit 1
  fi
  rm -f "$PAYLOAD" "$META"
  PAYLOAD=""
  META=""
}

invoke_action migrate
invoke_action ready
