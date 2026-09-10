# Outputs aligned with docs/INFRA_CONTRACT.md (bootstrap output contract)

output "service_url" {
  description = "Public base URL of the deployed app (for Slack app configuration)"
  value       = google_cloud_run_v2_service.syncbot.uri
}

output "region" {
  description = "Primary region for the deployment"
  value       = var.region
}

output "project_id" {
  description = "GCP project ID"
  value       = var.project_id
}

output "artifact_registry_repository" {
  description = "Artifact Registry repository for container images (CI pushes here)"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.syncbot.repository_id}"
}

output "deploy_service_account_email" {
  description = "Service account email for CI/deploy (use with Workload Identity Federation)"
  value       = google_service_account.deploy.email
}

output "cloud_run_service_name" {
  description = "Cloud Run service name (for deploy targeting)"
  value       = google_cloud_run_v2_service.syncbot.name
}

output "cloud_run_service_location" {
  description = "Cloud Run service location (region)"
  value       = google_cloud_run_v2_service.syncbot.location
}

output "litestream_bucket" {
  description = "GCS bucket for Litestream replicas (empty when database_backend is mysql or postgresql)"
  value       = local.is_sqlite ? google_storage_bucket.litestream[0].name : ""
}

output "workload_identity_provider" {
  description = "WIF provider resource name for GitHub Actions (empty if github_repo unset)"
  value       = var.github_repo != "" ? google_iam_workload_identity_pool_provider.github[0].name : ""
}

output "database_mode" {
  description = "sqlite or existing (alias of database_backend; remove in 2.0.0)"
  value       = local.is_sqlite ? "sqlite" : "existing"
}

output "database_backend" {
  description = "mysql, postgresql, or sqlite"
  value       = local.resolved_database_backend
}
