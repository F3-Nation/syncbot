#!/usr/bin/env bash
# Invoke Lambda {"action":"migrate"} and fail if the function errors.
# AWS CLI returns 0 even when FunctionError is set, so callers must check it.
#
# Usage: invoke_lambda_migrate.sh <function-arn> [region]

set -euo pipefail

FUNCTION_ARN="${1:?function ARN is required}"
REGION="${2:-}"

PAYLOAD="$(mktemp)"
META="$(mktemp)"
cleanup() { rm -f "$PAYLOAD" "$META"; }
trap cleanup EXIT

args=(
  --function-name "$FUNCTION_ARN"
  --payload '{"action":"migrate"}'
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
  echo "Error: Lambda migrate failed. The function reported FunctionError (see payload above)." >&2
  exit 1
fi
