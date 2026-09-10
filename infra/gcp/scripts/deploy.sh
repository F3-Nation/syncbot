#!/usr/bin/env bash
# Interactive GCP deploy helper (Terraform). Run from repo root:
#   ./infra/gcp/scripts/deploy.sh
# Or via: ./deploy.sh --env test  (CLOUD_PROVIDER=gcp in .env.deploy.test)
#
# Non-interactive path (ENV_FILE_LOADED=true, from ./deploy.sh --env <stage>):
#   Sources .env.deploy.{stage}, builds TF vars from env, runs terraform init/plan/apply.
#
# Interactive path:
#   1) Prerequisites (terraform, gcloud, python3, curl, logged-in gcloud + ADC)
#   2) Project, region, stage; detect existing Cloud Run service
#   3) Deploy Tasks: multi-select menu (build/deploy, CI/CD, Slack API)
#   4) Configuration (if build/deploy): database, image, log level, terraform init/plan/apply
#   5) Post-tasks: Slack manifest/API, deploy receipt, print-bootstrap-outputs, GitHub Actions
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GCP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SLACK_MANIFEST_GENERATED_PATH=""

# shellcheck source=/dev/null
source "$REPO_ROOT/deploy.sh"
# Aliases GCP_DATABASE_MODE / DATABASE_ENGINE / EXISTING_DATABASE_HOST: see resolve_database_backend.sh.
# shellcheck source=/dev/null
source "$REPO_ROOT/infra/aws/scripts/resolve_database_backend.sh"

ensure_gcloud_authenticated() {
  local active_account adc_ok="false"
  active_account="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null || true)"
  active_account="${active_account%%$'\n'*}"
  if gcloud auth application-default print-access-token >/dev/null 2>&1; then
    adc_ok="true"
  fi
  if [[ -n "$active_account" && "$adc_ok" == "true" ]]; then
    echo "gcloud session: $active_account"
    return 0
  fi
  echo "Error: no active gcloud session." >&2
  echo "Log in, then rerun this script:" >&2
  if [[ -z "$active_account" ]]; then
    echo "  gcloud auth login" >&2
  fi
  if [[ "$adc_ok" != "true" ]]; then
    echo "  gcloud auth application-default login" >&2
  fi
  exit 1
}

echo "=== Prerequisites ==="
prereqs_require_cmd terraform prereqs_hint_terraform
prereqs_require_cmd gcloud prereqs_hint_gcloud
prereqs_require_cmd python3 prereqs_hint_python3
prereqs_require_cmd curl prereqs_hint_curl

prereqs_print_cli_status_matrix "GCP" terraform gcloud python3 curl
ensure_gcloud_authenticated

prompt_line() {
  local p="$1"
  local d="${2:-}"
  local v
  if [[ -n "$d" ]]; then
    read -r -p "$p [$d]: " v
    echo "${v:-$d}"
  else
    read -r -p "$p: " v
    echo "$v"
  fi
}

prompt_secret() {
  local p="$1"
  local v
  read -r -s -p "$p: " v
  printf '\n' >&2
  echo "$v"
}

prompt_required() {
  local p="$1"
  local v
  while true; do
    read -r -p "$p: " v
    if [[ -n "$v" ]]; then
      echo "$v"
      return 0
    fi
    echo "Error: $p is required." >&2
  done
}

required_from_env_or_prompt() {
  local env_name="$1"
  local prompt="$2"
  local mode="${3:-plain}" # plain|secret
  local env_value="${!env_name:-}"
  if [[ -n "$env_value" ]]; then
    echo "Using $prompt from environment variable $env_name." >&2
    echo "$env_value"
    return 0
  fi
  if [[ "$mode" == "secret" ]]; then
    while true; do
      env_value="$(prompt_secret "$prompt")"
      if [[ -n "$env_value" ]]; then
        echo "$env_value"
        return 0
      fi
      echo "Error: $prompt is required." >&2
    done
  fi
  prompt_required "$prompt"
}

prompt_yn() {
  local p="$1"
  local def="${2:-y}"
  local a
  local hint="y/N"
  [[ "$def" == "y" ]] && hint="Y/n"
  read -r -p "$p [$hint]: " a
  if [[ -z "$a" ]]; then
    a="$def"
  fi
  [[ "$a" =~ ^[Yy]$ ]]
}

ensure_gh_authenticated() {
  if ! command -v gh >/dev/null 2>&1; then
    prereqs_hint_gh_cli >&2
    return 1
  fi
  if gh auth status >/dev/null 2>&1; then
    return 0
  fi
  echo "gh CLI is not authenticated."
  if prompt_yn "Run 'gh auth login' now?" "y"; then
    gh auth login || true
  fi
  if gh auth status >/dev/null 2>&1; then
    return 0
  fi
  echo "gh authentication is still missing. Skipping automatic GitHub setup."
  return 1
}

# Aliases GCP_DATABASE_MODE / DATABASE_ENGINE: resolve_database_backend.sh (remove in 2.0.0).

cloud_run_env_value() {
  local project_id="$1"
  local region="$2"
  local service_name="$3"
  local env_key="$4"
  gcloud run services describe "$service_name" \
    --project "$project_id" \
    --region "$region" \
    --format=json 2>/dev/null | python3 - "$env_key" <<'PY'
import json
import sys

env_key = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    print("")
    raise SystemExit(0)

containers = (data.get("spec", {}) or {}).get("template", {}).get("spec", {}).get("containers", [])
for c in containers:
    for e in c.get("env", []) or []:
        if e.get("name") == env_key:
            print(e.get("value", ""))
            raise SystemExit(0)
print("")
PY
}

cloud_run_image_value() {
  local project_id="$1"
  local region="$2"
  local service_name="$3"
  gcloud run services describe "$service_name" \
    --project "$project_id" \
    --region "$region" \
    --format='value(spec.template.spec.containers[0].image)' 2>/dev/null || true
}

slack_manifest_json_compact() {
  local manifest_file="$1"
  python3 - "$manifest_file" <<'PY'
import json
import sys
path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)
print(json.dumps(data, separators=(",", ":")))
PY
}

slack_api_configure_from_manifest() {
  local manifest_file="$1"
  local install_url="$2"
  local token app_id team_id manifest_json api_resp ok

  echo
  echo "=== Slack App API ==="

  token="$(required_from_env_or_prompt "SLACK_API_TOKEN" "Slack API token (required scopes: apps.manifest:write)" "secret")"
  app_id="$(prompt_line "Slack App ID (optional; blank = create new app)" "${SLACK_APP_ID:-}")"
  team_id="$(prompt_line "Slack Team ID (optional; usually blank)" "${SLACK_TEAM_ID:-}")"

  manifest_json="$(slack_manifest_json_compact "$manifest_file" 2>/dev/null || true)"
  if [[ -z "$manifest_json" ]]; then
    echo "Could not parse manifest JSON automatically."
    echo "Ensure $manifest_file is valid JSON and Python 3 is installed."
    return 0
  fi

  if [[ -n "$app_id" ]]; then
    if [[ -n "$team_id" ]]; then
      api_resp="$(curl -sS -X POST \
        -H "Authorization: Bearer $token" \
        --data-urlencode "app_id=$app_id" \
        --data-urlencode "team_id=$team_id" \
        --data-urlencode "manifest=$manifest_json" \
        "https://slack.com/api/apps.manifest.update" || true)"
    else
      api_resp="$(curl -sS -X POST \
        -H "Authorization: Bearer $token" \
        --data-urlencode "app_id=$app_id" \
        --data-urlencode "manifest=$manifest_json" \
        "https://slack.com/api/apps.manifest.update" || true)"
    fi
    ok="$(python3 - "$api_resp" <<'PY'
import json,sys
try:
    data=json.loads(sys.argv[1])
except Exception:
    print("invalid-json")
    sys.exit(0)
print("ok" if data.get("ok") else f"error:{data.get('error','unknown_error')}")
PY
)"
    if [[ "$ok" == "ok" ]]; then
      echo "Slack app manifest updated for App ID: $app_id"
      echo "Open install URL: $install_url"
    else
      echo "Slack API update failed: ${ok#error:}"
      echo "Response (truncated):"
      slack_api_echo_truncated_body "$api_resp"
      echo "Hint: check token scopes (apps.manifest:write), manifest JSON, and api.slack.com methods apps.manifest.update"
    fi
    return 0
  fi

  if [[ -n "$team_id" ]]; then
    api_resp="$(curl -sS -X POST \
      -H "Authorization: Bearer $token" \
      --data-urlencode "team_id=$team_id" \
      --data-urlencode "manifest=$manifest_json" \
      "https://slack.com/api/apps.manifest.create" || true)"
  else
    api_resp="$(curl -sS -X POST \
      -H "Authorization: Bearer $token" \
      --data-urlencode "manifest=$manifest_json" \
      "https://slack.com/api/apps.manifest.create" || true)"
  fi
  ok="$(python3 - "$api_resp" <<'PY'
import json,sys
try:
    data=json.loads(sys.argv[1])
except Exception:
    print("invalid-json")
    sys.exit(0)
if not data.get("ok"):
    print(f"error:{data.get('error','unknown_error')}")
    sys.exit(0)
app_id = data.get("app_id") or (data.get("app", {}) or {}).get("id") or ""
print(f"ok:{app_id}")
PY
)"
  if [[ "$ok" == ok:* ]]; then
    app_id="${ok#ok:}"
    echo "Slack app created successfully."
    [[ -n "$app_id" ]] && echo "New Slack App ID: $app_id"
    echo "Open install URL: $install_url"
  else
    echo "Slack API create failed: ${ok#error:}"
    echo "Response (truncated):"
    slack_api_echo_truncated_body "$api_resp"
    echo "Hint: check token scopes (apps.manifest:write), manifest JSON, and api.slack.com methods apps.manifest.create"
  fi
}

generate_stage_slack_manifest() {
  local stage="$1"
  local api_url="$2"
  local install_url="$3"
  local template="$REPO_ROOT/slack-manifest.json"
  local manifest_out="$REPO_ROOT/slack-manifest_${stage}.json"
  local events_url base_url oauth_redirect_url

  if [[ ! -f "$template" ]]; then
    echo "Slack manifest template not found at $template"
    return 0
  fi
  if [[ -z "$api_url" ]]; then
    echo "Could not determine API URL from service outputs. Skipping Slack manifest generation."
    return 0
  fi

  events_url="${api_url%/}"
  base_url="${events_url%/slack/events}"
  oauth_redirect_url="${base_url}/slack/oauth_redirect"

  if ! python3 - "$template" "$manifest_out" "$events_url" "$oauth_redirect_url" <<'PY'
import json
import sys

template_path, out_path, events_url, redirect_url = sys.argv[1:5]
with open(template_path, "r", encoding="utf-8") as f:
    manifest = json.load(f)

manifest.setdefault("oauth_config", {}).setdefault("redirect_urls", [])
manifest["oauth_config"]["redirect_urls"] = [redirect_url]
manifest.setdefault("settings", {}).setdefault("event_subscriptions", {})
manifest["settings"]["event_subscriptions"]["request_url"] = events_url
manifest.setdefault("settings", {}).setdefault("interactivity", {})
manifest["settings"]["interactivity"]["request_url"] = events_url

with open(out_path, "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2)
    f.write("\n")
PY
  then
    echo "Failed to generate stage Slack manifest from JSON template."
    return 0
  fi

  SLACK_MANIFEST_GENERATED_PATH="$manifest_out"

  echo "=== Slack Manifest (${stage}) ==="
  echo "Saved file: $manifest_out"
  echo "Install URL: $install_url"
  echo
  sed 's/^/  /' "$manifest_out"
}

write_deploy_receipt() {
  local ts_human ts_file receipt_dir receipt_path
  local api_url="${SYNCBOT_API_URL:-}"
  local base_url="${api_url%/slack/events}"
  local oauth_redirect_url=""
  [[ -n "$base_url" ]] && oauth_redirect_url="${base_url}/slack/oauth_redirect"

  ts_human="$(date -u +"%Y-%m-%d %H:%M:%S UTC")"
  ts_file="$(date -u +"%Y%m%dT%H%M%SZ")"
  receipt_dir="$REPO_ROOT/deploy-receipts"
  receipt_path="$receipt_dir/deploy-gcp-${STAGE}-${ts_file}.md"

  mkdir -p "$receipt_dir"
  {
    cat <<EOF
# SyncBot Deploy Receipt

- Provider: gcp
- Stage: $STAGE
- Timestamp: $ts_human
- Project/Stack: $PROJECT_ID
- Region: $REGION

## Slack URLs
- Events/API URL: ${api_url:-n/a}
- Install URL: ${SYNCBOT_INSTALL_URL:-n/a}
- OAuth Redirect URL: ${oauth_redirect_url:-n/a}
- Slack Manifest: ${SLACK_MANIFEST_GENERATED_PATH:-n/a}

## Configuration
- GCP_PROJECT_ID=$PROJECT_ID
- DATABASE_BACKEND=${DATABASE_BACKEND:-}
- GCP_CLOUD_RUN_MIN_INSTANCES=${GCP_CLOUD_RUN_MIN_INSTANCES:-0}
- ENABLE_KEEP_WARM=${ENABLE_KEEP_WARM:-true}
- GCP_CLOUD_RUN_IMAGE=${CLOUD_IMAGE:-}
- DATABASE_SCHEMA=${DATABASE_SCHEMA:-}
- DATABASE_HOST=${DATABASE_HOST:-}
- DATABASE_PORT=${DATABASE_PORT:-}
- DATABASE_USER=${DATABASE_USER:-}
- DATABASE_TLS_ENABLED=${DATABASE_TLS_ENABLED:-}
- LOG_LEVEL=${LOG_LEVEL:-INFO}
- PRIMARY_WORKSPACE=${PRIMARY_WORKSPACE:-}
- SLACK_CLIENT_ID=${SLACK_CLIENT_ID:-}
- ENABLE_DB_RESET=${ENABLE_DB_RESET:-false}

## Secrets
- SLACK_SIGNING_SECRET=${SLACK_SIGNING_SECRET:-}
- SLACK_CLIENT_SECRET=${SLACK_CLIENT_SECRET:-}
- DATA_ENCRYPTION_KEY=${DATA_ENCRYPTION_KEY:-}
- DATABASE_PASSWORD=${DATABASE_PASSWORD:-}
EOF

    if [[ "${VERBOSE:-}" == "true" ]]; then
      echo ""
      echo "## Terraform Variables"
      if [[ ${#VARS[@]} -gt 0 ]]; then
        local v
        for v in "${VARS[@]}"; do
          echo "- $v"
        done
      else
        echo "(VARS array not available)"
      fi
      echo ""
      echo "## Slack Manifest (inline)"
      if [[ -n "${SLACK_MANIFEST_GENERATED_PATH:-}" && -f "${SLACK_MANIFEST_GENERATED_PATH:-}" ]]; then
        echo '```json'
        cat "$SLACK_MANIFEST_GENERATED_PATH"
        echo '```'
      else
        echo "(no manifest file generated)"
      fi
    fi
  } >"$receipt_path"

  echo "Deploy receipt written: $receipt_path"
  if [[ "${VERBOSE:-}" == "true" ]]; then
    echo "--- receipt contents ---"
    cat "$receipt_path"
    echo "--- end receipt ---"
  fi
}

push_github_gcp_wif() {
  local repo="$1"
  local env_name="$2"
  local project_id="$3"
  local region="$4"
  local deploy_sa="${5:-}"
  local wif="${6:-}"

  gh api -X PUT "repos/$repo/environments/$env_name" >/dev/null 2>&1 || true
  gh variable set GCP_PROJECT_ID --body "$project_id" -R "$repo"
  gh variable set GCP_REGION --body "$region" -R "$repo"
  gh variable set GITHUB_DEPLOY_TARGET --body "gcp" -R "$repo"
  [[ -n "$deploy_sa" ]] && gh variable set GCP_SERVICE_ACCOUNT --body "$deploy_sa" -R "$repo"
  [[ -n "$wif" ]] && gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER --body "$wif" -R "$repo"
}

configure_github_actions_gcp() {
  # $1 GCP project ID
  # $2 GCP region (e.g. us-central1)
  # $3 Path to infra/gcp (terraform directory)
  # $4 Deploy stage (test|prod) — GitHub environment name
  local gcp_project_id="$1"
  local gcp_region="$2"
  local terraform_dir="$3"
  local deploy_stage="$4"
  local deploy_sa_email artifact_registry_url service_url
  local repo env_name
  env_name="$deploy_stage"

  deploy_sa_email="$(cd "$terraform_dir" && terraform output -raw deploy_service_account_email 2>/dev/null || true)"
  artifact_registry_url="$(cd "$terraform_dir" && terraform output -raw artifact_registry_repository 2>/dev/null || true)"
  service_url="$(cd "$terraform_dir" && terraform output -raw service_url 2>/dev/null || true)"

  echo
  echo "=== GitHub Actions (GCP) ==="
  echo "Detected project:         $gcp_project_id"
  echo "Detected region:          $gcp_region"
  echo "Detected service account: $deploy_sa_email"
  echo "Detected artifact repo:   $artifact_registry_url"
  echo "Detected service URL:     $service_url"
  repo="$(prompt_github_repo_for_actions "$REPO_ROOT")"

  if ! ensure_gh_authenticated; then
    echo
    echo "Set these GitHub Actions Variables manually:"
    echo "  GCP_PROJECT_ID   = $gcp_project_id"
    echo "  GCP_REGION       = $gcp_region"
    echo "  GCP_SERVICE_ACCOUNT = $deploy_sa_email"
    echo "  GITHUB_DEPLOY_TARGET = gcp"
    echo "Also set GCP_WORKLOAD_IDENTITY_PROVIDER from terraform output workload_identity_provider."
    return 0
  fi

  if prompt_yn "Create/update GitHub environments 'test' and 'prod' now?" "y"; then
    gh api -X PUT "repos/$repo/environments/test" >/dev/null
    gh api -X PUT "repos/$repo/environments/prod" >/dev/null
    echo "GitHub environments ensured: test, prod."
  fi

  if prompt_yn "Set repo variables with gh now (GCP_PROJECT_ID, GCP_REGION, GCP_SERVICE_ACCOUNT, GITHUB_DEPLOY_TARGET=gcp)?" "y"; then
    wif="$(cd "$terraform_dir" && terraform output -raw workload_identity_provider 2>/dev/null || true)"
    push_github_gcp_wif "$repo" "$env_name" "$gcp_project_id" "$gcp_region" "$deploy_sa_email" "$wif"
    echo "GitHub repository variables updated."
    if [[ -z "$wif" ]]; then
      echo "GCP_WORKLOAD_IDENTITY_PROVIDER is empty — re-apply Terraform with GITHUB_REPO=owner/repo (this GitHub repo)."
    fi
  fi
}

# ====================================================================
# Non-interactive fast path (./deploy.sh --env test|prod)
# ====================================================================
if [[ "${ENV_FILE_LOADED:-}" == "true" ]]; then
  echo "=== SyncBot GCP Deploy (non-interactive) ==="
  apply_gcp_provider_env_aliases
  if [[ "${BOOTSTRAP:-}" == "true" ]]; then
    echo "Note: --bootstrap is AWS-only (GCP uses a single terraform apply). Ignoring."
  fi
  PROJECT_ID="${GCP_PROJECT_ID:?GCP_PROJECT_ID required in env file}"
  REGION="${GCP_REGION:-us-central1}"
  STAGE="${STAGE:?STAGE required}"
  CLOUD_IMAGE="${GCP_CLOUD_RUN_IMAGE:-}"
  resolve_database_backend gcp
  require_database_credentials_for_backend
  GCP_CLOUD_RUN_MIN_INSTANCES="${GCP_CLOUD_RUN_MIN_INSTANCES:-0}"
  ENABLE_KEEP_WARM="${ENABLE_KEEP_WARM:-true}"
  GITHUB_REPO="${GITHUB_REPO:-}"

  gcloud config set project "$PROJECT_ID" >/dev/null 2>&1 || true

  DATA_ENCRYPTION_KEY="${DATA_ENCRYPTION_KEY:-${TOKEN_ENCRYPTION_KEY:-}}"
  USE_EXISTING="false"
  [[ "$DATABASE_BACKEND" != "sqlite" ]] && USE_EXISTING="true"

  if [[ -n "${ENV_FILE_PATH:-}" ]]; then
    update_env_file "$ENV_FILE_PATH" "DATABASE_BACKEND" "$DATABASE_BACKEND"
  fi

  # Auto-generate DATA_ENCRYPTION_KEY if empty
  if [[ -z "${DATA_ENCRYPTION_KEY:-}" ]]; then
    DATA_ENCRYPTION_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(36))')"
    echo "Generated DATA_ENCRYPTION_KEY=$DATA_ENCRYPTION_KEY"
    echo "IMPORTANT: Store this key securely. You need it for disaster recovery."
    if [[ -n "${ENV_FILE_PATH:-}" ]]; then
      update_env_file "$ENV_FILE_PATH" "DATA_ENCRYPTION_KEY" "$DATA_ENCRYPTION_KEY"
      echo "  (saved to $ENV_FILE_PATH)"
    fi
  fi

  if [[ -n "${ENV_FILE_PATH:-}" ]]; then
    update_env_file "$ENV_FILE_PATH" "GCP_CLOUD_RUN_MIN_INSTANCES" "$GCP_CLOUD_RUN_MIN_INSTANCES"
    update_env_file "$ENV_FILE_PATH" "ENABLE_KEEP_WARM" "$ENABLE_KEEP_WARM"
  fi

  echo "=== Terraform Init ==="
  cd "$GCP_DIR"
  terraform init

  VARS=(
    "-var=project_id=$PROJECT_ID"
    "-var=region=$REGION"
    "-var=stage=$STAGE"
    "-var=log_level=${LOG_LEVEL:-INFO}"
    "-var=slack_signing_secret=${SLACK_SIGNING_SECRET:?SLACK_SIGNING_SECRET required}"
    "-var=slack_client_id=${SLACK_CLIENT_ID:?SLACK_CLIENT_ID required}"
    "-var=slack_client_secret=${SLACK_CLIENT_SECRET:?SLACK_CLIENT_SECRET required}"
    "-var=data_encryption_key=${DATA_ENCRYPTION_KEY:?DATA_ENCRYPTION_KEY required}"
    "-var=database_backend=$DATABASE_BACKEND"
    "-var=cloud_run_min_instances=${GCP_CLOUD_RUN_MIN_INSTANCES}"
    "-var=enable_keep_warm=${ENABLE_KEEP_WARM}"
    "-var=github_repo=${GITHUB_REPO}"
  )
  [[ -n "${DATABASE_PORT:-}" ]] && VARS+=("-var=database_port=$DATABASE_PORT")
  [[ -n "$CLOUD_IMAGE" ]] && VARS+=("-var=cloud_run_image=$CLOUD_IMAGE")
  [[ -n "${DATABASE_USER:-}" ]] && VARS+=("-var=database_user=$DATABASE_USER")
  [[ -n "${PRIMARY_WORKSPACE:-}" ]] && VARS+=("-var=primary_workspace=$PRIMARY_WORKSPACE")
  [[ -n "${ENABLE_DB_RESET:-}" ]] && VARS+=("-var=enable_db_reset=$ENABLE_DB_RESET")
  [[ -n "${DATABASE_TLS_ENABLED:-}" ]] && VARS+=("-var=database_tls_enabled=$DATABASE_TLS_ENABLED")
  [[ -n "${DATABASE_SSL_CA_PATH:-}" ]] && VARS+=("-var=database_ssl_ca_path=$DATABASE_SSL_CA_PATH")
  [[ -n "${SLACK_BOT_SCOPES:-}" ]] && VARS+=("-var=slack_bot_scopes=$SLACK_BOT_SCOPES")
  [[ -n "${SLACK_USER_SCOPES:-}" ]] && VARS+=("-var=slack_user_scopes=$SLACK_USER_SCOPES")

  if [[ "$USE_EXISTING" == "true" ]]; then
    if [[ -z "${DATABASE_HOST:-}" ]]; then
      echo "Error: DATABASE_HOST is required when DATABASE_BACKEND=${DATABASE_BACKEND}." >&2
      exit 1
    fi
    if [[ -z "${DATABASE_PASSWORD:-}" ]]; then
      echo "Error: DATABASE_PASSWORD is required when DATABASE_BACKEND=${DATABASE_BACKEND}." >&2
      exit 1
    fi
    if [[ -z "${DATABASE_USER:-}" ]]; then
      echo "Error: DATABASE_USER is required when DATABASE_BACKEND=${DATABASE_BACKEND}." >&2
      exit 1
    fi
    VARS+=("-var=database_password=$DATABASE_PASSWORD")
    VARS+=("-var=database_host=$DATABASE_HOST")
    VARS+=("-var=database_schema=${DATABASE_SCHEMA:-syncbot_${STAGE}}")
    VARS+=("-var=database_user=$DATABASE_USER")
  fi

  echo "=== Terraform Plan ==="
  terraform plan "${VARS[@]}"

  echo "=== Terraform Apply ==="
  terraform apply -auto-approve "${VARS[@]}"

  SERVICE_URL="$(terraform output -raw service_url 2>/dev/null || true)"
  SYNCBOT_API_URL=""
  SYNCBOT_INSTALL_URL=""
  if [[ -n "$SERVICE_URL" ]]; then
    SYNCBOT_API_URL="${SERVICE_URL%/}/slack/events"
    SYNCBOT_INSTALL_URL="${SERVICE_URL%/}/slack/install"
  fi
  generate_stage_slack_manifest "$STAGE" "$SYNCBOT_API_URL" "$SYNCBOT_INSTALL_URL"

  if [[ "${SETUP_GITHUB:-}" == "true" ]]; then
    echo
    echo "=== GitHub Setup (non-interactive) ==="
    REPO="$(cd "$REPO_ROOT" && gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)"
    if [[ -z "$REPO" ]]; then
      echo "Warning: could not detect GitHub repo; skipping --setup-github." >&2
    else
      ENV_NAME="$STAGE"
      DEPLOY_SA="$(terraform output -raw deploy_service_account_email 2>/dev/null || true)"
      WIF_PROVIDER="$(terraform output -raw workload_identity_provider 2>/dev/null || true)"
      push_github_gcp_wif "$REPO" "$ENV_NAME" "$PROJECT_ID" "$REGION" "$DEPLOY_SA" "$WIF_PROVIDER"
      echo "GitHub repository variables updated (image-only CI; Slack secrets stay on Cloud Run from terraform apply)."
    fi
  fi

  echo
  echo "=== Deploy Receipt ==="
  write_deploy_receipt

  echo
  echo "=== Deploy Complete ==="
  echo "Project:     $PROJECT_ID"
  echo "Region:      $REGION"
  echo "Service URL: ${SERVICE_URL:-n/a}"
  echo "API URL:     ${SYNCBOT_API_URL:-n/a}"
  echo "Install URL: ${SYNCBOT_INSTALL_URL:-n/a}"
  if [[ -n "${SYNCBOT_API_URL:-}" ]]; then
    echo "OAuth URL:   ${SYNCBOT_API_URL%/slack/events}/slack/oauth_redirect"
  fi
  exit 0
fi

# ====================================================================
# Interactive deploy path
# ====================================================================
echo "=== SyncBot GCP Deploy ==="
echo "Working directory: $GCP_DIR"
echo

# Backward-compatible aliases: new name primary, EXISTING_ as fallback (same as non-interactive path)
DATABASE_HOST="${DATABASE_HOST:-${EXISTING_DATABASE_HOST:-}}"
DATA_ENCRYPTION_KEY="${DATA_ENCRYPTION_KEY:-${TOKEN_ENCRYPTION_KEY:-}}"
apply_gcp_provider_env_aliases

echo "=== Project And Region ==="
PROJECT_ID="$(prompt_line "GCP project_id" "${GCP_PROJECT_ID:-}")"
if [[ -z "$PROJECT_ID" ]]; then
  echo "Error: project_id is required." >&2
  exit 1
fi

REGION="$(prompt_line "GCP region" "${GCP_REGION:-us-central1}")"
gcloud config set project "$PROJECT_ID" >/dev/null 2>&1 || true
STAGE="$(prompt_line "Stage (test/prod)" "${STAGE:-test}")"
if [[ "$STAGE" != "test" && "$STAGE" != "prod" ]]; then
  echo "Error: stage must be 'test' or 'prod'." >&2
  exit 1
fi
GCP_CLOUD_RUN_MIN_INSTANCES="${GCP_CLOUD_RUN_MIN_INSTANCES:-0}"
ENABLE_KEEP_WARM="${ENABLE_KEEP_WARM:-true}"
CLOUD_IMAGE="${GCP_CLOUD_RUN_IMAGE:-}"
DATABASE_BACKEND="${DATABASE_BACKEND:-sqlite}"
USE_EXISTING="false"
[[ "$DATABASE_BACKEND" != "sqlite" ]] && USE_EXISTING="true"
VARS=()
DB_PORT="${DATABASE_PORT:-}"
SERVICE_NAME="syncbot-${STAGE}"
EXISTING_SERVICE_URL="$(gcloud run services describe "$SERVICE_NAME" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --format='value(status.url)' 2>/dev/null || true)"
if [[ -n "$EXISTING_SERVICE_URL" ]]; then
  echo "Detected existing Cloud Run service: $SERVICE_NAME"
  if ! prompt_yn "Continue and update this existing deployment?" "y"; then
    echo "Aborted."
    exit 0
  fi
fi

echo
prompt_deploy_tasks_gcp

if [[ "$TASK_BUILD_DEPLOY" != "true" ]]; then
  if [[ "$TASK_CICD" == "true" || "$TASK_SLACK_API" == "true" ]]; then
    cd "$GCP_DIR"
    if ! terraform output -raw service_url &>/dev/null; then
      echo "Error: No Terraform outputs found in $GCP_DIR. Select task 1 (Build/Deploy) first." >&2
      exit 1
    fi
  fi
fi

if [[ "$TASK_BUILD_DEPLOY" == "true" ]]; then
echo
echo "=== Configuration ==="
echo "=== Database ==="
echo "  1) SQLite + Litestream (default)"
echo "  2) MySQL (TiDB / your host). Cloud SQL is not created."
echo "  3) PostgreSQL"
DATABASE_BACKEND="sqlite"
USE_EXISTING="false"
DB_BACKEND_DEFAULT="1"
DB_CHOICE="$(prompt_line "Choose database (1, 2, or 3)" "$DB_BACKEND_DEFAULT")"
case "$DB_CHOICE" in
  1) DATABASE_BACKEND="sqlite"; USE_EXISTING="false" ;;
  2) DATABASE_BACKEND="mysql"; USE_EXISTING="true" ;;
  3) DATABASE_BACKEND="postgresql"; USE_EXISTING="true" ;;
  *)
    echo "Error: invalid database choice." >&2
    exit 1
    ;;
esac
DB_BACKEND="$DATABASE_BACKEND"

echo
echo "=== Cloud Run warmth ==="
echo "min_instances=0 (default) is free. Cold starts are best-effort: Slack events are queued/retried"
echo "(sometimes slower). Interactivity may need a second click after a long idle."
echo "min_instances=1 is the only paid knob (~always-on Cloud Run) and guarantees Slack's 3s budget."
GCP_CLOUD_RUN_MIN_INSTANCES=0
if prompt_yn "Keep one Cloud Run instance always on (paid)?" "n"; then
  GCP_CLOUD_RUN_MIN_INSTANCES=1
fi
ENABLE_KEEP_WARM="true"
if ! prompt_yn "Enable keep-warm Scheduler ping of /health every 5 minutes (free, recommended)?" "y"; then
  ENABLE_KEEP_WARM="false"
fi

GITHUB_REPO="${GITHUB_REPO:-}"
if [[ "$TASK_CICD" == "true" && -z "$GITHUB_REPO" ]]; then
  GITHUB_REPO="$(prompt_github_repo_for_actions "$REPO_ROOT")"
fi

EXISTING_HOST=""
EXISTING_SCHEMA=""
EXISTING_USER=""
DETECTED_EXISTING_HOST=""
DETECTED_EXISTING_SCHEMA=""
DETECTED_EXISTING_USER=""
if [[ -n "$EXISTING_SERVICE_URL" ]]; then
  DETECTED_EXISTING_HOST="$(cloud_run_env_value "$PROJECT_ID" "$REGION" "$SERVICE_NAME" "DATABASE_HOST")"
  DETECTED_EXISTING_SCHEMA="$(cloud_run_env_value "$PROJECT_ID" "$REGION" "$SERVICE_NAME" "DATABASE_SCHEMA")"
  DETECTED_EXISTING_USER="$(cloud_run_env_value "$PROJECT_ID" "$REGION" "$SERVICE_NAME" "DATABASE_USER")"
fi
if [[ "$USE_EXISTING" == "true" ]]; then
  EXISTING_HOST="$(prompt_line "Existing database host" "$DETECTED_EXISTING_HOST")"
  EXISTING_SCHEMA="$(prompt_line "Database schema name" "${DETECTED_EXISTING_SCHEMA:-syncbot_${STAGE}}")"
  EXISTING_USER="$(prompt_line "Database user (full username, including any TiDB prefix)" "$DETECTED_EXISTING_USER")"
  if [[ -z "$EXISTING_HOST" ]]; then
    echo "Error: DATABASE_HOST is required when DATABASE_BACKEND=${DATABASE_BACKEND}." >&2
    exit 1
  fi
  if [[ -z "$EXISTING_USER" ]]; then
    echo "Error: DATABASE_USER is required when DATABASE_BACKEND=${DATABASE_BACKEND}." >&2
    exit 1
  fi

  echo
  echo "=== Database port ==="
  echo "Leave port blank to use the engine default (3306 MySQL, 5432 PostgreSQL). TiDB Cloud uses 4000."
  DEFAULT_DB_PORT="${DATABASE_PORT:-}"
  if [[ -n "$EXISTING_SERVICE_URL" ]]; then
    DETECTED_DB_PORT_EARLY="$(cloud_run_env_value "$PROJECT_ID" "$REGION" "$SERVICE_NAME" "DATABASE_PORT")"
    [[ -n "$DETECTED_DB_PORT_EARLY" ]] && DEFAULT_DB_PORT="$DETECTED_DB_PORT_EARLY"
  fi
  DB_PORT="$(prompt_line "DATABASE_PORT (optional)" "$DEFAULT_DB_PORT")"
fi

DETECTED_CLOUD_IMAGE=""
if [[ -n "$EXISTING_SERVICE_URL" ]]; then
  DETECTED_CLOUD_IMAGE="$(cloud_run_image_value "$PROJECT_ID" "$REGION" "$SERVICE_NAME")"
fi
echo
echo "=== Container Image ==="
echo "Blank uses the public hello placeholder. CI replaces the live image (terraform ignores image changes)."
CLOUD_IMAGE="$(prompt_line "GCP_CLOUD_RUN_IMAGE" "${GCP_CLOUD_RUN_IMAGE:-$DETECTED_CLOUD_IMAGE}")"

DETECTED_LOG_LEVEL=""
if [[ -n "$EXISTING_SERVICE_URL" ]]; then
  DETECTED_LOG_LEVEL="$(cloud_run_env_value "$PROJECT_ID" "$REGION" "$SERVICE_NAME" "LOG_LEVEL")"
fi
LOG_LEVEL_DEFAULT="INFO"
if [[ -n "$DETECTED_LOG_LEVEL" ]]; then
  LOG_LEVEL_DEFAULT="$(normalize_log_level "$DETECTED_LOG_LEVEL")"
  if ! is_valid_log_level "$LOG_LEVEL_DEFAULT"; then
    LOG_LEVEL_DEFAULT="INFO"
  fi
fi

echo
echo "=== Log Level ==="
LOG_LEVEL="$(prompt_log_level "$LOG_LEVEL_DEFAULT")"

# Preserve optional runtime env on redeploy (Terraform defaults otherwise).
PRIMARY_WORKSPACE_VAR=""
ENABLE_DB_RESET_VAR=""
DB_TLS_VAR=""
DB_SSL_CA_VAR=""
DB_BACKEND="${DATABASE_BACKEND:-sqlite}"
if [[ -n "$EXISTING_SERVICE_URL" ]]; then
  DETECTED_PW="$(cloud_run_env_value "$PROJECT_ID" "$REGION" "$SERVICE_NAME" "PRIMARY_WORKSPACE")"
  PRIMARY_WORKSPACE_VAR="${DETECTED_PW:-}"
  DETECTED_ER="$(cloud_run_env_value "$PROJECT_ID" "$REGION" "$SERVICE_NAME" "ENABLE_DB_RESET")"
  ENABLE_DB_RESET_VAR="${DETECTED_ER:-}"
  DETECTED_DB_TLS="$(cloud_run_env_value "$PROJECT_ID" "$REGION" "$SERVICE_NAME" "DATABASE_TLS_ENABLED")"
  DB_TLS_VAR="${DETECTED_DB_TLS:-}"
  DETECTED_DB_SSL_CA="$(cloud_run_env_value "$PROJECT_ID" "$REGION" "$SERVICE_NAME" "DATABASE_SSL_CA_PATH")"
  DB_SSL_CA_VAR="${DETECTED_DB_SSL_CA:-}"
  DETECTED_DB_BACKEND="$(cloud_run_env_value "$PROJECT_ID" "$REGION" "$SERVICE_NAME" "DATABASE_BACKEND")"
  [[ -n "$DETECTED_DB_BACKEND" ]] && DB_BACKEND="$DETECTED_DB_BACKEND"
fi

echo
echo "=== App Settings ==="
PRIMARY_WORKSPACE_VAR="$(prompt_primary_workspace "$PRIMARY_WORKSPACE_VAR")"

echo
echo "=== App Secrets ==="
echo "Secrets are passed directly as sensitive Terraform variables."

if [[ -z "${DATA_ENCRYPTION_KEY:-}" ]]; then
  DATA_ENCRYPTION_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(36))')"
  echo "Generated DATA_ENCRYPTION_KEY=$DATA_ENCRYPTION_KEY"
  echo "IMPORTANT: Store this key securely. You need it for disaster recovery."
fi

SLACK_SIGNING_SECRET="$(required_from_env_or_prompt "SLACK_SIGNING_SECRET" "SlackSigningSecret" "secret")"
SLACK_CLIENT_ID="$(required_from_env_or_prompt "SLACK_CLIENT_ID" "SlackClientID")"
SLACK_CLIENT_SECRET="$(required_from_env_or_prompt "SLACK_CLIENT_SECRET" "SlackClientSecret" "secret")"
DATA_ENCRYPTION_KEY="$(required_from_env_or_prompt "DATA_ENCRYPTION_KEY" "DataEncryptionKey" "secret")"
DATABASE_PASSWORD=""
DATABASE_USER="${DATABASE_USER:-}"
if [[ "$USE_EXISTING" == "true" ]]; then
  DATABASE_PASSWORD="$(required_from_env_or_prompt "DATABASE_PASSWORD" "DatabasePassword" "secret")"
  DATABASE_USER="${DATABASE_USER:-$EXISTING_USER}"
  if [[ -z "$DATABASE_USER" ]]; then
    DATABASE_USER="$(required_from_env_or_prompt "DATABASE_USER" "DatabaseUser (full username, including any TiDB prefix)")"
  fi
fi

echo
echo "=== Terraform Init ==="
echo "Running: terraform init"
cd "$GCP_DIR"
terraform init

VARS=(
  "-var=project_id=$PROJECT_ID"
  "-var=region=$REGION"
  "-var=stage=$STAGE"
  "-var=log_level=$LOG_LEVEL"
  "-var=primary_workspace=${PRIMARY_WORKSPACE_VAR:-}"
  "-var=enable_db_reset=${ENABLE_DB_RESET_VAR:-}"
  "-var=database_tls_enabled=${DB_TLS_VAR:-}"
  "-var=database_ssl_ca_path=${DB_SSL_CA_VAR:-}"
  "-var=database_backend=$DATABASE_BACKEND"
  "-var=cloud_run_min_instances=$GCP_CLOUD_RUN_MIN_INSTANCES"
  "-var=enable_keep_warm=$ENABLE_KEEP_WARM"
  "-var=github_repo=${GITHUB_REPO:-}"
  "-var=slack_signing_secret=$SLACK_SIGNING_SECRET"
  "-var=slack_client_id=$SLACK_CLIENT_ID"
  "-var=slack_client_secret=$SLACK_CLIENT_SECRET"
  "-var=data_encryption_key=$DATA_ENCRYPTION_KEY"
)
[[ -n "${SLACK_BOT_SCOPES:-}" ]] && VARS+=("-var=slack_bot_scopes=$SLACK_BOT_SCOPES")
[[ -n "${SLACK_USER_SCOPES:-}" ]] && VARS+=("-var=slack_user_scopes=$SLACK_USER_SCOPES")
[[ -n "${DB_PORT:-}" ]] && VARS+=("-var=database_port=$DB_PORT")
[[ -n "$CLOUD_IMAGE" ]] && VARS+=("-var=cloud_run_image=$CLOUD_IMAGE")
[[ -n "$DATABASE_USER" ]] && VARS+=("-var=database_user=$DATABASE_USER")
if [[ "$USE_EXISTING" == "true" ]]; then
  VARS+=("-var=database_password=$DATABASE_PASSWORD")
  VARS+=("-var=database_host=$EXISTING_HOST")
  VARS+=("-var=database_schema=$EXISTING_SCHEMA")
  VARS+=("-var=database_user=$EXISTING_USER")
fi

echo
echo "Log level:        $LOG_LEVEL"
if [[ -n "$PRIMARY_WORKSPACE_VAR" ]]; then
  echo "Primary workspace: $PRIMARY_WORKSPACE_VAR"
else
  echo "Primary workspace: (not set — backup/restore hidden)"
fi
if [[ "$ENABLE_DB_RESET_VAR" == "true" ]]; then
  echo "DB reset:          enabled"
else
  echo "DB reset:          (disabled)"
fi
echo
echo "=== Terraform Plan ==="
terraform plan "${VARS[@]}"

echo
echo "=== Terraform Apply ==="
terraform apply -auto-approve "${VARS[@]}"

echo
echo "=== Apply Complete ==="
SERVICE_URL="$(terraform output -raw service_url 2>/dev/null || true)"

else
  echo
  echo "Skipping Build/Deploy (task 1 not selected)."
  cd "$GCP_DIR"
  SERVICE_URL="$(terraform output -raw service_url 2>/dev/null || true)"
fi

SYNCBOT_API_URL=""
SYNCBOT_INSTALL_URL=""
if [[ -n "$SERVICE_URL" ]]; then
  SYNCBOT_API_URL="${SERVICE_URL%/}/slack/events"
  SYNCBOT_INSTALL_URL="${SERVICE_URL%/}/slack/install"
fi

echo
echo "=== Post-Deploy ==="
if [[ "$TASK_BUILD_DEPLOY" == "true" ]]; then
  echo "Deploy complete."
fi

if [[ "$TASK_SLACK_API" == "true" || "$TASK_BUILD_DEPLOY" == "true" ]]; then
  generate_stage_slack_manifest "$STAGE" "$SYNCBOT_API_URL" "$SYNCBOT_INSTALL_URL"
fi

if [[ "$TASK_SLACK_API" == "true" ]] && [[ -n "${SLACK_MANIFEST_GENERATED_PATH:-}" ]]; then
  slack_api_configure_from_manifest "$SLACK_MANIFEST_GENERATED_PATH" "$SYNCBOT_INSTALL_URL"
fi

if [[ "$TASK_BUILD_DEPLOY" == "true" ]]; then
  echo
  echo "Next:"
  echo "  1) Push to test/prod after setting GITHUB_DEPLOY_TARGET=gcp so CI builds infra/gcp/Dockerfile."
  echo "  2) Run: ./infra/gcp/scripts/print-bootstrap-outputs.sh"
  bash "$SCRIPT_DIR/print-bootstrap-outputs.sh" || true
fi

if [[ "$TASK_CICD" == "true" ]]; then
  configure_github_actions_gcp "$PROJECT_ID" "$REGION" "$GCP_DIR" "$STAGE"
fi

# --- Save config to env file ---
echo
if [[ "$TASK_BUILD_DEPLOY" == "true" ]] && prompt_yn "Save config to .env.deploy.${STAGE} for future deploys?" "y"; then
  ENV_SAVE_FILE="$REPO_ROOT/.env.deploy.${STAGE}"
  {
    echo "# Generated by deploy.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "CLOUD_PROVIDER=gcp"
    echo "GCP_PROJECT_ID=$PROJECT_ID"
    echo "GCP_REGION=$REGION"
    echo "DATABASE_BACKEND=${DATABASE_BACKEND:-sqlite}"
    echo "GCP_CLOUD_RUN_MIN_INSTANCES=${GCP_CLOUD_RUN_MIN_INSTANCES:-0}"
    echo "ENABLE_KEEP_WARM=${ENABLE_KEEP_WARM:-true}"
    [[ -n "${GITHUB_REPO:-}" ]] && echo "GITHUB_REPO=$GITHUB_REPO"
    echo "GCP_CLOUD_RUN_IMAGE=${CLOUD_IMAGE:-}"
    echo ""
    echo "SLACK_SIGNING_SECRET=${SLACK_SIGNING_SECRET:-}"
    echo "SLACK_CLIENT_SECRET=${SLACK_CLIENT_SECRET:-}"
    echo "SLACK_CLIENT_ID=${SLACK_CLIENT_ID:-}"
    echo ""
    echo "DATA_ENCRYPTION_KEY=${DATA_ENCRYPTION_KEY:-}"
    echo ""
    if [[ "${USE_EXISTING:-false}" == "true" ]]; then
      echo "DATABASE_HOST=${EXISTING_HOST:-${DATABASE_HOST:-}}"
      [[ -n "${DB_PORT:-}" ]] && echo "DATABASE_PORT=$DB_PORT"
      echo "DATABASE_USER=${DATABASE_USER:-}"
      echo "DATABASE_PASSWORD=${DATABASE_PASSWORD:-}"
      echo "DATABASE_SCHEMA=${EXISTING_SCHEMA:-${DATABASE_SCHEMA:-syncbot_${STAGE}}}"
    fi
  } > "$ENV_SAVE_FILE"
  chmod 600 "$ENV_SAVE_FILE"
  echo "Saved to $ENV_SAVE_FILE"
  echo "Next time: ./deploy.sh --env $STAGE"
fi

# --- Push to GitHub (if --setup-github and TASK_CICD was not already run) ---
if [[ "${SETUP_GITHUB:-}" == "true" && "${TASK_CICD:-}" != "true" ]]; then
  echo
  echo "=== Push to GitHub Environment ==="
  prereqs_require_cmd gh prereqs_hint_gh_cli
  if ! gh auth status >/dev/null 2>&1; then
    echo "Error: gh CLI not authenticated. Run 'gh auth login' first." >&2
    exit 1
  fi
  REPO="$(prompt_github_repo_for_actions "$REPO_ROOT")"
  ENV_NAME="$STAGE"
  DEPLOY_SA="$(terraform output -raw deploy_service_account_email 2>/dev/null || true)"
  WIF_PROVIDER="$(terraform output -raw workload_identity_provider 2>/dev/null || true)"
  push_github_gcp_wif "$REPO" "$ENV_NAME" "$PROJECT_ID" "$REGION" "$DEPLOY_SA" "$WIF_PROVIDER"
  echo "GitHub environment '$ENV_NAME' configured for repo $REPO (image-only CI)."
fi

echo
echo "=== Deploy Receipt ==="
write_deploy_receipt

echo
echo "=== Deploy Complete ==="
echo "Project:     $PROJECT_ID"
echo "Region:      $REGION"
echo "Service URL: ${SERVICE_URL:-n/a}"
echo "API URL:     ${SYNCBOT_API_URL:-n/a}"
echo "Install URL: ${SYNCBOT_INSTALL_URL:-n/a}"
if [[ -n "${SYNCBOT_API_URL:-}" ]]; then
  echo "OAuth URL:   ${SYNCBOT_API_URL%/slack/events}/slack/oauth_redirect"
fi
