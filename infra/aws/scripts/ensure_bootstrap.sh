#!/usr/bin/env bash
# Create or sync the AWS bootstrap CloudFormation stack (OIDC role + SAM bucket).
#
# Used by infra/aws/scripts/deploy.sh and .github/workflows/deploy-aws.yml.
#
# Detection: stack parameter TemplateContentSha256 vs sha256 of template.bootstrap.yaml.
# Missing stacks are created with --create. Unchanged hashes skip CloudFormation.
#
# Usage:
#   ensure_bootstrap.sh [--force] [--create] [--skip-sync]
#
# Env:
#   AWS_BOOTSTRAP_STACK_NAME   default syncbot-bootstrap (alias: BOOTSTRAP_STACK_NAME)
#   AWS_REGION                 default us-east-1
#   GITHUB_REPO                owner/repo; required to create a missing stack
#   AWS_CREATE_OIDC_PROVIDER   create only (default true; alias: CREATE_OIDC_PROVIDER)
#   AWS_DEPLOY_BUCKET_PREFIX   create only (default syncbot-deploy; alias: DEPLOY_BUCKET_PREFIX)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_AWS="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/resolve_database_backend.sh"
BOOTSTRAP_TEMPLATE="$INFRA_AWS/template.bootstrap.yaml"
SHA_PARAM_KEY="TemplateContentSha256"

usage() {
  cat <<EOF
Usage: ensure_bootstrap.sh [--force] [--create] [--skip-sync]

  --create     Create the stack if it does not exist (needs GITHUB_REPO).
  --force      Deploy even when the template hash already matches.
  --skip-sync  Do not update an existing stack (create-if-missing still runs
               with --create).
EOF
}

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

resolve_github_repo_for_create() {
  if [[ -n "${GITHUB_REPO:-}" ]]; then
    echo "$GITHUB_REPO"
    return 0
  fi
  if [[ -n "${GITHUB_REPOSITORY:-}" ]]; then
    echo "$GITHUB_REPOSITORY"
    return 0
  fi
  local repo_root url parsed
  repo_root="$(cd "$INFRA_AWS/../.." && pwd)"
  url="$(git -C "$repo_root" remote get-url origin 2>/dev/null || true)"
  if parsed="$(github_owner_repo_from_url "$url")"; then
    echo "$parsed"
    return 0
  fi
  echo "Error: GITHUB_REPO is unset and origin is not a github.com remote." >&2
  echo "Set GITHUB_REPO=owner/repo in .env.deploy.<stage> to create the bootstrap stack." >&2
  return 1
}

# GitHub issues an immutable OIDC subject claim, repo:owner@owner-id/repo@repo-id,
# for repositories created or transferred after 2026-07-15. A trust policy that
# only matches repo:owner/repo rejects those tokens with a bare AccessDenied, so
# the role has to trust both forms. Empty output means the classic form is enough.
resolve_immutable_repository() {
  local repo="$1" prefix=""
  if [[ -n "${GITHUB_REPOSITORY:-}" && -n "${GITHUB_REPOSITORY_OWNER_ID:-}" && -n "${GITHUB_REPOSITORY_ID:-}" ]]; then
    echo "${GITHUB_REPOSITORY%%/*}@${GITHUB_REPOSITORY_OWNER_ID}/${GITHUB_REPOSITORY#*/}@${GITHUB_REPOSITORY_ID}"
    return 0
  fi
  if [[ -n "$repo" ]] && command -v gh >/dev/null 2>&1; then
    prefix="$(gh api "repos/${repo}/actions/oidc/customization/sub" --jq '.sub_claim_prefix // ""' 2>/dev/null || true)"
    prefix="${prefix//$'\r'/}"
    prefix="${prefix#repo:}"
    if [[ "$prefix" == *"@"* ]]; then
      echo "$prefix"
      return 0
    fi
  fi
  echo ""
}

stack_exists() {
  aws cloudformation describe-stacks \
    --stack-name "$STACK" \
    --region "$REGION" \
    >/dev/null 2>&1
}

template_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$BOOTSTRAP_TEMPLATE" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$BOOTSTRAP_TEMPLATE" | awk '{print $1}'
  else
    echo "Error: need sha256sum or shasum to hash the bootstrap template." >&2
    exit 1
  fi
}

stack_param() {
  local key="$1"
  aws cloudformation describe-stacks \
    --stack-name "$STACK" \
    --region "$REGION" \
    --query "Stacks[0].Parameters[?ParameterKey=='${key}'].ParameterValue | [0]" \
    --output text 2>/dev/null || true
}

deploy_bootstrap() {
  local github_repo="$1"
  local create_oidc="$2"
  local bucket_prefix="$3"
  local file_sha="$4"
  local immutable_repo="$5"

  if [[ -n "$immutable_repo" ]]; then
    echo "OIDC trust also allows immutable subject 'repo:${immutable_repo}:*'."
  fi
  echo "Deploying bootstrap stack '$STACK' in $REGION ..."
  aws cloudformation deploy \
    --template-file "$BOOTSTRAP_TEMPLATE" \
    --stack-name "$STACK" \
    --parameter-overrides \
      "GitHubRepository=$github_repo" \
      "GitHubImmutableRepository=$immutable_repo" \
      "CreateOIDCProvider=$create_oidc" \
      "DeploymentBucketPrefix=$bucket_prefix" \
      "${SHA_PARAM_KEY}=${file_sha}" \
    --capabilities CAPABILITY_NAMED_IAM \
    --no-fail-on-empty-changeset \
    --region "$REGION"
}

FORCE="false"
CREATE="false"
SKIP_SYNC="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h | --help | help)
      usage
      exit 0
      ;;
    --force)
      FORCE="true"
      shift
      ;;
    --create)
      CREATE="true"
      shift
      ;;
    --skip-sync)
      SKIP_SYNC="true"
      shift
      ;;
    *)
      echo "Error: unexpected argument '$1'." >&2
      usage
      exit 1
      ;;
  esac
done

if [[ ! -f "$BOOTSTRAP_TEMPLATE" ]]; then
  echo "Error: bootstrap template not found: $BOOTSTRAP_TEMPLATE" >&2
  exit 1
fi

apply_aws_provider_env_aliases
STACK="${AWS_BOOTSTRAP_STACK_NAME:-syncbot-bootstrap}"
REGION="${AWS_REGION:-us-east-1}"
FILE_SHA="$(template_sha256)"

if ! stack_exists; then
  if [[ "$CREATE" != "true" ]]; then
    echo "Error: bootstrap stack '$STACK' does not exist in $REGION." >&2
    echo "Run a local deploy once (./deploy.sh --env test) or pass --create." >&2
    exit 1
  fi
  GH_REPO="$(resolve_github_repo_for_create)"
  CREATE_OIDC="${AWS_CREATE_OIDC_PROVIDER:-true}"
  BUCKET_PREFIX="${AWS_DEPLOY_BUCKET_PREFIX:-syncbot-deploy}"
  IMMUTABLE_REPO="$(resolve_immutable_repository "$GH_REPO")"
  echo "Bootstrap stack '$STACK' not found; creating (GitHubRepository=$GH_REPO)."
  deploy_bootstrap "$GH_REPO" "$CREATE_OIDC" "$BUCKET_PREFIX" "$FILE_SHA" "$IMMUTABLE_REPO"
  exit 0
fi

if [[ "$SKIP_SYNC" == "true" ]]; then
  echo "Skipping bootstrap template sync (--skip-sync / SYNCBOT_SKIP_BOOTSTRAP_SYNC)."
  exit 0
fi

STACK_SHA="$(stack_param "$SHA_PARAM_KEY")"
STACK_SHA="${STACK_SHA//$'\r'/}"
if [[ "$STACK_SHA" == "None" ]]; then
  STACK_SHA=""
fi

GH_REPO="$(stack_param GitHubRepository)"
CREATE_OIDC="$(stack_param CreateOIDCProvider)"
BUCKET_PREFIX="$(stack_param DeploymentBucketPrefix)"
STACK_IMMUTABLE_REPO="$(stack_param GitHubImmutableRepository)"
GH_REPO="${GH_REPO//$'\r'/}"
CREATE_OIDC="${CREATE_OIDC//$'\r'/}"
BUCKET_PREFIX="${BUCKET_PREFIX//$'\r'/}"
STACK_IMMUTABLE_REPO="${STACK_IMMUTABLE_REPO//$'\r'/}"
[[ "$GH_REPO" == "None" ]] && GH_REPO=""
[[ "$CREATE_OIDC" == "None" || -z "$CREATE_OIDC" ]] && CREATE_OIDC="true"
[[ "$BUCKET_PREFIX" == "None" || -z "$BUCKET_PREFIX" ]] && BUCKET_PREFIX="syncbot-deploy"
[[ "$STACK_IMMUTABLE_REPO" == "None" ]] && STACK_IMMUTABLE_REPO=""

if [[ -z "$GH_REPO" ]]; then
  echo "Error: bootstrap stack has no GitHubRepository parameter; cannot sync." >&2
  exit 1
fi

# GitHub can switch a repository to immutable subject claims without the template
# changing, which would strand the stack on a trust policy that rejects every token.
IMMUTABLE_REPO="$(resolve_immutable_repository "$GH_REPO")"
[[ -z "$IMMUTABLE_REPO" ]] && IMMUTABLE_REPO="$STACK_IMMUTABLE_REPO"

if [[ "$FORCE" != "true" && -n "$STACK_SHA" && "$STACK_SHA" == "$FILE_SHA" && "$IMMUTABLE_REPO" == "$STACK_IMMUTABLE_REPO" ]]; then
  echo "Bootstrap template unchanged (sha256 $FILE_SHA); skipping sync."
  exit 0
fi

if [[ "$FORCE" == "true" ]]; then
  echo "Forcing bootstrap sync (--bootstrap / --force)."
elif [[ -z "$STACK_SHA" ]]; then
  echo "Bootstrap stack has no ${SHA_PARAM_KEY} parameter; syncing once to record the template hash."
elif [[ "$IMMUTABLE_REPO" != "$STACK_IMMUTABLE_REPO" ]]; then
  echo "OIDC subject claim changed (stack '${STACK_IMMUTABLE_REPO:-none}' → '${IMMUTABLE_REPO:-none}'); syncing."
else
  echo "Bootstrap template changed (stack $STACK_SHA → file $FILE_SHA); syncing."
fi

deploy_bootstrap "$GH_REPO" "$CREATE_OIDC" "$BUCKET_PREFIX" "$FILE_SHA" "$IMMUTABLE_REPO"
