#!/usr/bin/env bash
# SyncBot infra-agnostic deploy launcher.
# Discovers provider scripts at infra/<provider>/scripts/deploy.sh and runs
# the one named by CLOUD_PROVIDER in .env.deploy.<stage>.
#
# Phases when executed as ./deploy.sh (not when sourced):
#   1) Parse flags (--env, --bootstrap, --setup-github, --verbose, --update-stack)
#   2) Load .env.deploy.<stage> and require CLOUD_PROVIDER
#   3) Exec infra/<CLOUD_PROVIDER>/scripts/deploy.sh
#
# Prerequisite helpers below are also sourced by infra/*/scripts/deploy.sh:
#   source "$REPO_ROOT/deploy.sh"
# Also includes prompt_deploy_tasks_aws / prompt_deploy_tasks_gcp for multi-select deploy steps.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"

# ---------------------------------------------------------------------------
# Prerequisite helpers (shared with infra/aws and infra/gcp deploy scripts).
# macOS: Homebrew one-liners where common. Otherwise: vendor install documentation
# (Darwin / Linux / other uname from uname -s only — no platform-specific logic beyond that).
# Root: ./deploy.sh; alternate entrypoint: deploy.ps1 in repo root (see README).
# ---------------------------------------------------------------------------

prereqs_hint_aws_cli() {
  echo "Install AWS CLI v2:"
  case "$(uname -s 2>/dev/null)" in
    Darwin) echo "  brew install awscli" ;;
    Linux) echo "  https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html" ;;
    *) echo "  https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html" ;;
  esac
  echo "  User guide: https://docs.aws.amazon.com/cli/latest/userguide/cli-chap-welcome.html"
}

prereqs_hint_sam_cli() {
  echo "Install AWS SAM CLI:"
  case "$(uname -s 2>/dev/null)" in
    Darwin) echo "  brew install aws-sam-cli" ;;
    *)
      echo "  https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html"
      ;;
  esac
  echo "  Developer guide: https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/what-is-sam.html"
}

prereqs_hint_terraform() {
  echo "Install Terraform:"
  echo "  https://developer.hashicorp.com/terraform/install"
  echo "  Introduction: https://developer.hashicorp.com/terraform/docs"
}

prereqs_hint_gcloud() {
  echo "Install Google Cloud SDK:"
  case "$(uname -s 2>/dev/null)" in
    Darwin) echo "  brew install --cask google-cloud-sdk" ;;
    *) echo "  https://cloud.google.com/sdk/docs/install" ;;
  esac
  echo "  gcloud CLI reference: https://cloud.google.com/sdk/gcloud/reference"
}

prereqs_hint_gh_cli() {
  echo "Install GitHub CLI (gh):"
  case "$(uname -s 2>/dev/null)" in
    Darwin) echo "  brew install gh" ;;
    Linux) echo "  https://github.com/cli/cli/blob/trunk/docs/install_linux.md" ;;
    *) echo "  https://cli.github.com/" ;;
  esac
  echo "  Manual: https://cli.github.com/manual/"
}

prereqs_hint_python3() {
  echo "Install Python 3.12+ (the deploy helpers use python3 for manifest/JSON helpers):"
  echo "  https://www.python.org/downloads/"
  echo "  Documentation: https://docs.python.org/3/"
}

prereqs_hint_curl() {
  echo "Install curl (used for Slack manifest API and downloads):"
  echo "  https://curl.se/download.html"
}

prereqs_hint_slack_apps_docs() {
  echo "Slack apps (browser) and API tokens (optional manifest automation):"
  echo "  https://api.slack.com/apps"
  echo "  https://api.slack.com/authentication/token-types"
  echo "Manifest API (apps.manifest.update / create):"
  echo "  https://api.slack.com/reference/methods/apps.manifest.update"
}

prereqs_icon_ok() {
  printf '\033[0;32m✓\033[0m'
}

prereqs_icon_optional() {
  printf '\033[1;33m!\033[0m'
}

prereqs_icon_required_missing() {
  printf '\033[0;31m✗\033[0m'
}

prereqs_prompt_continue_without_optional() {
  local answer
  read -r -p "Do you want to proceed? [Y/n]: " answer
  if [[ -z "$answer" || "$answer" =~ ^[Yy]$ ]]; then
    return 0
  fi
  return 1
}

prereqs_print_cli_status_matrix() {
  local provider="$1"
  shift
  local name
  echo "" >&2
  echo "=== CLI Prerequisites ($provider) ===" >&2
  for name in "$@"; do
    if command -v "$name" >/dev/null 2>&1; then
      printf '  %s: %s\n' "$name" "$(prereqs_icon_ok)" >&2
    else
      printf '  %s: %s\n' "$name" "$(prereqs_icon_required_missing)" >&2
    fi
  done
  if command -v gh >/dev/null 2>&1; then
    printf '  gh: %s\n' "$(prereqs_icon_ok)" >&2
  else
    printf '  gh: %s\n' "$(prereqs_icon_optional)" >&2
    echo "" >&2
    echo "The GitHub gh command was not found; install it for automated GitHub repository setup." >&2
    prereqs_hint_gh_cli >&2
    echo "" >&2
    if ! prereqs_prompt_continue_without_optional; then
      echo "Exiting. Install gh and rerun, or answer Y to continue without it." >&2
      exit 1
    fi
  fi
  echo "" >&2
  prereqs_hint_slack_apps_docs >&2
  echo "" >&2
}

prereqs_require_cmd() {
  local cmd="$1"
  local hint_fn="${2:-}"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Error: required command '$cmd' not found in PATH." >&2
    if [[ -n "$hint_fn" ]] && declare -F "$hint_fn" >/dev/null 2>&1; then
      "$hint_fn" >&2
    fi
    exit 1
  fi
}

# Slack Web API responses can be large; avoid flooding the terminal on errors (AWS/GCP deploy scripts).
slack_api_echo_truncated_body() {
  local body="$1"
  local max_len="${2:-500}"
  if [[ -z "$body" ]]; then
    echo "(empty response)"
    return 0
  fi
  if [[ ${#body} -gt max_len ]]; then
    echo "${body:0:max_len}... (truncated, ${#body} chars total)"
  else
    echo "$body"
  fi
}

# Log level (shared by infra/aws and infra/gcp deploy scripts; matches syncbot/logger.py LOG_LEVEL).
is_valid_log_level() {
  case "$1" in
    DEBUG | INFO | WARNING | ERROR | CRITICAL) return 0 ;;
    *) return 1 ;;
  esac
}

normalize_log_level() {
  echo "$1" | tr "[:lower:]" "[:upper:]"
}

# Menu order: DEBUG first (1), then INFO..CRITICAL. Matches Python logging severity order.
log_level_to_menu_index() {
  case "$(normalize_log_level "$1")" in
    DEBUG) echo 1 ;;
    INFO) echo 2 ;;
    WARNING) echo 3 ;;
    ERROR) echo 4 ;;
    CRITICAL) echo 5 ;;
    *) echo 2 ;;
  esac
}

menu_index_to_log_level() {
  case "$1" in
    1) echo DEBUG ;;
    2) echo INFO ;;
    3) echo WARNING ;;
    4) echo ERROR ;;
    5) echo CRITICAL ;;
    *) return 1 ;;
  esac
}

prompt_log_level() {
  local default_level="$1"
  local default_idx choice i name suf
  default_idx="$(log_level_to_menu_index "$default_level")"

  echo >&2
  for i in 1 2 3 4 5; do
    name="$(menu_index_to_log_level "$i")"
    suf=""
    [[ "$i" == "$default_idx" ]] && suf=" (default/current)"
    echo "  $i) $name$suf" >&2
  done

  while true; do
    read -r -p "Choose level [$default_idx]: " choice
    [[ -z "$choice" ]] && choice="$default_idx"
    case "$choice" in
      1 | 2 | 3 | 4 | 5)
        menu_index_to_log_level "$choice"
        return 0
        ;;
    esac
    echo "Invalid choice: $choice. Enter a number from 1 to 5." >&2
  done
}

# App settings (used by infra/aws and infra/gcp deploy scripts). Hints on stderr; value on stdout.

prompt_primary_workspace() {
  local default="$1"
  echo "Slack Team ID for PRIMARY_WORKSPACE (required for backup/restore to appear; also scopes DB reset)." >&2
  echo "Leave empty to hide backup/restore from all workspaces." >&2
  local disp
  if [[ -z "$default" ]]; then
    disp="(any workspace)"
  else
    disp="$default"
  fi
  local v
  read -r -p "PRIMARY_WORKSPACE (Slack Team ID) [$disp]: " v
  v="${v:-$default}"
  case "$(echo "$v" | tr "[:upper:]" "[:lower:]")" in
    "" | none) echo "" ;;
    *) echo "$v" ;;
  esac
}

# Parse owner/repo from a github.com git remote URL (ssh, https, ssh://). Empty if not GitHub.
github_owner_repo_from_url() {
  local url="$1"
  url="${url%.git}"
  url="${url%/}"
  if [[ "$url" =~ ^git@github\.com:([^/]+)/(.+)$ ]]; then
    echo "${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
    return 0
  fi
  if [[ "$url" =~ ^ssh://git@github\.com/([^/]+)/(.+)$ ]]; then
    echo "${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
    return 0
  fi
  if [[ "$url" =~ ^https://([^/@]+@)?github\.com/([^/]+)/([^/]+)$ ]]; then
    echo "${BASH_REMATCH[2]}/${BASH_REMATCH[3]}"
    return 0
  fi
  return 1
}

# Emit owner/repo for GitHub Actions variables. Uses git remotes (origin, upstream, others) so forks
# are not confused with `gh repo view` (which often follows upstream). If there are no github.com
# remotes, falls back to `gh repo view` or a manual prompt. Prints chosen repo to stdout; hints to stderr.
prompt_github_repo_for_actions() {
  local git_dir="${1:-$REPO_ROOT}"
  local canon tmp url or n gh_inf nlines choice i line or_only lab_only
  local _cr_done
  _cr_done() {
    rm -f "$canon" "$tmp"
  }
  canon="$(mktemp)"
  tmp="$(mktemp)"

  if [[ -n "${GITHUB_REPO:-}" ]]; then
    if [[ "$GITHUB_REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
      echo "Using GitHub repository: $GITHUB_REPO (from GITHUB_REPO env var)" >&2
      _cr_done
      echo "$GITHUB_REPO"
      return 0
    fi
    echo "Warning: GITHUB_REPO is set but invalid (expected owner/repo); ignoring." >&2
  fi

  if ! git -C "$git_dir" rev-parse --git-dir >/dev/null 2>&1; then
    echo "Not a git checkout; enter GitHub owner/repo manually." >&2
    while true; do
      read -r -p "GitHub repository (owner/repo): " choice
      if [[ "$choice" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
        _cr_done
        echo "$choice"
        return 0
      fi
      echo "Expected owner/repo (e.g. myorg/syncbot)." >&2
    done
  fi

  _github_repo_add_unique() {
    local o="$1"
    local label="$2"
    [[ -z "$o" ]] && return
    if ! grep -Fxq "$o" "$tmp" 2>/dev/null; then
      echo "$o" >>"$tmp"
      printf '%s\t%s\n' "$o" "$label" >>"$canon"
    fi
  }

  for n in origin upstream; do
    url="$(git -C "$git_dir" remote get-url "$n" 2>/dev/null || true)"
    or="$(github_owner_repo_from_url "$url" || true)"
    _github_repo_add_unique "$or" "git remote $n"
  done
  while IFS= read -r n; do
    [[ "$n" == "origin" || "$n" == "upstream" ]] && continue
    url="$(git -C "$git_dir" remote get-url "$n" 2>/dev/null || true)"
    or="$(github_owner_repo_from_url "$url" || true)"
    _github_repo_add_unique "$or" "git remote $n"
  done < <(git -C "$git_dir" remote 2>/dev/null | LC_ALL=C sort)

  # Do not merge in `gh repo view` when remotes exist: gh often tracks upstream and
  # disagrees with the fork (origin) the user wants for Actions variables.

  nlines="$(wc -l <"$canon" | tr -d ' ')"
  gh_inf=""
  if [[ "$nlines" -eq 0 ]] && command -v gh >/dev/null 2>&1; then
    gh_inf="$(gh -C "$git_dir" repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)"
  fi

  if [[ "$nlines" -eq 0 ]]; then
    if [[ -n "$gh_inf" ]]; then
      read -r -p "GitHub repository for Actions variables [$gh_inf] (from gh; no github.com remotes): " choice
      choice="${choice:-$gh_inf}"
      if [[ "$choice" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
        echo "Using GitHub repository: $choice" >&2
        _cr_done
        echo "$choice"
        return 0
      fi
      echo "Using GitHub repository: $gh_inf" >&2
      _cr_done
      echo "$gh_inf"
      return 0
    fi
    echo "Could not detect owner/repo from remotes. Enter it manually." >&2
    while true; do
      read -r -p "GitHub repository (owner/repo): " choice
      if [[ "$choice" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
        _cr_done
        echo "$choice"
        return 0
      fi
      echo "Expected owner/repo (e.g. myorg/syncbot)." >&2
    done
  fi

  if [[ "$nlines" -eq 1 ]]; then
    IFS=$'\t' read -r or_only lab_only <"$canon"
    read -r -p "GitHub repository for Actions variables [$or_only] ($lab_only): " choice
    choice="${choice:-$or_only}"
    if [[ "$choice" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
      echo "Using GitHub repository: $choice" >&2
      _cr_done
      echo "$choice"
      return 0
    fi
    echo "Invalid owner/repo; using $or_only." >&2
    _cr_done
    echo "$or_only"
    return 0
  fi

  echo "Multiple GitHub repositories detected (fork vs upstream, etc.). Choose where to set Actions variables and secrets:" >&2
  i=1
  while IFS=$'\t' read -r or lab_only; do
    echo "  $i) $or  ($lab_only)" >&2
    i=$((i + 1))
  done <"$canon"

  while true; do
    read -r -p "Enter number [1-$nlines] or owner/repo: " choice
    [[ -z "$choice" ]] && choice=1
    if [[ "$choice" =~ ^[0-9]+$ ]]; then
      line="$(sed -n "${choice}p" "$canon")"
      if [[ -n "$line" ]]; then
        IFS=$'\t' read -r or_only lab_only <<<"$line"
        echo "Using GitHub repository: $or_only" >&2
        _cr_done
        echo "$or_only"
        return 0
      fi
    fi
    if [[ "$choice" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
      echo "Using GitHub repository: $choice" >&2
      _cr_done
      echo "$choice"
      return 0
    fi
    echo "Invalid choice. Enter 1-$nlines or owner/repo." >&2
  done
}

# ---------------------------------------------------------------------------
# Deploy task selection (used by infra/aws and infra/gcp deploy scripts).
# Sets global variables named in flag_names to "true" or "false".
# ---------------------------------------------------------------------------

_prompt_deploy_tasks_parsechoices() {
  local choices_raw="${1:-}"
  shift
  local -a flag_names=("$@")
  local n="${#flag_names[@]}"
  local i name def="" part idx
  for name in "${flag_names[@]}"; do
    eval "${name}=false"
  done
  for ((i = 1; i <= n; i++)); do
    [[ -n "$def" ]] && def+=","
    def+="$i"
  done
  local choices="${choices_raw// /}"
  [[ -z "$choices" ]] && choices="$def"
  IFS=',' read -r -a parts <<<"$choices"
  for part in "${parts[@]}"; do
    part="${part// /}"
    [[ -z "$part" ]] && continue
    if [[ "$part" =~ ^[0-9]+$ ]]; then
      idx="$part"
      if [[ "$idx" -ge 1 && "$idx" -le "$n" ]]; then
        eval "${flag_names[$((idx - 1))]}=true"
      else
        echo "Invalid task number: $part (must be 1-$n)" >&2
        exit 1
      fi
    else
      echo "Invalid task selection: $part (use comma-separated numbers)" >&2
      exit 1
    fi
  done
}

prompt_deploy_tasks_aws() {
  echo "=== Deploy Tasks ==="
  printf '  1) %s\n' "Build/Deploy - SAM build + deploy"
  printf '  2) %s\n' "CI/CD - GitHub Actions configuration"
  printf '  3) %s\n' "Slack API - Configure Slack app via API"
  local default_all="1,2,3"
  local choices=""
  read -r -e -p "Select tasks (comma-separated) [$default_all]: " choices
  choices="${choices:-$default_all}"
  _prompt_deploy_tasks_parsechoices "$choices" TASK_BUILD_DEPLOY TASK_CICD TASK_SLACK_API
}

prompt_deploy_tasks_gcp() {
  echo "=== Deploy Tasks ==="
  printf '  1) %s\n' "Build/Deploy - Terraform plan + apply"
  printf '  2) %s\n' "CI/CD - GitHub Actions configuration"
  printf '  3) %s\n' "Slack API - Configure Slack app via API"
  local default_all="1,2,3"
  local choices=""
  read -r -e -p "Select tasks (comma-separated) [$default_all]: " choices
  choices="${choices:-$default_all}"
  _prompt_deploy_tasks_parsechoices "$choices" TASK_BUILD_DEPLOY TASK_CICD TASK_SLACK_API
}

# ---------------------------------------------------------------------------
# Env file helpers (shared with provider scripts for save-back).
# ---------------------------------------------------------------------------

update_env_file() {
  local file="$1" key="$2" value="$3"
  if [[ ! -f "$file" ]]; then
    echo "${key}=${value}" >> "$file"
    return
  fi
  if grep -q "^${key}=" "$file" 2>/dev/null; then
    sed -i.bak "s|^${key}=.*|${key}=${value}|" "$file" && rm -f "${file}.bak"
  elif grep -q "^# *${key}=" "$file" 2>/dev/null; then
    sed -i.bak "s|^# *${key}=.*|${key}=${value}|" "$file" && rm -f "${file}.bak"
  else
    echo "${key}=${value}" >> "$file"
  fi
}

# When sourced by infra/*/scripts/deploy.sh, only load helpers above.
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
  return 0
fi

# ---------------------------------------------------------------------------
# Launcher (only when this file is executed: ./deploy.sh)
# ---------------------------------------------------------------------------

usage() {
  cat <<EOF
Usage: ./deploy.sh [--env <stage>] [--bootstrap] [--setup-github] [--verbose]
                   [--update-stack]

Platform comes from CLOUD_PROVIDER in .env.deploy.<stage> (aws or gcp).
There is no aws/gcp command-line argument.

Options:
  --env <stage>     Source .env.deploy.<stage> and run a non-interactive deploy
                    (e.g. --env test, --env prod).
  --bootstrap       AWS only. Force bootstrap stack create/sync even if the
                    template hash already matches. First AWS deploy creates
                    the bootstrap stack automatically when it is missing.
  --setup-github    Push config to GitHub environment vars/secrets after deploy.
                    Works with --env (non-interactive) or interactive deploys.
  --verbose         Extended deploy receipts (SAM/Terraform parameters, inline
                    Slack manifest) and extra screen output for debugging.
  --update-stack    AWS only. Skip sam deploy and use aws cloudformation
                    update-stack directly (no changeset; bypasses early
                    validation). Normally unnecessary: the AWS deploy script
                    auto-retries with update-stack when CloudFormation rejects
                    a changeset with EarlyValidation::ResourceExistenceCheck.

No --env:
  Prompt for stage, load that env file, then the provider script's interactive
  task menu (build / CI/CD / Slack API as applicable).

Examples:
  ./deploy.sh
  ./deploy.sh --env test
  ./deploy.sh --env test --bootstrap
  ./deploy.sh --env prod --setup-github
  ./deploy.sh --setup-github
  ./deploy.sh --env test --verbose
  ./deploy.sh --env test --update-stack
EOF
}

discover_deploy_scripts() {
  local path provider
  shopt -s nullglob
  for path in "$REPO_ROOT"/infra/*/scripts/deploy.sh; do
    provider="$(basename "$(dirname "$(dirname "$path")")")"
    printf "%s\t%s\n" "$provider" "$path"
  done | sort
  shopt -u nullglob
}

resolve_script_from_cloud_provider() {
  local entries="$1"
  local wanted="$2"
  local provider path
  while IFS=$'\t' read -r provider path; do
    [[ -z "$provider" ]] && continue
    if [[ "$provider" == "$wanted" ]]; then
      echo "$path"
      return 0
    fi
  done <<< "$entries"
  return 1
}

load_deploy_env_file() {
  local env_name="$1"
  local env_file="$REPO_ROOT/.env.deploy.$env_name"
  if [[ ! -f "$env_file" ]]; then
    echo "Error: env file not found: $env_file" >&2
    echo "Copy .env.deploy.example to $env_file, set CLOUD_PROVIDER, and fill in values." >&2
    exit 1
  fi
  echo "=== Loading $env_file ==="
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
  export STAGE="$env_name"
  export ENV_FILE_PATH="$env_file"
  if [[ -z "${CLOUD_PROVIDER:-}" ]]; then
    echo "Error: CLOUD_PROVIDER is unset in $env_file (set aws or gcp)." >&2
    exit 1
  fi
}

main() {
  local env_name="" setup_github="false" bootstrap="false" verbose="false" update_stack="false"
  local env_from_flag="false"

  while [[ $# -gt 0 ]]; do
    case "$1" in
      -h | --help | help)
        usage
        exit 0
        ;;
      --env)
        shift
        env_name="${1:?--env requires a stage name (e.g. test, prod)}"
        env_from_flag="true"
        shift
        ;;
      --setup-github)
        setup_github="true"
        shift
        ;;
      --bootstrap)
        bootstrap="true"
        shift
        ;;
      --verbose)
        verbose="true"
        shift
        ;;
      --update-stack)
        update_stack="true"
        shift
        ;;
      *)
        echo "Error: unexpected argument '$1'." >&2
        echo "Platform is CLOUD_PROVIDER in .env.deploy.<stage>, not a CLI argument." >&2
        echo >&2
        usage
        exit 1
        ;;
    esac
  done

  export SETUP_GITHUB="$setup_github"
  export BOOTSTRAP="$bootstrap"
  export VERBOSE="$verbose"

  if [[ -z "$env_name" ]]; then
    local stage_choice
    read -r -p "Stage (test/prod) [test]: " stage_choice
    env_name="${stage_choice:-test}"
  fi

  load_deploy_env_file "$env_name"
  if [[ "$env_from_flag" == "true" ]]; then
    export ENV_FILE_LOADED="true"
  fi

  # AWS_UPDATE_STACK: CLI --update-stack wins; else keep value from .env.deploy.* if set.
  if [[ "$update_stack" == "true" ]]; then
    export AWS_UPDATE_STACK=true
  fi
  export UPDATE_STACK="${AWS_UPDATE_STACK:-${UPDATE_STACK:-false}}"
  export AWS_UPDATE_STACK="${AWS_UPDATE_STACK:-$UPDATE_STACK}"

  local entries
  entries="$(discover_deploy_scripts)"
  if [[ -z "$entries" ]]; then
    echo "No deploy scripts found under infra/*/scripts/deploy.sh" >&2
    exit 1
  fi

  local script_path
  if ! script_path="$(resolve_script_from_cloud_provider "$entries" "$CLOUD_PROVIDER")"; then
    echo "Error: no deploy script for CLOUD_PROVIDER=$CLOUD_PROVIDER (expected infra/$CLOUD_PROVIDER/scripts/deploy.sh)." >&2
    exit 1
  fi

  # Match GitHub Actions: install committed pins. Do not run poetry update or rewrite
  # poetry.lock / *requirements.txt. Warn if an export from the lockfile differs.
  if command -v poetry &>/dev/null \
    && poetry self show plugins 2>/dev/null | grep -q poetry-plugin-export; then
    echo "=== Check Python dependency export ==="
    _export_tmp="$(mktemp -d)"
    if poetry export -f requirements.txt --without-hashes -o "$_export_tmp/syncbot-requirements.txt"; then
      if ! cmp -s "$_export_tmp/syncbot-requirements.txt" "$REPO_ROOT/syncbot/requirements.txt"; then
        echo "Warning: committed *requirements.txt files differ from a poetry.lock export." >&2
        echo "Deploy uses the committed pins (same as GitHub Actions). Refresh with pre-commit or poetry export; see docs/DEVELOPMENT.md." >&2
      fi
    else
      echo "Warning: poetry export failed; continuing with committed *requirements.txt." >&2
    fi
    rm -rf "$_export_tmp"
  fi

  echo "=== Run Provider Script ==="
  echo "CLOUD_PROVIDER=$CLOUD_PROVIDER"
  echo "Running: $script_path"
  bash "$script_path"
}

main "$@"
