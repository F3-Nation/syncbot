# SyncBot on GCP — F3 Nation variant (SQLite + Litestream on Cloud Run)
# No Cloud SQL. The SQLite DB lives inside the container and is continuously
# replicated to a GCS bucket via Litestream.

terraform {
  required_version = ">= 1.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  name_prefix = "syncbot-${var.stage}"
  secret_ids = [
    var.secret_slack_signing_secret,
    var.secret_slack_client_id,
    var.secret_slack_client_secret,
    var.secret_slack_bot_scopes,
    var.secret_token_encryption_key,
  ]
  env_to_secret_key = {
    "SLACK_SIGNING_SECRET" = var.secret_slack_signing_secret
    "SLACK_CLIENT_ID"      = var.secret_slack_client_id
    "SLACK_CLIENT_SECRET"  = var.secret_slack_client_secret
    "SLACK_BOT_SCOPES"     = var.secret_slack_bot_scopes
    "TOKEN_ENCRYPTION_KEY" = var.secret_token_encryption_key
  }
  syncbot_public_url_effective = trimspace(var.syncbot_public_url_override) != "" ? trimspace(var.syncbot_public_url_override) : ""
  runtime_plain_env = merge(
    {
      DATABASE_BACKEND             = "sqlite"
      DATABASE_URL                 = "sqlite:////data/syncbot.db"
      SLACK_USER_SCOPES            = var.slack_user_scopes
      LOG_LEVEL                    = var.log_level
      REQUIRE_ADMIN                = var.require_admin
      SLACK_BOT_TOKEN              = "123"
      SOFT_DELETE_RETENTION_DAYS   = tostring(var.soft_delete_retention_days)
      SYNCBOT_FEDERATION_ENABLED   = var.syncbot_federation_enabled ? "true" : "false"
      LITESTREAM_GCS_BUCKET        = google_storage_bucket.litestream.name
    },
    var.syncbot_instance_id != "" ? { SYNCBOT_INSTANCE_ID = var.syncbot_instance_id } : {},
    local.syncbot_public_url_effective != "" ? { SYNCBOT_PUBLIC_URL = trimsuffix(local.syncbot_public_url_effective, "/") } : {},
    trimspace(var.primary_workspace) != "" ? { PRIMARY_WORKSPACE = var.primary_workspace } : {},
    trimspace(var.enable_db_reset) != "" ? { ENABLE_DB_RESET = var.enable_db_reset } : {},
  )
}

# ---------------------------------------------------------------------------
# APIs
# ---------------------------------------------------------------------------

resource "google_project_service" "run" {
  project            = var.project_id
  service            = "run.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "secretmanager" {
  project            = var.project_id
  service            = "secretmanager.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "scheduler" {
  count              = var.enable_keep_warm ? 1 : 0
  project            = var.project_id
  service            = "cloudscheduler.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "artifact_registry" {
  project            = var.project_id
  service            = "artifactregistry.googleapis.com"
  disable_on_destroy = false
}

# ---------------------------------------------------------------------------
# Secret Manager
# ---------------------------------------------------------------------------

resource "google_secret_manager_secret" "app_secrets" {
  for_each  = toset(local.secret_ids)
  project   = var.project_id
  secret_id = "${local.name_prefix}-${each.key}"

  replication {
    auto {}
  }

  depends_on = [google_project_service.secretmanager]
}

# ---------------------------------------------------------------------------
# Artifact Registry (container images)
# ---------------------------------------------------------------------------

resource "google_artifact_registry_repository" "syncbot" {
  location      = var.region
  repository_id = "${local.name_prefix}-images"
  description   = "SyncBot container images"
  format        = "DOCKER"

  depends_on = [google_project_service.artifact_registry]
}

# ---------------------------------------------------------------------------
# GCS bucket for Litestream SQLite replicas
# ---------------------------------------------------------------------------

resource "google_storage_bucket" "litestream" {
  project                     = var.project_id
  name                        = "${local.name_prefix}-litestream-${var.project_id}"
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  force_destroy               = false

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      num_newer_versions = 3
    }
    action {
      type = "Delete"
    }
  }
}

# ---------------------------------------------------------------------------
# Service account for Cloud Run (runtime)
# ---------------------------------------------------------------------------

resource "google_service_account" "cloud_run" {
  project      = var.project_id
  account_id   = "${replace(local.name_prefix, "-", "")}run"
  display_name = "SyncBot Cloud Run runtime (${var.stage})"
}

# Grant Cloud Run SA access to read the app secrets
resource "google_project_iam_member" "cloud_run_secret_access" {
  for_each = toset(local.secret_ids)
  project  = var.project_id
  role     = "roles/secretmanager.secretAccessor"
  member   = "serviceAccount:${google_service_account.cloud_run.email}"
}

# Grant Cloud Run SA read/write to the Litestream GCS bucket
resource "google_storage_bucket_iam_member" "cloud_run_litestream" {
  bucket = google_storage_bucket.litestream.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.cloud_run.email}"
}

# ---------------------------------------------------------------------------
# Deploy service account (CI / Workload Identity Federation)
# ---------------------------------------------------------------------------

resource "google_service_account" "deploy" {
  project      = var.project_id
  account_id   = "${replace(local.name_prefix, "-", "")}deploy"
  display_name = "SyncBot deploy (CI) (${var.stage})"
}

resource "google_project_iam_member" "deploy_run_admin" {
  project = var.project_id
  role    = "roles/run.admin"
  member  = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_project_iam_member" "deploy_sa_user" {
  project = var.project_id
  role    = "roles/iam.serviceAccountUser"
  member  = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_project_iam_member" "deploy_artifact_writer" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${google_service_account.deploy.email}"
}

# ---------------------------------------------------------------------------
# Token encryption key (auto-generated, stored in Secret Manager)
# ---------------------------------------------------------------------------

resource "random_password" "token_encryption_key" {
  length  = 48
  special = false
}

resource "google_secret_manager_secret_version" "token_encryption_key" {
  secret      = google_secret_manager_secret.app_secrets[var.secret_token_encryption_key].id
  secret_data = var.token_encryption_key_override != "" ? var.token_encryption_key_override : random_password.token_encryption_key.result
}

# ---------------------------------------------------------------------------
# Cloud Run service
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "syncbot" {
  project  = var.project_id
  name     = local.name_prefix
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.cloud_run.email

    # SQLite is single-writer: exactly 1 instance, 1 request at a time.
    max_instance_request_concurrency = 1

    scaling {
      min_instance_count = var.cloud_run_min_instances
      max_instance_count = 1
    }

    containers {
      image = var.cloud_run_image

      resources {
        limits = {
          cpu    = var.cloud_run_cpu
          memory = var.cloud_run_memory
        }
      }

      dynamic "env" {
        for_each = local.runtime_plain_env
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.env_to_secret_key
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.app_secrets[env.value].name
              version = "latest"
            }
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.run,
    google_secret_manager_secret.app_secrets,
  ]
}

# Allow unauthenticated invocations (Slack hits the public URL)
resource "google_cloud_run_v2_service_iam_member" "public" {
  project  = google_cloud_run_v2_service.syncbot.project
  location = google_cloud_run_v2_service.syncbot.location
  name     = google_cloud_run_v2_service.syncbot.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ---------------------------------------------------------------------------
# Cloud Scheduler (keep-warm) — important for SQLite: keeps the single
# instance alive so Litestream doesn't need a cold-start restore on every request.
# ---------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "keep_warm" {
  count            = var.enable_keep_warm ? 1 : 0
  project          = var.project_id
  name             = "${local.name_prefix}-keep-warm"
  region           = var.region
  schedule         = "*/${var.keep_warm_interval_minutes} * * * *"
  time_zone        = "UTC"
  attempt_deadline = "60s"

  http_target {
    uri         = "${google_cloud_run_v2_service.syncbot.uri}/health"
    http_method = "GET"
    oidc_token {
      service_account_email = google_service_account.cloud_run.email
    }
  }

  depends_on = [
    google_project_service.scheduler,
    google_cloud_run_v2_service.syncbot,
  ]
}

# ---------------------------------------------------------------------------
# Workload Identity Federation (GitHub Actions OIDC → deploy SA)
# ---------------------------------------------------------------------------

resource "google_iam_workload_identity_pool" "github" {
  count                     = var.github_repo != "" ? 1 : 0
  project                   = var.project_id
  workload_identity_pool_id = "${local.name_prefix}-gh-pool"
  display_name              = "GitHub Actions (${var.stage})"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  count                              = var.github_repo != "" ? 1 : 0
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "${local.name_prefix}-gh"
  display_name                       = "GitHub (${var.stage})"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }

  attribute_condition = "assertion.repository == '${var.github_repo}'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "wif_deploy" {
  count              = var.github_repo != "" ? 1 : 0
  service_account_id = google_service_account.deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github[0].name}/attribute.repository/${var.github_repo}"
}
