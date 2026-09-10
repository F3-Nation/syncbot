#!/usr/bin/env bash
# Print SyncBot AWS bootstrap stack outputs for GitHub variables or local config.
# Run from repo root:  infra/aws/scripts/print-bootstrap-outputs.sh
# Optional env: AWS_BOOTSTRAP_STACK_NAME (default syncbot-bootstrap), AWS_REGION (default us-east-1).
#
# Flow: describe-stack (key/value) -> raw lines -> suggested GitHub variable names.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/resolve_database_backend.sh"
apply_aws_provider_env_aliases

STACK_NAME="${AWS_BOOTSTRAP_STACK_NAME:-syncbot-bootstrap}"
REGION="${AWS_REGION:-us-east-1}"

echo "=== Bootstrap Stack Outputs ==="
echo "Bootstrap stack: $STACK_NAME (region: $REGION)"
echo ""

outputs=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
  --output text \
  --region "$REGION" 2>/dev/null) || {
  echo "Error: Could not describe stack '$STACK_NAME' in $REGION. Is the bootstrap stack deployed?" >&2
  exit 1
}

while read -r key value; do
  echo "$key = $value"
done <<< "$outputs"

echo ""
echo "=== Suggested GitHub Actions Variables ==="
echo "AWS_ROLE_TO_ASSUME  = $(echo "$outputs" | awk -F'\t' '$1=="GitHubDeployRoleArn"{print $2}')"
echo "AWS_S3_BUCKET       = $(echo "$outputs" | awk -F'\t' '$1=="DeploymentBucketName"{print $2}')  (SAM/CI packaging for sam deploy — not Slack or app media)"
echo "AWS_REGION          = $(echo "$outputs" | awk -F'\t' '$1=="BootstrapRegion"{print $2}')"
echo ""
echo "Next: deploy the app stack (sam deploy) and set the remaining GitHub vars/secrets."
echo "DATA_ENCRYPTION_KEY is created by the deploy script on first deploy — back it up then (see docs/DEPLOY.md)."
