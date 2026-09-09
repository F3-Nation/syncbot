#!/usr/bin/env bash
# Interactive AWS deploy helper for SyncBot.
# Handles: bootstrap (auto create/sync), sam build, sam deploy (SQL host or sqlite+Litestream).
#
# Run from repo root:
#   ./infra/aws/scripts/deploy.sh
# Or via: ./deploy.sh --env test  (CLOUD_PROVIDER=aws in .env.deploy.test)
#
# Non-interactive path (ENV_FILE_LOADED=true, from ./deploy.sh --env <stage>):
#   Sources .env.deploy.{stage}, ensures bootstrap, builds SAM params, sam build + deploy.
#   --bootstrap forces a bootstrap template sync even when the hash already matches.
#
# Interactive path (./deploy.sh without --env):
#   1) Prerequisites: CLI checks, active AWS session, template paths
#   2) Stack identity: region, stage, app stack name; detect existing stack for update
#   3) Bootstrap: create if missing; sync if template hash changed
#   4) Deploy Tasks: multi-select menu (build/deploy, CI/CD, Slack API)
#   5) Configuration (if build/deploy): database, Slack creds, SAM build + deploy
#   6) Post-tasks: Slack manifest/API, GitHub Actions, deploy receipt

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# shellcheck source=/dev/null
source "$SCRIPT_DIR/resolve_database_backend.sh"

BOOTSTRAP_TEMPLATE="$REPO_ROOT/infra/aws/template.bootstrap.yaml"
APP_TEMPLATE="$REPO_ROOT/infra/aws/template.yaml"
SLACK_MANIFEST_GENERATED_PATH=""

# shellcheck source=/dev/null
source "$REPO_ROOT/deploy.sh"

# ---------------------------------------------------------------------------
# SAM deploy with fallback to direct CloudFormation update-stack
# When sam deploy fails because changeset early validation rejects the update
# (e.g. AWS::EarlyValidation::ResourceExistenceCheck), retry with update-stack,
# which skips changeset creation. Optional --update-stack skips sam deploy.
# Uses globals: STACK_NAME, REGION, S3_BUCKET, PARAMS (update-stack converts PARAMS to JSON)
# ---------------------------------------------------------------------------
delete_failed_changesets() {
  local stack_name="$1" region="$2" names cs
  names="$(aws cloudformation list-change-sets \
    --stack-name "$stack_name" \
    --region "$region" \
    --query 'Summaries[?Status==`FAILED`].ChangeSetName' \
    --output text 2>/dev/null || true)"
  [[ -z "$names" || "$names" == "None" ]] && return 0
  for cs in $names; do
    [[ -z "$cs" ]] && continue
    aws cloudformation delete-change-set \
      --change-set-name "$cs" \
      --stack-name "$stack_name" \
      --region "$region" 2>/dev/null || true
  done
}

# Lambda may auto-create /aws/lambda/<name> before CloudFormation's LogGroup resource runs,
# causing ResourceExistenceCheck / AlreadyExists on deploy. Delete those so CF can create them.
delete_orphaned_log_groups() {
  local stack="$1" region="$2" functions fn lg_name
  functions="$(aws cloudformation list-stack-resources \
    --stack-name "$stack" \
    --region "$region" \
    --query "StackResourceSummaries[?ResourceType=='AWS::Lambda::Function'].PhysicalResourceId" \
    --output text 2>/dev/null || true)"
  [[ -z "$functions" || "$functions" == "None" ]] && return 0
  for fn in $functions; do
    [[ -z "$fn" ]] && continue
    lg_name="/aws/lambda/${fn}"
    if aws logs describe-log-groups \
      --log-group-name-prefix "$lg_name" \
      --region "$region" \
      --query 'logGroups[].logGroupName' \
      --output text 2>/dev/null | tr '\t' '\n' | grep -Fxq "$lg_name"; then
      echo "=== Deleting orphaned log group: $lg_name ===" >&2
      aws logs delete-log-group --log-group-name "$lg_name" --region "$region" 2>/dev/null || true
    fi
  done
}

# GitHub Actions variables cannot be empty strings (HTTP 422). Delete if empty, set otherwise.
# Piping avoids gh treating --body "" as interactive stdin in some gh versions.
gh_variable_set_env() {
  local name="$1" env_name="$2" repo="$3" value="${4:-}"
  if [[ -z "$value" ]]; then
    gh variable delete "$name" --env "$env_name" -R "$repo" 2>/dev/null || true
  else
    printf '%s' "$value" | gh variable set "$name" --env "$env_name" -R "$repo"
  fi
}

abort_if_stack_managed_rds() {
  local stack="$1" region="$2" ids
  if [[ -z "$stack" || -z "$region" ]]; then
    return 0
  fi
  ids="$(aws cloudformation list-stack-resources \
    --stack-name "$stack" \
    --region "$region" \
    --query "StackResourceSummaries[?ResourceType=='AWS::RDS::DBInstance'].LogicalResourceId" \
    --output text 2>/dev/null || true)"
  if [[ -z "$ids" || "$ids" == "None" ]]; then
    return 0
  fi
  cat >&2 <<EOF
Error: CloudFormation stack '$stack' still contains stack-managed RDS ($ids).

This template no longer creates or updates RDS. An in-place SAM update would try to
destroy that database. Deploy is aborted.

Do this instead (while the old stack is still serving Slack):

  1. Backup from Slack Home → Backup/Restore (needs PRIMARY_WORKSPACE).
     Keep the same DATA_ENCRYPTION_KEY on the new stack.
     See docs/BACKUP_AND_MIGRATION.md and docs/DEPLOY.md.
  2. Delete the CloudFormation app stack. RDS DeletionProtection may block
     delete-stack until you disable protection or delete the instance in the
     RDS console — do that manually after the backup.
  3. Redeploy a fresh stack with DATABASE_BACKEND=mysql (TiDB / your host)
     or sqlite. Point Slack at the new Function URL / manifest.
  4. Restore the backup JSON on the empty new database.

There is no in-place migrate from stack RDS to TiDB or sqlite.
EOF
  exit 1
}

# Aliases AWS_DATABASE_MODE / DATABASE_ENGINE / EXISTING_DATABASE_HOST: see resolve_database_backend.sh (remove in 2.0.0).

gh_delete_legacy_database_vars() {
  local env_name="$1" repo="$2" name
  for name in \
    DATABASE_CREATE_APP_USER \
    DATABASE_CREATE_SCHEMA \
    DATABASE_ADMIN_USER \
    DATABASE_USERNAME_PREFIX \
    DATABASE_APP_USERNAME \
    DATABASE_NETWORK_MODE \
    DATABASE_SUBNET_IDS_CSV \
    DATABASE_LAMBDA_SECURITY_GROUP_ID; do
    gh variable delete "$name" --env "$env_name" -R "$repo" 2>/dev/null || true
  done
  gh variable delete SYNCBOT_INSTANCE_ID --env "$env_name" -R "$repo" 2>/dev/null || true
  gh secret delete DATABASE_ADMIN_PASSWORD --env "$env_name" -R "$repo" 2>/dev/null || true
}


# Always: bootstrap OIDC trio + AWS_STACK_NAME. Env-file consume list when assigned.
# Never writes STAGE_NAME. Maps aliases to canonical GitHub names.
push_github_aws_ci_config() {
  local repo="$1"
  local env_name="$2"
  local role="${3:-}"
  local bucket="${4:-}"
  local region="${5:-}"
  local stack_name="${6:-}"
  local val k github_name scope

  gh api -X PUT "repos/$repo/environments/$env_name" >/dev/null

  [[ -n "$role" ]] && gh variable set AWS_ROLE_TO_ASSUME --body "$role" -R "$repo"
  [[ -n "$bucket" ]] && gh variable set AWS_S3_BUCKET --body "$bucket" -R "$repo"
  [[ -n "$region" ]] && gh variable set AWS_REGION --body "$region" -R "$repo"
  [[ -n "$stack_name" ]] && gh_variable_set_env AWS_STACK_NAME "$env_name" "$repo" "$stack_name"

  _gh_push_from_env_file() {
    github_name="$1"
    scope="$2"
    shift 2
    val=""
    for k in "$@"; do
      if val="$(env_file_assignment_value "$k")"; then
        break
      fi
      val=""
    done
    [[ -n "$val" ]] || return 0
    case "$scope" in
      env) gh_variable_set_env "$github_name" "$env_name" "$repo" "$val" ;;
      repo) gh variable set "$github_name" --body "$val" -R "$repo" ;;
      secret) gh secret set "$github_name" --env "$env_name" --body "$val" -R "$repo" ;;
    esac
  }

  _gh_push_from_env_file AWS_BOOTSTRAP_STACK_NAME repo AWS_BOOTSTRAP_STACK_NAME BOOTSTRAP_STACK_NAME
  _gh_push_from_env_file DATABASE_BACKEND env DATABASE_BACKEND
  _gh_push_from_env_file ENABLE_KEEP_WARM env ENABLE_KEEP_WARM
  _gh_push_from_env_file DATABASE_SCHEMA env DATABASE_SCHEMA
  _gh_push_from_env_file DATABASE_HOST env DATABASE_HOST
  _gh_push_from_env_file DATABASE_PORT env DATABASE_PORT
  _gh_push_from_env_file DATABASE_USER env DATABASE_USER
  _gh_push_from_env_file DATABASE_TLS_ENABLED env DATABASE_TLS_ENABLED
  _gh_push_from_env_file DATABASE_SSL_CA_PATH env DATABASE_SSL_CA_PATH
  _gh_push_from_env_file LOG_LEVEL env LOG_LEVEL
  _gh_push_from_env_file PRIMARY_WORKSPACE env PRIMARY_WORKSPACE
  _gh_push_from_env_file ENABLE_DB_RESET env ENABLE_DB_RESET
  _gh_push_from_env_file AWS_ENABLE_XRAY env AWS_ENABLE_XRAY ENABLE_XRAY
  _gh_push_from_env_file SLACK_CLIENT_ID env SLACK_CLIENT_ID
  _gh_push_from_env_file DATABASE_PASSWORD secret DATABASE_PASSWORD
  _gh_push_from_env_file SLACK_SIGNING_SECRET secret SLACK_SIGNING_SECRET
  _gh_push_from_env_file SLACK_CLIENT_SECRET secret SLACK_CLIENT_SECRET
  _gh_push_from_env_file DATA_ENCRYPTION_KEY secret DATA_ENCRYPTION_KEY

  if ! env_file_assignment_value DATA_ENCRYPTION_KEY >/dev/null; then
    [[ -n "${DATA_ENCRYPTION_KEY:-}" ]] && gh secret set DATA_ENCRYPTION_KEY --env "$env_name" --body "$DATA_ENCRYPTION_KEY" -R "$repo"
  fi
  if ! env_file_assignment_value SLACK_SIGNING_SECRET >/dev/null; then
    [[ -n "${SLACK_SIGNING_SECRET:-}" ]] && gh secret set SLACK_SIGNING_SECRET --env "$env_name" --body "$SLACK_SIGNING_SECRET" -R "$repo"
  fi
  if ! env_file_assignment_value SLACK_CLIENT_SECRET >/dev/null; then
    [[ -n "${SLACK_CLIENT_SECRET:-}" ]] && gh secret set SLACK_CLIENT_SECRET --env "$env_name" --body "$SLACK_CLIENT_SECRET" -R "$repo"
  fi

  gh_delete_legacy_database_vars "$env_name" "$repo"
}

# Convert Key=Value lines (stdin or pipe) to JSON for aws cloudformation update-stack --parameters.
params_to_json() {
  python3 -c "
import json, sys
result = []
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    k, _, v = line.partition('=')
    # sam deploy omits empty overrides; update-stack must not wipe secrets with \"\".
    if v == '':
        result.append({'ParameterKey': k, 'UsePreviousValue': True})
    else:
        result.append({'ParameterKey': k, 'ParameterValue': v})
print(json.dumps(result))
"
}

deploy_via_update_stack() {
  local packaged template_key template_url cf_params_json

  mkdir -p .aws-sam/build
  packaged=".aws-sam/build/packaged-for-update-stack.yaml"

  echo "=== SAM Package (for CloudFormation update-stack) ===" >&2
  sam package \
    --template-file .aws-sam/build/template.yaml \
    --s3-bucket "$S3_BUCKET" \
    --output-template-file "$packaged" \
    --region "$REGION"

  template_key="packaged-templates/${STACK_NAME}-$(date +%s)-$$.yaml"
  echo "=== Upload packaged template to s3://${S3_BUCKET}/${template_key} ===" >&2
  aws s3 cp "$packaged" "s3://${S3_BUCKET}/${template_key}" --region "$REGION"

  template_url="https://${S3_BUCKET}.s3.${REGION}.amazonaws.com/${template_key}"

  cf_params_json="$(printf '%s\n' "${PARAMS[@]}" | params_to_json)"

  echo "=== CloudFormation update-stack ===" >&2
  aws cloudformation update-stack \
    --stack-name "$STACK_NAME" \
    --template-url "$template_url" \
    --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
    --region "$REGION" \
    --parameters "$cf_params_json"

  echo "=== Waiting for stack update to complete ===" >&2
  aws cloudformation wait stack-update-complete --stack-name "$STACK_NAME" --region "$REGION"
}

sam_deploy_or_fallback() {
  if [[ "${AWS_UPDATE_STACK:-}" == "true" ]]; then
    echo "=== SAM Deploy (direct update-stack; --update-stack set) ===" >&2
    deploy_via_update_stack
    return 0
  fi

  local log rc
  local -a sam_params=()
  local _p
  log="$(mktemp)"
  trap 'rm -f "$log"' RETURN

  for _p in "${PARAMS[@]}"; do
    [[ "$_p" == *"="?* ]] && sam_params+=("$_p")
  done

  set +e
  set -o pipefail
  sam deploy \
    -t .aws-sam/build/template.yaml \
    --stack-name "$STACK_NAME" \
    --s3-bucket "$S3_BUCKET" \
    --capabilities CAPABILITY_IAM \
    --region "$REGION" \
    --no-fail-on-empty-changeset \
    --parameter-overrides "${sam_params[@]}" 2>&1 | tee "$log"
  rc="${PIPESTATUS[0]}"
  set +o pipefail
  set -e

  if [[ "$rc" -eq 0 ]]; then
    return 0
  fi

  if grep -q 'EarlyValidation::ResourceExistenceCheck' "$log"; then
    echo "" >&2
    echo "=== Changeset rejected by CloudFormation early validation; retrying with direct update-stack... ===" >&2
    delete_failed_changesets "$STACK_NAME" "$REGION" || true
    delete_orphaned_log_groups "$STACK_NAME" "$REGION" || true
    deploy_via_update_stack
    return 0
  fi

  return "$rc"
}

prompt_default() {
  local prompt="$1"
  local default="$2"
  local value
  read -r -p "$prompt [$default]: " value
  if [[ -z "$value" ]]; then
    value="$default"
  fi
  echo "$value"
}

prompt_secret() {
  local prompt="$1"
  local value
  read -r -s -p "$prompt: " value
  # Keep the visual newline on the terminal even when called via $(...).
  printf '\n' >&2
  echo "$value"
}

prompt_required() {
  local prompt="$1"
  local value
  while true; do
    read -r -p "$prompt: " value
    if [[ -n "$value" ]]; then
      echo "$value"
      return 0
    fi
    echo "Error: $prompt is required." >&2
  done
}

prompt_secret_required() {
  local prompt="$1"
  local value
  while true; do
    value="$(prompt_secret "$prompt")"
    if [[ -n "$value" ]]; then
      echo "$value"
      return 0
    fi
    echo "Error: $prompt is required." >&2
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
    prompt_secret_required "$prompt"
  else
    prompt_required "$prompt"
  fi
}

# When local env overrides differ from the CloudFormation stack (e.g. GitHub-deployed TiDB vs .env existing host),
# prompt the operator instead of silently preferring env.
resolve_with_conflict_check() {
  local label="$1"
  local env_value="$2"
  local stack_value="$3"
  local prompt_default_value="$4"
  local mode="${5:-plain}" # plain|secret|bool

  if [[ -z "$env_value" ]]; then
    if [[ "$mode" == "secret" ]]; then
      prompt_secret_required "$label"
    elif [[ "$mode" == "bool" ]]; then
      local yn_def="${prompt_default_value:-y}"
      if prompt_yes_no "$label" "$yn_def"; then
        echo "true"
      else
        echo "false"
      fi
    else
      prompt_default "$label" "$prompt_default_value"
    fi
    return 0
  fi

  if [[ "$mode" == "bool" ]]; then
    if [[ "$env_value" != "true" && "$env_value" != "false" ]]; then
      echo "Error: environment value for $label must be true or false." >&2
      exit 1
    fi
    if [[ -z "$stack_value" || "$env_value" == "$stack_value" ]]; then
      echo "Using $label from environment variable." >&2
      echo "$env_value"
      return 0
    fi
    echo "" >&2
    echo "CONFLICT: $label differs between local env and deployed stack:" >&2
    echo "  Local env:  $env_value" >&2
    echo "  AWS stack:  $stack_value" >&2
    local choice
    read -r -p "Use (l)ocal env / (a)ws stack / (e)nter new value? [l/a/e]: " choice >&2
    case "$choice" in
      a | A) echo "$stack_value" ;;
      e | E)
        if prompt_yes_no "$label" "${prompt_default_value:-y}"; then
          echo "true"
        else
          echo "false"
        fi
        ;;
      *) echo "$env_value" ;;
    esac
    return 0
  fi

  if [[ -z "$stack_value" || "$env_value" == "$stack_value" ]]; then
    echo "Using $label from environment variable." >&2
    echo "$env_value"
    return 0
  fi

  local display_env="$env_value"
  local display_stack="$stack_value"
  if [[ "$mode" == "secret" ]]; then
    display_env="(hidden)"
    display_stack="(hidden)"
  fi
  echo "" >&2
  echo "CONFLICT: $label differs between local env and deployed stack:" >&2
  echo "  Local env:  $display_env" >&2
  echo "  AWS stack:  $display_stack" >&2
  local choice
  read -r -p "Use (l)ocal env / (a)ws stack / (e)nter new value? [l/a/e]: " choice >&2
  case "$choice" in
    a | A) echo "$stack_value" ;;
    e | E)
      if [[ "$mode" == "secret" ]]; then
        prompt_secret_required "$label"
      else
        prompt_required "$label"
      fi
      ;;
    *) echo "$env_value" ;;
  esac
}

prompt_yes_no() {
  local prompt="$1"
  local default="${2:-y}"
  local answer
  local shown="y/N"
  [[ "$default" == "y" ]] && shown="Y/n"
  read -r -p "$prompt [$shown]: " answer
  if [[ -z "$answer" ]]; then
    answer="$default"
  fi
  [[ "$answer" =~ ^[Yy]$ ]]
}

ensure_aws_authenticated() {
  local identity profile_hint=""
  if identity="$(aws sts get-caller-identity --query Arn --output text 2>/dev/null)"; then
    echo "AWS session: $identity"
    return 0
  fi
  if [[ -n "${AWS_PROFILE:-}" ]]; then
    profile_hint=" --profile $AWS_PROFILE"
  fi
  echo "Error: no active AWS CLI session." >&2
  echo "Log in, then rerun this script:" >&2
  echo "  aws login${profile_hint}" >&2
  echo "  aws sso login${profile_hint}" >&2
  echo "  aws configure" >&2
  exit 1
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
  if prompt_yes_no "Run 'gh auth login' now?" "y"; then
    gh auth login || true
  fi
  if gh auth status >/dev/null 2>&1; then
    return 0
  fi
  echo "gh authentication is still missing. Skipping automatic GitHub setup."
  return 1
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
  app_id="$(prompt_default "Slack App ID (optional; blank = create new app)" "${SLACK_APP_ID:-}")"
  team_id="$(prompt_default "Slack Team ID (optional; usually blank)" "${SLACK_TEAM_ID:-}")"

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

  # No App ID supplied: create a new Slack app from manifest.
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

bootstrap_describe_outputs() {
  local stack_name="$1"
  local region="$2"
  aws cloudformation describe-stacks \
    --stack-name "$stack_name" \
    --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
    --output text \
    --region "$region" 2>/dev/null || true
}

app_describe_outputs() {
  local stack_name="$1"
  local region="$2"
  aws cloudformation describe-stacks \
    --stack-name "$stack_name" \
    --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
    --output text \
    --region "$region" 2>/dev/null || true
}

output_value() {
  local outputs="$1"
  local key="$2"
  echo "$outputs" | awk -F'\t' -v k="$key" '$1==k {print $2}'
}

configure_github_actions_aws() {
  # $1 bootstrap outputs  $2 bootstrap stack  $3 region  $4 app stack
  # $5 stage  $6 schema  $7 DATABASE_BACKEND  $8 host  $9 port
  local bootstrap_outputs="$1"
  local bootstrap_stack_name="$2"
  local aws_region="$3"
  local app_stack_name="$4"
  local deploy_stage="$5"
  local database_schema="$6"
  local database_backend="${7:-mysql}"
  local db_host="${8:-}"
  local db_port="${9:-}"
  [[ -z "$database_backend" ]] && database_backend="mysql"
  local role bucket boot_region
  role="$(output_value "$bootstrap_outputs" "GitHubDeployRoleArn")"
  bucket="$(output_value "$bootstrap_outputs" "DeploymentBucketName")"
  boot_region="$(output_value "$bootstrap_outputs" "BootstrapRegion")"
  [[ -z "$boot_region" ]] && boot_region="$aws_region"
  local repo env_name
  env_name="$deploy_stage"

  echo
  echo "=== GitHub Actions (AWS) ==="
  echo "Detected bootstrap role:   $role"
  echo "Detected deploy bucket:    $bucket  (SAM/CI packaging for sam deploy — not Slack or app media)"
  echo "Detected bootstrap region: $boot_region"
  repo="$(prompt_github_repo_for_actions "$REPO_ROOT")"
  maybe_prompt_bootstrap_github_trust_update "$repo" "$bootstrap_stack_name" "$aws_region"

  if ! ensure_gh_authenticated; then
    echo
    echo "Set these GitHub Actions Variables manually (on the repo you intend):"
    echo "  AWS_ROLE_TO_ASSUME = $role"
    echo "  AWS_S3_BUCKET      = $bucket  (SAM deploy artifact bucket / DeploymentBucketName; not Slack file storage)"
    echo "  AWS_REGION         = $boot_region"
    echo "For environment '$env_name' also set AWS_STACK_NAME, DATABASE_BACKEND,"
    echo "DATABASE_SCHEMA, DATABASE_USER, and (mysql/postgresql) DATABASE_HOST — see docs/DEPLOY.md."
    echo "If those keys are in the env file, --setup-github copies them. The AWS job sets Stage (test or prod)."
    return 0
  fi

  if prompt_yes_no "Create/update GitHub environments 'test' and 'prod' now?" "y"; then
    gh api -X PUT "repos/$repo/environments/test" >/dev/null
    gh api -X PUT "repos/$repo/environments/prod" >/dev/null
    echo "GitHub environments ensured: test, prod."
  fi

  if prompt_yes_no "Set repo variables with gh now (AWS_ROLE_TO_ASSUME, AWS_S3_BUCKET, AWS_REGION)? AWS_S3_BUCKET is SAM/CI packaging only (DeploymentBucketName)." "y"; then
    [[ -n "$role" ]] && gh variable set AWS_ROLE_TO_ASSUME --body "$role" -R "$repo"
    [[ -n "$bucket" ]] && gh variable set AWS_S3_BUCKET --body "$bucket" -R "$repo"
    [[ -n "$boot_region" ]] && gh variable set AWS_REGION --body "$boot_region" -R "$repo"
    echo "GitHub repository variables updated."
  fi

  if prompt_yes_no "Set environment variables and secrets for '$env_name' now (AWS_STACK_NAME and env-file keys AWS CI reads)?" "y"; then
    if [[ -z "${SLACK_SIGNING_SECRET:-}" ]]; then
      SLACK_SIGNING_SECRET="$(required_from_env_or_prompt "SLACK_SIGNING_SECRET" "SlackSigningSecret" "secret")"
    fi
    if [[ -z "${SLACK_CLIENT_SECRET:-}" ]]; then
      SLACK_CLIENT_SECRET="$(required_from_env_or_prompt "SLACK_CLIENT_SECRET" "SlackClientSecret" "secret")"
    fi
    push_github_aws_ci_config "$repo" "$env_name" "$role" "$bucket" "$boot_region" "$app_stack_name"
    echo "GitHub environment '$env_name' updated."
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
    echo "Could not determine API URL from stack outputs. Skipping Slack manifest generation."
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
  receipt_path="$receipt_dir/deploy-aws-${STAGE}-${ts_file}.md"

  mkdir -p "$receipt_dir"
  {
    cat <<EOF
# SyncBot Deploy Receipt

- Provider: aws
- Stage: $STAGE
- Timestamp: $ts_human
- Project/Stack: $STACK_NAME
- Region: $REGION

## Slack URLs
- Events/API URL: ${api_url:-n/a}
- Install URL: ${SYNCBOT_INSTALL_URL:-n/a}
- OAuth Redirect URL: ${oauth_redirect_url:-n/a}
- Slack Manifest: ${SLACK_MANIFEST_GENERATED_PATH:-n/a}

## Configuration
- AWS_STACK_NAME=$STACK_NAME
- DATABASE_BACKEND=${DATABASE_BACKEND:-}
- ENABLE_KEEP_WARM=${ENABLE_KEEP_WARM:-true}
- DATABASE_SCHEMA=${DATABASE_SCHEMA:-}
- DATABASE_HOST=${DATABASE_HOST:-}
- DATABASE_PORT=${DATABASE_PORT:-}
- DATABASE_USER=${DATABASE_USER:-}
- DATABASE_TLS_ENABLED=${DATABASE_TLS_ENABLED:-}
- LOG_LEVEL=${LOG_LEVEL:-INFO}
- PRIMARY_WORKSPACE=${PRIMARY_WORKSPACE:-}
- SLACK_CLIENT_ID=${SLACK_CLIENT_ID:-}
- AWS_ENABLE_XRAY=${AWS_ENABLE_XRAY:-false}
- ENABLE_DB_RESET=${ENABLE_DB_RESET:-false}

## Secrets
- SLACK_SIGNING_SECRET=${SLACK_SIGNING_SECRET:-}
- SLACK_CLIENT_SECRET=${SLACK_CLIENT_SECRET:-}
- DATA_ENCRYPTION_KEY=${DATA_ENCRYPTION_KEY:-}
- DATABASE_PASSWORD=${DATABASE_PASSWORD:-}
EOF

    if [[ "${VERBOSE:-}" == "true" ]]; then
      echo ""
      echo "## SAM Parameters"
      if [[ ${#PARAMS[@]} -gt 0 ]]; then
        local p
        for p in "${PARAMS[@]}"; do
          echo "- $p"
        done
      else
        echo "(PARAMS array not available)"
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

stack_status() {
  local stack_name="$1"
  local region="$2"
  aws cloudformation describe-stacks \
    --stack-name "$stack_name" \
    --region "$region" \
    --query 'Stacks[0].StackStatus' \
    --output text 2>/dev/null || true
}

stack_parameters() {
  local stack_name="$1"
  local region="$2"
  aws cloudformation describe-stacks \
    --stack-name "$stack_name" \
    --region "$region" \
    --query 'Stacks[0].Parameters[*].[ParameterKey,ParameterValue]' \
    --output text 2>/dev/null || true
}

stack_param_value() {
  local params="$1"
  local key="$2"
  echo "$params" | awk -F'\t' -v k="$key" '$1==k {print $2}'
}

# Keep bootstrap stack aligned with the checked-in template so IAM/policy fixes
# (for example CloudFormation changeset permissions) apply before app deploy.
# Set SYNCBOT_SKIP_BOOTSTRAP_SYNC=1 to opt out.
sync_bootstrap_stack_from_repo() {
  local bootstrap_stack="$1"
  local aws_region="$2"
  local params github_repo create_oidc bucket_prefix

  if [[ "${SYNCBOT_SKIP_BOOTSTRAP_SYNC:-}" == "1" ]]; then
    echo "Skipping bootstrap template sync (SYNCBOT_SKIP_BOOTSTRAP_SYNC=1)."
    return 0
  fi

  params="$(stack_parameters "$bootstrap_stack" "$aws_region")"
  if [[ -z "$params" ]]; then
    echo "Could not read bootstrap stack parameters for '$bootstrap_stack' in $aws_region; skipping bootstrap template sync." >&2
    return 0
  fi

  github_repo="$(stack_param_value "$params" "GitHubRepository")"
  github_repo="${github_repo//$'\r'/}"
  github_repo="${github_repo#"${github_repo%%[![:space:]]*}"}"
  github_repo="${github_repo%"${github_repo##*[![:space:]]}"}"
  if [[ -z "$github_repo" ]]; then
    echo "Bootstrap stack has no GitHubRepository parameter; skipping bootstrap template sync." >&2
    return 0
  fi

  create_oidc="$(stack_param_value "$params" "CreateOIDCProvider")"
  bucket_prefix="$(stack_param_value "$params" "DeploymentBucketPrefix")"
  [[ -z "$create_oidc" ]] && create_oidc="true"
  [[ -z "$bucket_prefix" ]] && bucket_prefix="syncbot-deploy"

  echo
  echo "Syncing bootstrap stack with repo template..."
  aws cloudformation deploy \
    --template-file "$BOOTSTRAP_TEMPLATE" \
    --stack-name "$bootstrap_stack" \
    --parameter-overrides \
      "GitHubRepository=$github_repo" \
      "CreateOIDCProvider=$create_oidc" \
      "DeploymentBucketPrefix=$bucket_prefix" \
    --capabilities CAPABILITY_NAMED_IAM \
    --no-fail-on-empty-changeset \
    --region "$aws_region"
}

# Create bootstrap if missing; sync only when template.bootstrap.yaml hash differs
# (stack parameter TemplateContentSha256). BOOTSTRAP=true (--bootstrap) forces a sync.
ensure_aws_bootstrap_stack() {
  local extra=()
  extra+=(--create)
  [[ "${BOOTSTRAP:-}" == "true" ]] && extra+=(--force)
  [[ "${SYNCBOT_SKIP_BOOTSTRAP_SYNC:-}" == "1" ]] && extra+=(--skip-sync)
  echo
  echo "=== Bootstrap ==="
  BOOTSTRAP_STACK_NAME="$BOOTSTRAP_STACK" \
    AWS_BOOTSTRAP_STACK_NAME="$BOOTSTRAP_STACK" \
    AWS_REGION="$REGION" \
    GITHUB_REPO="${GITHUB_REPO:-}" \
    AWS_CREATE_OIDC_PROVIDER="${AWS_CREATE_OIDC_PROVIDER:-true}" \
    AWS_DEPLOY_BUCKET_PREFIX="${AWS_DEPLOY_BUCKET_PREFIX:-syncbot-deploy}" \
    bash "$REPO_ROOT/infra/aws/scripts/ensure_bootstrap.sh" "${extra[@]}"
}

# Compare GitHub owner/repo from bootstrap stack to the repo chosen for gh; offer to update OIDC trust.
maybe_prompt_bootstrap_github_trust_update() {
  local picked_repo="$1"
  local bootstrap_stack="$2"
  local aws_region="$3"
  local params trusted picked_lc trusted_lc create_oidc bucket_prefix

  if [[ -z "$bootstrap_stack" || -z "$picked_repo" ]]; then
    return 0
  fi

  params="$(stack_parameters "$bootstrap_stack" "$aws_region")"
  if [[ -z "$params" ]]; then
    echo "Could not read bootstrap stack parameters for '$bootstrap_stack' in $aws_region; skipping OIDC trust drift check." >&2
    return 0
  fi

  trusted="$(stack_param_value "$params" "GitHubRepository")"
  # CloudFormation / CLI sometimes surface trailing whitespace; normalize for compare + display.
  trusted="${trusted//$'\r'/}"
  trusted="${trusted#"${trusted%%[![:space:]]*}"}"
  trusted="${trusted%"${trusted##*[![:space:]]}"}"
  if [[ -z "$trusted" ]]; then
    echo "Bootstrap stack has no GitHubRepository parameter; skipping OIDC trust drift check." >&2
    return 0
  fi

  picked_lc="$(printf '%s' "$picked_repo" | tr '[:upper:]' '[:lower:]')"
  trusted_lc="$(printf '%s' "$trusted" | tr '[:upper:]' '[:lower:]')"
  if [[ "$picked_lc" == "$trusted_lc" ]]; then
    echo "Bootstrap OIDC: stack '$bootstrap_stack' has GitHubRepository=$trusted — matches your choice; no bootstrap update needed."
    return 0
  fi

  echo
  echo "Warning: Bootstrap stack '$bootstrap_stack' OIDC trust is scoped to:"
  echo "  GitHubRepository=$trusted"
  echo "You chose this repository for GitHub Actions variables:"
  echo "  $picked_repo"
  echo "GitHub Actions in '$picked_repo' cannot assume the deploy role until trust matches."
  echo
  if ! prompt_yes_no "Update bootstrap OIDC trust to '$picked_repo'? (CloudFormation stack update)" "n"; then
    echo "Leaving bootstrap GitHubRepository unchanged. Fix manually or update the bootstrap stack later." >&2
    return 0
  fi

  create_oidc="$(stack_param_value "$params" "CreateOIDCProvider")"
  bucket_prefix="$(stack_param_value "$params" "DeploymentBucketPrefix")"
  [[ -z "$create_oidc" ]] && create_oidc="true"
  [[ -z "$bucket_prefix" ]] && bucket_prefix="syncbot-deploy"

  echo "Updating bootstrap stack '$bootstrap_stack'..."
  aws cloudformation deploy \
    --template-file "$BOOTSTRAP_TEMPLATE" \
    --stack-name "$bootstrap_stack" \
    --parameter-overrides \
      "GitHubRepository=$picked_repo" \
      "CreateOIDCProvider=$create_oidc" \
      "DeploymentBucketPrefix=$bucket_prefix" \
    --capabilities CAPABILITY_NAMED_IAM \
    --region "$aws_region"
  echo "Bootstrap OIDC trust updated to $picked_repo."
}

print_recent_stack_failures() {
  local stack_name="$1"
  local region="$2"
  echo "Recent failure events for $stack_name:"
  aws cloudformation describe-stack-events \
    --stack-name "$stack_name" \
    --region "$region" \
    --query "StackEvents[?contains(ResourceStatus, 'FAILED')].[Timestamp,LogicalResourceId,ResourceStatus,ResourceStatusReason]" \
    --output table 2>/dev/null || true
}

handle_unhealthy_stack_state() {
  local stack_name="$1"
  local region="$2"
  local status
  status="$(stack_status "$stack_name" "$region")"
  if [[ -z "$status" || "$status" == "None" ]]; then
    return 0
  fi

  case "$status" in
    CREATE_FAILED|ROLLBACK_COMPLETE|ROLLBACK_FAILED|UPDATE_ROLLBACK_FAILED|DELETE_FAILED)
      echo
      echo "Stack $stack_name is in a failed state: $status"
      print_recent_stack_failures "$stack_name" "$region"
      echo
      if prompt_yes_no "Delete failed stack '$stack_name' now so deploy can continue?" "y"; then
        aws cloudformation delete-stack --stack-name "$stack_name" --region "$region"
        echo "Waiting for stack deletion to complete..."
        aws cloudformation wait stack-delete-complete --stack-name "$stack_name" --region "$region"
      else
        echo "Cannot continue deploy while stack is in $status."
        exit 1
      fi
      ;;
    *_IN_PROGRESS)
      echo "Error: stack $stack_name is currently $status. Wait for it to finish, then rerun." >&2
      exit 1
      ;;
    *)
      ;;
  esac
}

echo "=== Prerequisites ==="
prereqs_require_cmd aws prereqs_hint_aws_cli
prereqs_require_cmd sam prereqs_hint_sam_cli
prereqs_require_cmd python3 prereqs_hint_python3
prereqs_require_cmd curl prereqs_hint_curl

prereqs_print_cli_status_matrix "AWS" aws sam python3 curl
ensure_aws_authenticated

if [[ ! -f "$APP_TEMPLATE" ]]; then
  echo "Error: app template not found at $APP_TEMPLATE" >&2
  exit 1
fi
if [[ ! -f "$BOOTSTRAP_TEMPLATE" ]]; then
  echo "Error: bootstrap template not found at $BOOTSTRAP_TEMPLATE" >&2
  exit 1
fi

# ====================================================================
# Non-interactive fast path (./deploy.sh --env test|prod)
# ====================================================================
if [[ "${ENV_FILE_LOADED:-}" == "true" ]]; then
  echo "=== SyncBot AWS Deploy (non-interactive) ==="
  apply_aws_provider_env_aliases
  REGION="${AWS_REGION:-us-east-1}"
  BOOTSTRAP_STACK="${AWS_BOOTSTRAP_STACK_NAME:-syncbot-bootstrap}"

  ensure_aws_bootstrap_stack

  BOOTSTRAP_OUTPUTS="$(bootstrap_describe_outputs "$BOOTSTRAP_STACK" "$REGION")"
  S3_BUCKET="${AWS_S3_BUCKET:-$(output_value "$BOOTSTRAP_OUTPUTS" "DeploymentBucketName")}"
  if [[ -z "$S3_BUCKET" ]]; then
    echo "Error: could not determine S3 deploy bucket after bootstrap. Set AWS_S3_BUCKET in env file." >&2
    exit 1
  fi
  AWS_S3_BUCKET="$S3_BUCKET"
  STACK_NAME="${AWS_STACK_NAME:?AWS_STACK_NAME required in env file}"
  STAGE="${STAGE:?STAGE required}"

  handle_unhealthy_stack_state "$STACK_NAME" "$REGION"
  abort_if_stack_managed_rds "$STACK_NAME" "$REGION"

  resolve_database_schema "$STACK_NAME" "$REGION" "$STAGE"
  DATA_ENCRYPTION_KEY="${DATA_ENCRYPTION_KEY:-${TOKEN_ENCRYPTION_KEY:-}}"
  ENABLE_KEEP_WARM="${ENABLE_KEEP_WARM:-true}"
  resolve_database_backend aws
  require_database_credentials_for_backend

  if [[ -n "${ENV_FILE_PATH:-}" ]]; then
    update_env_file "$ENV_FILE_PATH" "DATABASE_BACKEND" "$DATABASE_BACKEND"
    update_env_file "$ENV_FILE_PATH" "ENABLE_KEEP_WARM" "$ENABLE_KEEP_WARM"
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

  if [[ "$DATABASE_BACKEND" != "sqlite" ]]; then
    DATABASE_PASSWORD="${DATABASE_PASSWORD:?DATABASE_PASSWORD required in env file when DATABASE_BACKEND is not sqlite}"
    DATABASE_USER="${DATABASE_USER:?DATABASE_USER required in env file when DATABASE_BACKEND is not sqlite}"
  else
    DATABASE_PASSWORD="${DATABASE_PASSWORD:-}"
    DATABASE_USER="${DATABASE_USER:-}"
    DATABASE_HOST=""
  fi

  PARAMS=(
    "Stage=$STAGE"
    "DatabaseBackend=$DATABASE_BACKEND"
    "EnableKeepWarm=$ENABLE_KEEP_WARM"
    "SlackSigningSecret=${SLACK_SIGNING_SECRET:?SLACK_SIGNING_SECRET required}"
    "SlackClientSecret=${SLACK_CLIENT_SECRET:?SLACK_CLIENT_SECRET required}"
    "SlackClientID=${SLACK_CLIENT_ID:?SLACK_CLIENT_ID required}"
    "DatabaseSchema=$DATABASE_SCHEMA"
    "DataEncryptionKey=$DATA_ENCRYPTION_KEY"
    "DatabasePassword=${DATABASE_PASSWORD:-}"
    "DatabaseUser=${DATABASE_USER:-}"
    "LogLevel=${LOG_LEVEL:-INFO}"
    "PrimaryWorkspace=${PRIMARY_WORKSPACE:-}"
    "EnableDbReset=${ENABLE_DB_RESET:-}"
    "DatabaseTlsEnabled=${DATABASE_TLS_ENABLED:-}"
    "DatabaseSslCaPath=${DATABASE_SSL_CA_PATH:-}"
    "EnableXRay=${AWS_ENABLE_XRAY:-false}"
    "DatabaseHost=${DATABASE_HOST:-}"
    "DatabasePort=${DATABASE_PORT:-}"
    "SlackOauthBotScopes=${SLACK_BOT_SCOPES:-app_mentions:read,channels:history,channels:join,channels:read,channels:manage,chat:write,chat:write.customize,emoji:read,files:read,files:write,groups:history,groups:read,groups:write,im:write,reactions:read,reactions:write,team:read,usergroups:read,users:read,users:read.email}"
    "SlackOauthUserScopes=${SLACK_USER_SCOPES:-chat:write,channels:history,channels:read,files:read,files:write,groups:history,groups:read,groups:write,reactions:read,reactions:write,team:read,users:read,users:read.email}"
  )

  echo "=== SAM Build ==="
  sam build -t "$APP_TEMPLATE" --build-in-source

  echo "=== SAM Deploy ==="
  sam_deploy_or_fallback

  APP_OUTPUTS="$(app_describe_outputs "$STACK_NAME" "$REGION")"
  FUNCTION_ARN="$(output_value "$APP_OUTPUTS" "SyncBotFunctionArn")"
  if [[ -n "$FUNCTION_ARN" ]]; then
    echo "=== Lambda migrate + warm-up ==="
    "$REPO_ROOT/infra/aws/scripts/invoke_lambda_migrate.sh" "$FUNCTION_ARN" "$REGION"
  fi

  SYNCBOT_API_URL="$(output_value "$APP_OUTPUTS" "SyncBotApiUrl")"
  SYNCBOT_INSTALL_URL="$(output_value "$APP_OUTPUTS" "SyncBotInstallUrl")"
  generate_stage_slack_manifest "$STAGE" "$SYNCBOT_API_URL" "$SYNCBOT_INSTALL_URL"

  if [[ "${SETUP_GITHUB:-}" == "true" ]]; then
    echo
    echo "=== Push to GitHub Environment ==="
    prereqs_require_cmd gh prereqs_hint_gh_cli
    if ! gh auth status >/dev/null 2>&1; then
      echo "Error: gh CLI not authenticated. Run 'gh auth login' first." >&2
      exit 1
    fi
    REPO="$(prompt_github_repo_for_actions "$REPO_ROOT")"
    ENV_NAME="$STAGE"
    ROLE_ARN="${AWS_ROLE_TO_ASSUME:-$(output_value "$BOOTSTRAP_OUTPUTS" "GitHubDeployRoleArn")}"
    push_github_aws_ci_config "$REPO" "$ENV_NAME" "$ROLE_ARN" "$S3_BUCKET" "$REGION" "$STACK_NAME"
    echo "GitHub environment '$ENV_NAME' updated for repo $REPO."
  fi

  echo
  echo "=== Deploy Receipt ==="
  write_deploy_receipt

  echo
  echo "=== Deploy Complete ==="
  echo "Stack:       $STACK_NAME"
  echo "Region:      $REGION"
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
echo "=== SyncBot AWS Deploy ==="
echo

# Backward-compatible aliases: new name primary, EXISTING_ as fallback (same as non-interactive path)
apply_aws_provider_env_aliases
DATABASE_HOST="${DATABASE_HOST:-${EXISTING_DATABASE_HOST:-}}"
DATABASE_PORT="${DATABASE_PORT:-${EXISTING_DATABASE_PORT:-}}"
DATA_ENCRYPTION_KEY="${DATA_ENCRYPTION_KEY:-${TOKEN_ENCRYPTION_KEY:-}}"
ENABLE_KEEP_WARM="${ENABLE_KEEP_WARM:-true}"

DEFAULT_REGION="${AWS_REGION:-us-east-1}"
REGION="$(prompt_default "AWS region" "$DEFAULT_REGION")"
BOOTSTRAP_STACK="$(prompt_default "Bootstrap stack name" "${AWS_BOOTSTRAP_STACK_NAME:-syncbot-bootstrap}")"

if ! aws cloudformation describe-stacks --stack-name "$BOOTSTRAP_STACK" --region "$REGION" >/dev/null 2>&1; then
  if [[ -z "${GITHUB_REPO:-}" ]]; then
    GITHUB_REPO="$(prompt_github_repo_for_actions "$REPO_ROOT")"
  fi
fi
ensure_aws_bootstrap_stack

# Probe bootstrap outputs for suggested app stack names.
BOOTSTRAP_OUTPUTS="$(bootstrap_describe_outputs "$BOOTSTRAP_STACK" "$REGION")"

SUGGESTED_TEST_STACK="$(output_value "$BOOTSTRAP_OUTPUTS" "SuggestedTestStackName")"
SUGGESTED_PROD_STACK="$(output_value "$BOOTSTRAP_OUTPUTS" "SuggestedProdStackName")"
[[ -z "$SUGGESTED_TEST_STACK" ]] && SUGGESTED_TEST_STACK="syncbot-test"
[[ -z "$SUGGESTED_PROD_STACK" ]] && SUGGESTED_PROD_STACK="syncbot-prod"

echo
echo "=== Stack Identity ==="
STAGE="$(prompt_default "Deploy stage (test/prod)" "test")"
if [[ "$STAGE" != "test" && "$STAGE" != "prod" ]]; then
  echo "Error: stage must be 'test' or 'prod'." >&2
  exit 1
fi

DEFAULT_STACK="$SUGGESTED_TEST_STACK"
[[ "$STAGE" == "prod" ]] && DEFAULT_STACK="$SUGGESTED_PROD_STACK"
STACK_NAME="$(prompt_default "App stack name" "${AWS_STACK_NAME:-$DEFAULT_STACK}")"
AWS_STACK_NAME="$STACK_NAME"
EXISTING_STACK_STATUS="$(stack_status "$STACK_NAME" "$REGION")"
IS_STACK_UPDATE="false"
EXISTING_STACK_PARAMS=""
PREV_DATABASE_HOST=""
PREV_DATABASE_PORT=""
PREV_DATABASE_ENGINE=""
PREV_DATABASE_BACKEND=""
PREV_DATABASE_SCHEMA=""
PREV_DATABASE_MODE=""
PREV_ENABLE_KEEP_WARM=""
PREV_LOG_LEVEL=""
PREV_PRIMARY_WORKSPACE=""
PREV_ENABLE_DB_RESET=""
PREV_DB_TLS=""
PREV_DB_SSL_CA=""
PREV_DATABASE_HOST_IN_USE=""
PREV_STACK_USES_EXTERNAL_DB="false"
EXISTING_STACK_OUTPUTS=""
if [[ -n "$EXISTING_STACK_STATUS" && "$EXISTING_STACK_STATUS" != "None" ]]; then
  echo "Detected existing CloudFormation stack: $STACK_NAME ($EXISTING_STACK_STATUS)"
  if ! prompt_yes_no "Continue and update this existing stack?" "y"; then
    echo "Aborted."
    exit 0
  fi
  IS_STACK_UPDATE="true"
  EXISTING_STACK_PARAMS="$(stack_parameters "$STACK_NAME" "$REGION")"
  abort_if_stack_managed_rds "$STACK_NAME" "$REGION"
  PREV_DATABASE_BACKEND=""
  PREV_DATABASE_HOST="$(stack_param_value "$EXISTING_STACK_PARAMS" "DatabaseHost")"
  [[ -z "$PREV_DATABASE_HOST" ]] && PREV_DATABASE_HOST="$(stack_param_value "$EXISTING_STACK_PARAMS" "ExistingDatabaseHost")"
  PREV_DATABASE_PORT="$(stack_param_value "$EXISTING_STACK_PARAMS" "DatabasePort")"
  [[ -z "$PREV_DATABASE_PORT" ]] && PREV_DATABASE_PORT="$(stack_param_value "$EXISTING_STACK_PARAMS" "ExistingDatabasePort")"
  PREV_DATABASE_BACKEND="$(stack_param_value "$EXISTING_STACK_PARAMS" "DatabaseBackend")"
  PREV_DATABASE_ENGINE="$(stack_param_value "$EXISTING_STACK_PARAMS" "DatabaseEngine")"
  PREV_DATABASE_MODE="$(stack_param_value "$EXISTING_STACK_PARAMS" "DatabaseMode")"
  PREV_ENABLE_KEEP_WARM="$(stack_param_value "$EXISTING_STACK_PARAMS" "EnableKeepWarm")"
  PREV_DATABASE_SCHEMA="$(stack_param_value "$EXISTING_STACK_PARAMS" "DatabaseSchema")"
  PREV_LOG_LEVEL="$(stack_param_value "$EXISTING_STACK_PARAMS" "LogLevel")"
  PREV_PRIMARY_WORKSPACE="$(stack_param_value "$EXISTING_STACK_PARAMS" "PrimaryWorkspace")"
  PREV_ENABLE_DB_RESET="$(stack_param_value "$EXISTING_STACK_PARAMS" "EnableDbReset")"
  PREV_DB_TLS="$(stack_param_value "$EXISTING_STACK_PARAMS" "DatabaseTlsEnabled")"
  PREV_DB_SSL_CA="$(stack_param_value "$EXISTING_STACK_PARAMS" "DatabaseSslCaPath")"
  EXISTING_STACK_OUTPUTS="$(app_describe_outputs "$STACK_NAME" "$REGION")"
  PREV_DATABASE_HOST_IN_USE="$(output_value "$EXISTING_STACK_OUTPUTS" "DatabaseHostInUse")"
  if [[ -n "$PREV_DATABASE_BACKEND" ]]; then
    :
  elif [[ "$PREV_DATABASE_MODE" == "sqlite" || "$PREV_DATABASE_HOST_IN_USE" == "sqlite" ]]; then
    PREV_DATABASE_BACKEND="sqlite"
  elif [[ -n "$PREV_DATABASE_HOST" || "$PREV_DATABASE_MODE" == "existing" ]]; then
    PREV_STACK_USES_EXTERNAL_DB="true"
    if [[ "$PREV_DATABASE_ENGINE" == "postgresql" ]]; then
      PREV_DATABASE_BACKEND="postgresql"
    else
      PREV_DATABASE_BACKEND="mysql"
    fi
  fi
  if [[ "$PREV_DATABASE_BACKEND" == "sqlite" ]]; then
    PREV_DATABASE_MODE="sqlite"
  elif [[ -n "$PREV_DATABASE_BACKEND" ]]; then
    PREV_STACK_USES_EXTERNAL_DB="true"
    PREV_DATABASE_MODE="existing"
  fi
  if [[ -z "$PREV_DATABASE_HOST" && -n "$PREV_DATABASE_HOST_IN_USE" && "$PREV_DATABASE_HOST_IN_USE" != "sqlite" ]]; then
    PREV_DATABASE_HOST="$PREV_DATABASE_HOST_IN_USE"
  fi
fi

echo
prompt_deploy_tasks_aws

BOOTSTRAP_OUTPUTS="$(bootstrap_describe_outputs "$BOOTSTRAP_STACK" "$REGION")"
S3_BUCKET="$(output_value "$BOOTSTRAP_OUTPUTS" "DeploymentBucketName")"
if [[ -n "$S3_BUCKET" ]]; then
  echo "Detected deploy bucket from bootstrap: $S3_BUCKET"
elif [[ "$TASK_BUILD_DEPLOY" == "true" ]]; then
  S3_BUCKET="$(prompt_default "Deployment S3 bucket name" "REPLACE_ME_DEPLOY_BUCKET")"
else
  S3_BUCKET=""
fi

if [[ "$TASK_BUILD_DEPLOY" != "true" ]]; then
  if [[ "$TASK_CICD" == "true" || "$TASK_SLACK_API" == "true" ]]; then
    if [[ -z "${EXISTING_STACK_STATUS:-}" || "$EXISTING_STACK_STATUS" == "None" ]]; then
      echo "Error: CloudFormation stack '$STACK_NAME' does not exist in $REGION. Select task 1 (Build/Deploy) first or create the stack." >&2
      exit 1
    fi
  fi
fi

if [[ "$TASK_BUILD_DEPLOY" == "true" ]]; then
echo
echo "=== Configuration ==="
echo "=== Database ==="
echo "  1) MySQL (TiDB / your own host) — default"
echo "  2) PostgreSQL"
echo "  3) SQLite + Litestream to S3 (pennies of S3; reserved concurrency 1; keep-warm recommended)"
DATABASE_BACKEND="mysql"
DB_BACKEND_DEFAULT="1"
if [[ "$IS_STACK_UPDATE" == "true" && "$PREV_DATABASE_BACKEND" == "sqlite" ]]; then
  DB_BACKEND_DEFAULT="3"
  echo "Current stack: sqlite"
elif [[ "$IS_STACK_UPDATE" == "true" && "$PREV_DATABASE_BACKEND" == "postgresql" ]]; then
  DB_BACKEND_DEFAULT="2"
  echo "Current stack: postgresql"
elif [[ "$IS_STACK_UPDATE" == "true" ]]; then
  echo "Current stack: mysql"
fi
DB_CHOICE="$(prompt_default "Choose database (1, 2, or 3)" "$DB_BACKEND_DEFAULT")"
case "$DB_CHOICE" in
  1) DATABASE_BACKEND="mysql" ;;
  2) DATABASE_BACKEND="postgresql" ;;
  3) DATABASE_BACKEND="sqlite" ;;
  *)
    echo "Error: invalid database choice." >&2
    exit 1
    ;;
esac

echo
echo "=== Slack App Credentials ==="
SLACK_SIGNING_SECRET="$(required_from_env_or_prompt "SLACK_SIGNING_SECRET" "SlackSigningSecret" "secret")"
SLACK_CLIENT_SECRET="$(required_from_env_or_prompt "SLACK_CLIENT_SECRET" "SlackClientSecret" "secret")"
SLACK_CLIENT_ID="$(required_from_env_or_prompt "SLACK_CLIENT_ID" "SlackClientID")"

ENV_DATABASE_HOST="${DATABASE_HOST:-}"
ENV_DATABASE_PORT="${DATABASE_PORT:-}"
DATABASE_HOST=""
DATABASE_PORT=""
DB_EFFECTIVE_PORT=""
DATABASE_SCHEMA=""
DATABASE_SCHEMA_DEFAULT="syncbot_${STAGE}"
if [[ "$IS_STACK_UPDATE" == "true" && -n "$PREV_DATABASE_SCHEMA" ]]; then
  DATABASE_SCHEMA_DEFAULT="$PREV_DATABASE_SCHEMA"
fi

if [[ "$DATABASE_BACKEND" != "sqlite" ]]; then
  echo
  echo "=== Database Host ==="
  echo "Create the database and app user first (see docs/DEPLOY.md). Pass the full DATABASE_USER"
  echo "(including any TiDB cluster prefix). The host must be reachable from public Lambda (no VPC)."
  DATABASE_HOST_DEFAULT="YOUR_DATABASE_HOST"
  [[ -n "$PREV_DATABASE_HOST" ]] && DATABASE_HOST_DEFAULT="$PREV_DATABASE_HOST"
  DATABASE_HOST="$(resolve_with_conflict_check \
    "DATABASE_HOST (database hostname)" \
    "$ENV_DATABASE_HOST" \
    "$PREV_DATABASE_HOST" \
    "$DATABASE_HOST_DEFAULT")"

  echo
  echo "Database name (DatabaseSchema): use syncbot_${STAGE} or similar so each stage has its own DB on a shared host"
  echo "(e.g. syncbot_test, syncbot_prod). The default below includes the stage you chose."
  DATABASE_SCHEMA="$(prompt_default "DatabaseSchema" "$DATABASE_SCHEMA_DEFAULT")"

  echo
  echo "=== Database port ==="
  echo "Leave port blank to use the engine default (3306 MySQL, 5432 PostgreSQL). TiDB Cloud uses 4000."
  DEFAULT_DB_PORT=""
  [[ -n "$PREV_DATABASE_PORT" ]] && DEFAULT_DB_PORT="$PREV_DATABASE_PORT"
  DATABASE_PORT="$(resolve_with_conflict_check \
    "DATABASE_PORT (optional)" \
    "$ENV_DATABASE_PORT" \
    "$PREV_DATABASE_PORT" \
    "$DEFAULT_DB_PORT")"
  if [[ "$DATABASE_BACKEND" == "mysql" && "$DATABASE_PORT" == "3306" ]]; then
    DATABASE_PORT=""
  fi
  if [[ "$DATABASE_BACKEND" == "postgresql" && "$DATABASE_PORT" == "5432" ]]; then
    DATABASE_PORT=""
  fi
  DB_EFFECTIVE_PORT="3306"
  [[ "$DATABASE_BACKEND" == "postgresql" ]] && DB_EFFECTIVE_PORT="5432"
  [[ -n "$DATABASE_PORT" ]] && DB_EFFECTIVE_PORT="$DATABASE_PORT"

  if [[ -z "$DATABASE_HOST" || "$DATABASE_HOST" == REPLACE_ME* || "$DATABASE_HOST" == YOUR_* ]]; then
    echo "Error: valid DATABASE_HOST is required when DATABASE_BACKEND is not sqlite." >&2
    exit 1
  fi
else
  DATABASE_SCHEMA="$DATABASE_SCHEMA_DEFAULT"
  echo
  echo "SQLite + Litestream: file /tmp/syncbot.db, replica in S3. Reserved concurrency 1."
  echo "Cold starts restore from S3 and can miss Slack's 3s window; keep-warm is recommended."
fi

echo
if [[ -n "$PREV_ENABLE_KEEP_WARM" ]]; then
  ENABLE_KEEP_WARM="$PREV_ENABLE_KEEP_WARM"
fi
if prompt_yes_no "Enable keep-warm EventBridge ping every 5 minutes (recommended)?" "$([[ "${ENABLE_KEEP_WARM}" == "false" ]] && echo n || echo y)"; then
  ENABLE_KEEP_WARM="true"
else
  ENABLE_KEEP_WARM="false"
fi

echo
echo "=== App Secrets ==="

if [[ -z "${DATA_ENCRYPTION_KEY:-}" ]]; then
  DATA_ENCRYPTION_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(36))')"
  echo "Generated DATA_ENCRYPTION_KEY=$DATA_ENCRYPTION_KEY"
  echo "IMPORTANT: Store this key securely. You need it for disaster recovery."
fi
DATA_ENCRYPTION_KEY="$(required_from_env_or_prompt "DATA_ENCRYPTION_KEY" "DataEncryptionKey" "secret")"

DATABASE_PASSWORD=""
DATABASE_USER=""
if [[ "$DATABASE_BACKEND" != "sqlite" ]]; then
  DATABASE_PASSWORD="$(required_from_env_or_prompt "DATABASE_PASSWORD" "DatabasePassword" "secret")"
  DATABASE_USER="$(required_from_env_or_prompt "DATABASE_USER" "DatabaseUser (full app username, including any TiDB prefix)")"
fi

LOG_LEVEL_DEFAULT="INFO"
if [[ "$IS_STACK_UPDATE" == "true" && -n "$PREV_LOG_LEVEL" ]]; then
  LOG_LEVEL_DEFAULT="$PREV_LOG_LEVEL"
fi

PRIMARY_WORKSPACE="${PREV_PRIMARY_WORKSPACE:-}"
ENABLE_DB_RESET="${PREV_ENABLE_DB_RESET:-}"
DATABASE_TLS_ENABLED="${PREV_DB_TLS:-}"
DATABASE_SSL_CA_PATH="${PREV_DB_SSL_CA:-}"

echo
echo "=== Log Level ==="
LOG_LEVEL="$(prompt_log_level "$LOG_LEVEL_DEFAULT")"

echo
echo "=== App Settings ==="
PRIMARY_WORKSPACE="$(prompt_primary_workspace "$PRIMARY_WORKSPACE")"

echo
echo "=== Deploy Summary ==="
echo "Region:           $REGION"
echo "Stack:            $STACK_NAME"
echo "Stage:            $STAGE"
echo "Log level:        $LOG_LEVEL"
echo "Keep-warm:        $ENABLE_KEEP_WARM"
if [[ -n "$PRIMARY_WORKSPACE" ]]; then
  echo "Primary workspace: $PRIMARY_WORKSPACE"
else
  echo "Primary workspace: (not set — backup/restore hidden)"
fi
if [[ "$ENABLE_DB_RESET" == "true" ]]; then
  echo "DB reset:          enabled (PRIMARY_WORKSPACE must match)"
else
  echo "DB reset:          (disabled)"
fi
echo "Deploy bucket:    $S3_BUCKET"
if [[ "$DATABASE_BACKEND" != "sqlite" ]]; then
  echo "DB backend:       $DATABASE_BACKEND"
  echo "DB host:          $DATABASE_HOST"
  echo "DB port:          ${DB_EFFECTIVE_PORT:-engine default}"
  echo "DB schema:        $DATABASE_SCHEMA"
  echo "DB user:          $DATABASE_USER"
else
  echo "DB backend:       sqlite + Litestream to S3"
fi
echo "Token encryption: provided (NoEcho SAM parameter)"
echo

if ! prompt_yes_no "Proceed with build + deploy?" "y"; then
  echo "Aborted."
  exit 0
fi

echo
echo "=== Preflight ==="
handle_unhealthy_stack_state "$STACK_NAME" "$REGION"
abort_if_stack_managed_rds "$STACK_NAME" "$REGION"

echo

PARAMS=(
  "Stage=$STAGE"
  "DatabaseBackend=$DATABASE_BACKEND"
  "EnableKeepWarm=$ENABLE_KEEP_WARM"
  "SlackSigningSecret=$SLACK_SIGNING_SECRET"
  "SlackClientSecret=$SLACK_CLIENT_SECRET"
  "DatabaseSchema=$DATABASE_SCHEMA"
  "DataEncryptionKey=$DATA_ENCRYPTION_KEY"
  "DatabasePassword=${DATABASE_PASSWORD:-}"
  "LogLevel=$LOG_LEVEL"
)
[[ -n "${DATABASE_USER:-}" ]] && PARAMS+=("DatabaseUser=$DATABASE_USER")
[[ -n "$PRIMARY_WORKSPACE" ]] && PARAMS+=("PrimaryWorkspace=$PRIMARY_WORKSPACE")
[[ -n "$ENABLE_DB_RESET" ]] && PARAMS+=("EnableDbReset=$ENABLE_DB_RESET")
[[ -n "$DATABASE_TLS_ENABLED" ]] && PARAMS+=("DatabaseTlsEnabled=$DATABASE_TLS_ENABLED")
[[ -n "$DATABASE_SSL_CA_PATH" ]] && PARAMS+=("DatabaseSslCaPath=$DATABASE_SSL_CA_PATH")
PARAMS+=("EnableXRay=${AWS_ENABLE_XRAY:-false}")
[[ -n "$SLACK_CLIENT_ID" ]] && PARAMS+=("SlackClientID=$SLACK_CLIENT_ID")
if [[ "$DATABASE_BACKEND" != "sqlite" ]]; then
  PARAMS+=("DatabaseHost=$DATABASE_HOST")
  [[ -n "$DATABASE_PORT" ]] && PARAMS+=("DatabasePort=$DATABASE_PORT")
fi
PARAMS+=(
  "SlackOauthBotScopes=${SLACK_BOT_SCOPES:-app_mentions:read,channels:history,channels:join,channels:read,channels:manage,chat:write,chat:write.customize,emoji:read,files:read,files:write,groups:history,groups:read,groups:write,im:write,reactions:read,reactions:write,team:read,usergroups:read,users:read,users:read.email}"
  "SlackOauthUserScopes=${SLACK_USER_SCOPES:-chat:write,channels:history,channels:read,files:read,files:write,groups:history,groups:read,groups:write,reactions:read,reactions:write,team:read,users:read,users:read.email}"
)

echo "=== SAM Build ==="
echo "Building app..."
sam build -t "$APP_TEMPLATE" --build-in-source

echo "=== SAM Deploy ==="
echo "Deploying stack..."
sam_deploy_or_fallback

APP_OUTPUTS="$(app_describe_outputs "$STACK_NAME" "$REGION")"

  FUNCTION_ARN="$(output_value "$APP_OUTPUTS" "SyncBotFunctionArn")"
  if [[ -n "$FUNCTION_ARN" ]]; then
    echo "=== Lambda migrate + warm-up ==="
    "$REPO_ROOT/infra/aws/scripts/invoke_lambda_migrate.sh" "$FUNCTION_ARN" "$REGION"
  fi

else
  echo
  echo "Skipping Build/Deploy (task 2 not selected)."
  APP_OUTPUTS="${EXISTING_STACK_OUTPUTS:-}"
  DATABASE_BACKEND="${PREV_DATABASE_BACKEND:-mysql}"
  DATABASE_SCHEMA="${PREV_DATABASE_SCHEMA:-}"
  [[ -z "$DATABASE_SCHEMA" ]] && DATABASE_SCHEMA="syncbot_${STAGE}"
  DATABASE_HOST="${PREV_DATABASE_HOST:-}"
  DATABASE_PORT="${PREV_DATABASE_PORT:-}"
  ENABLE_KEEP_WARM="${PREV_ENABLE_KEEP_WARM:-${ENABLE_KEEP_WARM:-true}}"
  SLACK_SIGNING_SECRET="${SLACK_SIGNING_SECRET:-}"
  SLACK_CLIENT_SECRET="${SLACK_CLIENT_SECRET:-}"
  SLACK_CLIENT_ID="${SLACK_CLIENT_ID:-}"
fi

SYNCBOT_API_URL="$(output_value "$APP_OUTPUTS" "SyncBotApiUrl")"
SYNCBOT_INSTALL_URL="$(output_value "$APP_OUTPUTS" "SyncBotInstallUrl")"

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

if [[ "$TASK_CICD" == "true" ]]; then
  configure_github_actions_aws \
    "$BOOTSTRAP_OUTPUTS" \
    "$BOOTSTRAP_STACK" \
    "$REGION" \
    "$STACK_NAME" \
    "$STAGE" \
    "$DATABASE_SCHEMA" \
    "$DATABASE_BACKEND" \
    "$DATABASE_HOST" \
    "${DATABASE_PORT:-}"
fi

# --- Save config to env file ---
echo
if prompt_yes_no "Save config to .env.deploy.${STAGE} for future deploys?" "y"; then
  ENV_SAVE_FILE="$REPO_ROOT/.env.deploy.${STAGE}"
  {
    echo "# Generated by deploy.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "CLOUD_PROVIDER=aws"
    echo "AWS_REGION=$REGION"
    echo "AWS_STACK_NAME=$STACK_NAME"
    echo "AWS_BOOTSTRAP_STACK_NAME=$BOOTSTRAP_STACK"
    echo "DATABASE_BACKEND=$DATABASE_BACKEND"
    echo "ENABLE_KEEP_WARM=$ENABLE_KEEP_WARM"
    echo ""
    echo "SLACK_SIGNING_SECRET=$SLACK_SIGNING_SECRET"
    echo "SLACK_CLIENT_SECRET=$SLACK_CLIENT_SECRET"
    echo "SLACK_CLIENT_ID=$SLACK_CLIENT_ID"
    echo ""
    echo "DATA_ENCRYPTION_KEY=$DATA_ENCRYPTION_KEY"
    echo ""
    if [[ "$DATABASE_BACKEND" != "sqlite" ]]; then
      echo "DATABASE_HOST=${DATABASE_HOST:-}"
      [[ -n "${DB_EFFECTIVE_PORT:-}" ]] && echo "DATABASE_PORT=$DB_EFFECTIVE_PORT"
      echo "DATABASE_USER=${DATABASE_USER:-}"
      echo "DATABASE_PASSWORD=$DATABASE_PASSWORD"
      echo "DATABASE_SCHEMA=$DATABASE_SCHEMA"
      [[ -n "${DATABASE_TLS_ENABLED:-}" ]] && echo "DATABASE_TLS_ENABLED=$DATABASE_TLS_ENABLED"
    fi
  } > "$ENV_SAVE_FILE"
  chmod 600 "$ENV_SAVE_FILE"
  echo "Saved to $ENV_SAVE_FILE"
  echo "Next time: ./deploy.sh --env $STAGE"
fi

# --- Push to GitHub (if --setup-github and TASK_CICD was not already run) ---
if [[ "${SETUP_GITHUB:-}" == "true" && "$TASK_CICD" != "true" ]]; then
  echo
  echo "=== Push to GitHub Environment ==="
  prereqs_require_cmd gh prereqs_hint_gh_cli
  if ! gh auth status >/dev/null 2>&1; then
    echo "Error: gh CLI not authenticated. Run 'gh auth login' first." >&2
    exit 1
  fi
  REPO="$(prompt_github_repo_for_actions "$REPO_ROOT")"
  ENV_NAME="$STAGE"
  ROLE_ARN="${AWS_ROLE_TO_ASSUME:-$(output_value "$BOOTSTRAP_OUTPUTS" "GitHubDeployRoleArn")}"
  push_github_aws_ci_config "$REPO" "$ENV_NAME" "$ROLE_ARN" "$S3_BUCKET" "$REGION" "$STACK_NAME"
  echo "GitHub environment '$ENV_NAME' updated for repo $REPO."
fi

echo
echo "=== Deploy Receipt ==="
write_deploy_receipt

echo
echo "=== Deploy Complete ==="
echo "Stack:       $STACK_NAME"
echo "Region:      $REGION"
echo "API URL:     ${SYNCBOT_API_URL:-n/a}"
echo "Install URL: ${SYNCBOT_INSTALL_URL:-n/a}"
if [[ -n "${SYNCBOT_API_URL:-}" ]]; then
  echo "OAuth URL:   ${SYNCBOT_API_URL%/slack/events}/slack/oauth_redirect"
fi
