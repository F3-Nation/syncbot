#!/usr/bin/env bash
# Resolve DATABASE_BACKEND for AWS/GCP deploy scripts.
#
# Canonical: DATABASE_BACKEND=mysql|postgresql|sqlite
# Aliases (warn; remove in 2.0.0): AWS_DATABASE_MODE, GCP_DATABASE_MODE,
# DATABASE_ENGINE, EXISTING_DATABASE_HOST, EXISTING_DATABASE_PORT.
#
# Source this file, then:
#   resolve_database_backend aws|gcp
#   require_database_credentials_for_backend
#
# Sets DATABASE_BACKEND (and DATABASE_HOST / DATABASE_PORT when applying EXISTING_* aliases).

syncbot_warn_deprecated() {
  local msg="$1"
  echo "Warning: $msg" >&2
  if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
    echo "::warning::$msg" >&2
  fi
}

_syncbot_lower_trim() {
  echo "${1:-}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]'
}

apply_existing_database_host_aliases() {
  if [[ -z "${DATABASE_HOST:-}" && -n "${EXISTING_DATABASE_HOST:-}" ]]; then
    DATABASE_HOST="$EXISTING_DATABASE_HOST"
    syncbot_warn_deprecated "EXISTING_DATABASE_HOST is deprecated; set DATABASE_HOST instead."
  fi
  if [[ -z "${DATABASE_PORT:-}" && -n "${EXISTING_DATABASE_PORT:-}" ]]; then
    DATABASE_PORT="$EXISTING_DATABASE_PORT"
    syncbot_warn_deprecated "EXISTING_DATABASE_PORT is deprecated; set DATABASE_PORT instead."
  fi
}

# Sets DATABASE_BACKEND. Provider default when nothing is set: aws=mysql, gcp=sqlite.
resolve_database_backend() {
  local provider="${1:?resolve_database_backend: aws or gcp required}"
  local canonical engine aws_mode gcp_mode alias_used old_set=0

  apply_existing_database_host_aliases

  if [[ -n "${AWS_DATABASE_MODE:-}" || -n "${GCP_DATABASE_MODE:-}" || -n "${DATABASE_ENGINE:-}" ]]; then
    old_set=1
  fi

  canonical="$(_syncbot_lower_trim "${DATABASE_BACKEND:-}")"
  if [[ -n "$canonical" ]]; then
    case "$canonical" in
      mysql | postgresql | sqlite) ;;
      *)
        echo "Error: DATABASE_BACKEND must be mysql, postgresql, or sqlite (got '${DATABASE_BACKEND}')." >&2
        return 1
        ;;
    esac
    if [[ "$old_set" -eq 1 ]]; then
      syncbot_warn_deprecated "AWS_DATABASE_MODE, GCP_DATABASE_MODE, and DATABASE_ENGINE are ignored because DATABASE_BACKEND=${canonical} is set."
    fi
    DATABASE_BACKEND="$canonical"
    return 0
  fi

  engine="$(_syncbot_lower_trim "${DATABASE_ENGINE:-}")"
  aws_mode="$(_syncbot_lower_trim "${AWS_DATABASE_MODE:-}")"
  gcp_mode="$(_syncbot_lower_trim "${GCP_DATABASE_MODE:-}")"

  if [[ "$engine" == "sqlite" || "$aws_mode" == "sqlite" || "$gcp_mode" == "sqlite" ]]; then
    DATABASE_BACKEND="sqlite"
    if [[ "$engine" == "sqlite" ]]; then
      alias_used="DATABASE_ENGINE=sqlite"
    elif [[ "$aws_mode" == "sqlite" ]]; then
      alias_used="AWS_DATABASE_MODE=sqlite"
    else
      alias_used="GCP_DATABASE_MODE=sqlite"
    fi
    syncbot_warn_deprecated "${alias_used} is deprecated; set DATABASE_BACKEND=sqlite instead."
    return 0
  fi

  if [[ "$aws_mode" == "existing" || "$gcp_mode" == "existing" ]]; then
    case "$engine" in
      postgresql) DATABASE_BACKEND="postgresql" ;;
      mysql | "") DATABASE_BACKEND="mysql" ;;
      *)
        echo "Error: DATABASE_ENGINE must be mysql, postgresql, or sqlite (got '${DATABASE_ENGINE}')." >&2
        return 1
        ;;
    esac
    if [[ "$gcp_mode" == "existing" ]]; then
      alias_used="GCP_DATABASE_MODE=existing"
    else
      alias_used="AWS_DATABASE_MODE=existing"
    fi
    syncbot_warn_deprecated "${alias_used} is deprecated; set DATABASE_BACKEND=${DATABASE_BACKEND} instead."
    return 0
  fi

  if [[ -n "$aws_mode" ]]; then
    echo "Error: AWS_DATABASE_MODE must be sqlite or existing (got '${AWS_DATABASE_MODE}')." >&2
    return 1
  fi
  if [[ -n "$gcp_mode" ]]; then
    echo "Error: GCP_DATABASE_MODE must be sqlite or existing (got '${GCP_DATABASE_MODE}')." >&2
    return 1
  fi
  if [[ -n "$engine" ]]; then
    case "$engine" in
      mysql | postgresql)
        DATABASE_BACKEND="$engine"
        syncbot_warn_deprecated "DATABASE_ENGINE=${engine} is deprecated; set DATABASE_BACKEND=${engine} instead."
        return 0
        ;;
      *)
        echo "Error: DATABASE_ENGINE must be mysql, postgresql, or sqlite (got '${DATABASE_ENGINE}')." >&2
        return 1
        ;;
    esac
  fi

  case "$provider" in
    aws) DATABASE_BACKEND="mysql" ;;
    gcp) DATABASE_BACKEND="sqlite" ;;
    *)
      echo "Error: resolve_database_backend provider must be aws or gcp (got '${provider}')." >&2
      return 1
      ;;
  esac
}

# Copy LEGACY into CANONICAL when only the old name is set (warn; remove in 2.0.0).
syncbot_coalesce_alias() {
  local canonical_name="$1"
  local legacy_name="$2"
  local canonical_val=""
  local legacy_val=""
  eval "canonical_val=\"\${${canonical_name}:-}\""
  eval "legacy_val=\"\${${legacy_name}:-}\""
  if [[ -n "$canonical_val" || -z "$legacy_val" ]]; then
    return 0
  fi
  printf -v "$canonical_name" '%s' "$legacy_val"
  syncbot_warn_deprecated "${legacy_name} is deprecated; set ${canonical_name} instead."
}

apply_aws_provider_env_aliases() {
  syncbot_coalesce_alias AWS_STACK_NAME STACK_NAME
  syncbot_coalesce_alias AWS_BOOTSTRAP_STACK_NAME BOOTSTRAP_STACK_NAME
  syncbot_coalesce_alias AWS_S3_BUCKET DEPLOYMENT_S3_BUCKET
  syncbot_coalesce_alias AWS_ROLE_TO_ASSUME AWS_ROLE_ARN
  syncbot_coalesce_alias AWS_CREATE_OIDC_PROVIDER CREATE_OIDC_PROVIDER
  syncbot_coalesce_alias AWS_DEPLOY_BUCKET_PREFIX DEPLOY_BUCKET_PREFIX
  syncbot_coalesce_alias AWS_ENABLE_XRAY ENABLE_XRAY
  syncbot_coalesce_alias AWS_UPDATE_STACK UPDATE_STACK
}

apply_gcp_provider_env_aliases() {
  syncbot_coalesce_alias GCP_CLOUD_RUN_IMAGE CLOUD_RUN_IMAGE
}

# Print the first uncommented non-empty KEY=value from an env file. Exit 1 if missing.
env_file_assignment_value() {
  local key="$1"
  local file="${2:-${ENV_FILE_PATH:-}}"
  [[ -n "$file" && -f "$file" ]] || return 1
  python3 - "$key" "$file" <<'PY'
import re
import sys

key, path = sys.argv[1], sys.argv[2]
pat = re.compile(r"^\s*" + re.escape(key) + r"=(.*)$")
with open(path, encoding="utf-8") as fh:
    for raw in fh:
        line = raw.rstrip("\n")
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        match = pat.match(line)
        if not match:
            continue
        val = match.group(1).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if val:
            sys.stdout.write(val)
            raise SystemExit(0)
raise SystemExit(1)
PY
}

# If DATABASE_SCHEMA is set, keep it. On an existing stack, reuse live CloudFormation
# DatabaseSchema. On a new stack, infer syncbot_${stage}.
resolve_database_schema() {
  local stack="$1"
  local region="$2"
  local stage="$3"
  local live=""
  if [[ -n "${DATABASE_SCHEMA:-}" ]]; then
    return 0
  fi
  live="$(aws cloudformation describe-stacks \
    --stack-name "$stack" \
    --region "$region" \
    --query "Stacks[0].Parameters[?ParameterKey=='DatabaseSchema'].ParameterValue" \
    --output text 2>/dev/null || true)"
  live="${live//$'\r'/}"
  if [[ -n "$live" && "$live" != "None" ]]; then
    DATABASE_SCHEMA="$live"
    return 0
  fi
  DATABASE_SCHEMA="syncbot_${stage}"
}

require_database_credentials_for_backend() {
  if [[ "${DATABASE_BACKEND:-}" == "sqlite" ]]; then
    DATABASE_HOST=""
    DATABASE_USER="${DATABASE_USER:-}"
    DATABASE_PASSWORD="${DATABASE_PASSWORD:-}"
    return 0
  fi
  if [[ -z "${DATABASE_HOST:-}" ]]; then
    echo "Error: DATABASE_HOST is required when DATABASE_BACKEND=${DATABASE_BACKEND}." >&2
    return 1
  fi
  if [[ -z "${DATABASE_USER:-}" ]]; then
    echo "Error: DATABASE_USER is required when DATABASE_BACKEND=${DATABASE_BACKEND}." >&2
    return 1
  fi
  if [[ -z "${DATABASE_PASSWORD:-}" ]]; then
    echo "Error: DATABASE_PASSWORD is required when DATABASE_BACKEND=${DATABASE_BACKEND}." >&2
    return 1
  fi
}
