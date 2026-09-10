#!/usr/bin/env bash
# Print SyncBot GCP Terraform outputs for GitHub variables (WIF, deploy).
# Run from repo root:  infra/gcp/scripts/print-bootstrap-outputs.sh
# Requires: terraform in PATH; run from repo root so infra/gcp is available.
#
# Flow: terraform output (full) -> suggested variable names for CI.
#
# Terraform state is local (infra/gcp/terraform.tfstate) unless you configure
# a remote backend. GitHub Actions never runs terraform apply — do not lose
# this state file. Optional GCS backend is a later improvement.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GCP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ ! -d "$GCP_DIR" ]] || [[ ! -f "$GCP_DIR/main.tf" ]]; then
  echo "Error: infra/gcp not found (expected at $GCP_DIR). Run from repo root." >&2
  exit 1
fi

echo "=== Terraform Outputs (Infra/GCP) ==="
echo ""

cd "$GCP_DIR"
if ! terraform output -json >/dev/null 2>&1; then
  echo "Error: Terraform state not initialized or no outputs. Run 'terraform init' and 'terraform apply' in infra/gcp first." >&2
  exit 1
fi

terraform output

echo ""
echo "=== Suggested GitHub Actions Variables ==="
echo "GCP_PROJECT_ID                    = $(terraform output -raw project_id 2>/dev/null || echo '<set from output project_id>')"
echo "GCP_REGION                        = $(terraform output -raw region 2>/dev/null || echo '<set from output region>')"
echo "GCP_SERVICE_ACCOUNT               = $(terraform output -raw deploy_service_account_email 2>/dev/null || echo '<set from output deploy_service_account_email>')"
WIF="$(terraform output -raw workload_identity_provider 2>/dev/null || true)"
if [[ -n "$WIF" ]]; then
  echo "GCP_WORKLOAD_IDENTITY_PROVIDER    = $WIF"
else
  echo "GCP_WORKLOAD_IDENTITY_PROVIDER    = <re-apply with -var=github_repo=owner/repo>"
fi
echo "Artifact Registry                 = $(terraform output -raw artifact_registry_repository 2>/dev/null || echo '<set from output artifact_registry_repository>')"
echo "Service URL                       = $(terraform output -raw service_url 2>/dev/null || echo '<set from output service_url>')"
echo "Litestream bucket                 = $(terraform output -raw litestream_bucket 2>/dev/null || echo '')"
echo ""
echo "Set GITHUB_DEPLOY_TARGET=gcp on the deploying GitHub repo. Image updates are CI-only"
echo "(gcloud run services update --image); terraform apply ignores the container image."
echo ""
echo "DATA_ENCRYPTION_KEY is provided via .env.deploy file or Terraform variable."
echo "Back it up securely (see docs/DEPLOY.md)."
