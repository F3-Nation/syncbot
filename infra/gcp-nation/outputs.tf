output "service_url" {
  description = "Public base URL of the deployed Cloud Run service"
  value       = google_cloud_run_v2_service.syncbot.uri
}

output "region" {
  value = var.region
}

output "project_id" {
  value = var.project_id
}

output "artifact_registry_repository" {
  description = "Artifact Registry URL for container images"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.syncbot.repository_id}"
}

output "deploy_service_account_email" {
  description = "Service account email for CI deploy"
  value       = google_service_account.deploy.email
}

output "cloud_run_service_name" {
  value = google_cloud_run_v2_service.syncbot.name
}

output "litestream_bucket" {
  description = "GCS bucket used for Litestream SQLite replicas"
  value       = google_storage_bucket.litestream.name
}

output "token_encryption_secret_name" {
  description = "Secret Manager secret name for TOKEN_ENCRYPTION_KEY"
  value       = google_secret_manager_secret.app_secrets[var.secret_token_encryption_key].name
}

output "workload_identity_provider" {
  description = "WIF provider resource name for GitHub Actions (empty if github_repo not set)"
  value       = var.github_repo != "" ? google_iam_workload_identity_pool_provider.github[0].name : ""
}
