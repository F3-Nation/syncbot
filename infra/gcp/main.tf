# SyncBot on GCP — Cloud Run + SQLite/Litestream (default) or mysql/postgresql.
# No Cloud SQL. Secrets are Terraform variables injected as Cloud Run env, or
# optional Secret Manager when use_secret_manager is true.

terraform {
  required_version = ">= 1.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_project" "current" {
  project_id = var.project_id
}

locals {
  name_prefix = "syncbot-${var.stage}"
  resolved_database_backend = (
    trimspace(var.database_backend) != "" ? trimspace(var.database_backend) : (
      var.database_mode == "sqlite" ? "sqlite" : "mysql"
    )
  )
  is_sqlite             = local.resolved_database_backend == "sqlite"
  use_existing_database = !local.is_sqlite
  resolved_database_host = (
    trimspace(var.database_host) != "" ? trimspace(var.database_host) : trimspace(var.existing_db_host)
  )
  db_user = local.use_existing_database ? (
    trimspace(var.database_user) != "" ? trimspace(var.database_user) : trimspace(var.existing_db_user)
  ) : ""
  db_schema = local.use_existing_database ? (
    trimspace(var.database_schema) != "" ? trimspace(var.database_schema) : (
      trimspace(var.existing_db_schema) != "" ? trimspace(var.existing_db_schema) : "syncbot_${var.stage}"
    )
  ) : ""
  db_backend = local.resolved_database_backend

  sqlite_plain_env = merge(
    {
      DATABASE_BACKEND      = "sqlite"
      DATABASE_URL          = "sqlite:////data/syncbot.db"
      LITESTREAM_GCS_BUCKET = try(google_storage_bucket.litestream[0].name, "")
      SLACK_USER_SCOPES     = var.slack_user_scopes
      SLACK_BOT_SCOPES      = var.slack_bot_scopes
      LOG_LEVEL             = var.log_level
      SLACK_BOT_TOKEN       = "123"
    },
    trimspace(var.primary_workspace) != "" ? { PRIMARY_WORKSPACE = var.primary_workspace } : {},
    trimspace(var.enable_db_reset) != "" ? { ENABLE_DB_RESET = var.enable_db_reset } : {},
  )

  existing_plain_env = merge(
    {
      DATABASE_HOST     = local.resolved_database_host
      DATABASE_USER     = local.db_user
      DATABASE_SCHEMA   = local.db_schema
      DATABASE_BACKEND  = local.db_backend
      SLACK_USER_SCOPES = var.slack_user_scopes
      SLACK_BOT_SCOPES  = var.slack_bot_scopes
      LOG_LEVEL         = var.log_level
      SLACK_BOT_TOKEN   = "123"
    },
    trimspace(var.primary_workspace) != "" ? { PRIMARY_WORKSPACE = var.primary_workspace } : {},
    trimspace(var.enable_db_reset) != "" ? { ENABLE_DB_RESET = var.enable_db_reset } : {},
    var.database_tls_enabled != "" ? { DATABASE_TLS_ENABLED = var.database_tls_enabled } : {},
    trimspace(var.database_ssl_ca_path) != "" ? { DATABASE_SSL_CA_PATH = var.database_ssl_ca_path } : {},
    trimspace(var.database_port) != "" ? { DATABASE_PORT = trimspace(var.database_port) } : {},
  )

  runtime_plain_env = merge(
    local.is_sqlite ? local.sqlite_plain_env : local.existing_plain_env,
    {
      # Cloud Run HTTP/1 max is 32 MiB per request. 30 is the same 2 MiB headroom as SAM's 4 under 6 MB.
      FEDERATION_HTTP_MAX_MB = "30"
    },
  )

  runtime_secret_env = merge(
    {
      SLACK_SIGNING_SECRET = var.slack_signing_secret
      SLACK_CLIENT_ID      = var.slack_client_id
      SLACK_CLIENT_SECRET  = var.slack_client_secret
      DATA_ENCRYPTION_KEY  = var.data_encryption_key
    },
    local.use_existing_database ? { DATABASE_PASSWORD = var.database_password } : {},
  )

  sm_secret_ids = var.use_secret_manager ? toset(compact([
    "slack-signing-secret",
    "slack-client-id",
    "slack-client-secret",
    "data-encryption-key",
    local.use_existing_database ? "database-password" : "",
  ])) : toset([])

  sm_env_to_secret = var.use_secret_manager ? merge(
    {
      SLACK_SIGNING_SECRET = "slack-signing-secret"
      SLACK_CLIENT_ID      = "slack-client-id"
      SLACK_CLIENT_SECRET  = "slack-client-secret"
      DATA_ENCRYPTION_KEY  = "data-encryption-key"
    },
    local.use_existing_database ? { DATABASE_PASSWORD = "database-password" } : {},
  ) : {}

  sm_rotatable_data = var.use_secret_manager ? merge(
    {
      slack-signing-secret = var.slack_signing_secret
      slack-client-id      = var.slack_client_id
      slack-client-secret  = var.slack_client_secret
    },
    local.use_existing_database ? { database-password = var.database_password } : {},
  ) : {}
}

# ---------------------------------------------------------------------------
# APIs
# ---------------------------------------------------------------------------

resource "google_project_service" "run" {
  project            = var.project_id
  service            = "run.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "artifact_registry" {
  project            = var.project_id
  service            = "artifactregistry.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "storage" {
  count              = local.is_sqlite ? 1 : 0
  project            = var.project_id
  service            = "storage.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "iam" {
  project            = var.project_id
  service            = "iam.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "iamcredentials" {
  project            = var.project_id
  service            = "iamcredentials.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "sts" {
  project            = var.project_id
  service            = "sts.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "scheduler" {
  count              = var.enable_keep_warm ? 1 : 0
  project            = var.project_id
  service            = "cloudscheduler.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "secretmanager" {
  count              = var.use_secret_manager ? 1 : 0
  project            = var.project_id
  service            = "secretmanager.googleapis.com"
  disable_on_destroy = false
}

# ---------------------------------------------------------------------------
# Artifact Registry
# ---------------------------------------------------------------------------

resource "google_artifact_registry_repository" "syncbot" {
  location      = var.region
  repository_id = "${local.name_prefix}-images"
  description   = "SyncBot container images"
  format        = "DOCKER"

  depends_on = [google_project_service.artifact_registry]
}

# ---------------------------------------------------------------------------
# Secret Manager (optional; GCP_USE_SECRET_MANAGER)
# ---------------------------------------------------------------------------

resource "google_secret_manager_secret" "app" {
  for_each  = local.sm_secret_ids
  project   = var.project_id
  secret_id = "${local.name_prefix}-${each.key}"

  replication {
    auto {}
  }

  depends_on = [google_project_service.secretmanager]
}

resource "google_secret_manager_secret_version" "rotatable" {
  for_each    = local.sm_rotatable_data
  secret      = google_secret_manager_secret.app[each.key].id
  secret_data = each.value
}

resource "google_secret_manager_secret_version" "data_encryption_key" {
  count       = var.use_secret_manager ? 1 : 0
  secret      = google_secret_manager_secret.app["data-encryption-key"].id
  secret_data = var.data_encryption_key

  lifecycle {
    ignore_changes = [secret_data]
  }
}

resource "google_secret_manager_secret_iam_member" "cloud_run_accessor" {
  for_each  = google_secret_manager_secret.app
  project   = var.project_id
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.cloud_run.email}"
}

# ---------------------------------------------------------------------------
# GCS bucket for Litestream (sqlite mode only)
# ---------------------------------------------------------------------------

resource "google_storage_bucket" "litestream" {
  count                       = local.is_sqlite ? 1 : 0
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

  depends_on = [google_project_service.storage]
}

# ---------------------------------------------------------------------------
# Service account for Cloud Run (runtime) — keep existing account_id scheme
# ---------------------------------------------------------------------------

resource "google_service_account" "cloud_run" {
  project      = var.project_id
  account_id   = "${replace(local.name_prefix, "-", "")}-run"
  display_name = "SyncBot Cloud Run runtime (${var.stage})"

  depends_on = [google_project_service.iam]
}

resource "google_storage_bucket_iam_member" "cloud_run_litestream" {
  count  = local.is_sqlite ? 1 : 0
  bucket = google_storage_bucket.litestream[0].name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.cloud_run.email}"
}

resource "google_artifact_registry_repository_iam_member" "cloud_run_reader" {
  project    = var.project_id
  location   = google_artifact_registry_repository.syncbot.location
  repository = google_artifact_registry_repository.syncbot.repository_id
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.cloud_run.email}"
}

# ---------------------------------------------------------------------------
# Deploy service account (CI / Workload Identity Federation)
# ---------------------------------------------------------------------------

resource "google_service_account" "deploy" {
  project      = var.project_id
  account_id   = "${replace(local.name_prefix, "-", "")}-deploy"
  display_name = "SyncBot deploy (CI) (${var.stage})"

  depends_on = [google_project_service.iam]
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
# Cloud Run
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "syncbot" {
  project  = var.project_id
  name     = local.name_prefix
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  labels = {
    syncbot_database_backend = local.resolved_database_backend
    syncbot_database_mode    = local.is_sqlite ? "sqlite" : "existing"
  }

  template {
    service_account = google_service_account.cloud_run.email
    timeout         = "300s"

    max_instance_request_concurrency = local.is_sqlite ? 1 : 80

    scaling {
      min_instance_count = var.cloud_run_min_instances
      max_instance_count = local.is_sqlite ? 1 : var.cloud_run_max_instances
    }

    containers {
      image = var.cloud_run_image

      resources {
        cpu_idle          = true
        startup_cpu_boost = true
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
        for_each = var.use_secret_manager ? {} : local.runtime_secret_env
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.sm_env_to_secret
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.app[env.value].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      client,
      client_version,
      template[0].containers[0].image,
    ]
    precondition {
      condition     = local.is_sqlite || (local.resolved_database_host != "" && trimspace(var.database_password) != "" && local.db_user != "")
      error_message = "database_host, database_password, and database_user are required when database_backend is mysql or postgresql."
    }
    precondition {
      condition     = trimspace(var.data_encryption_key) != ""
      error_message = "data_encryption_key is required. deploy.sh generates one if it is empty, or reuses the Secret Manager value when GCP_USE_SECRET_MANAGER is true."
    }
  }

  depends_on = [
    google_project_service.run,
    google_storage_bucket_iam_member.cloud_run_litestream,
    google_secret_manager_secret_version.rotatable,
    google_secret_manager_secret_version.data_encryption_key,
    google_secret_manager_secret_iam_member.cloud_run_accessor,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "public" {
  project  = google_cloud_run_v2_service.syncbot.project
  location = google_cloud_run_v2_service.syncbot.location
  name     = google_cloud_run_v2_service.syncbot.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_service_iam_member" "keep_warm" {
  count    = var.enable_keep_warm ? 1 : 0
  project  = google_cloud_run_v2_service.syncbot.project
  location = google_cloud_run_v2_service.syncbot.location
  name     = google_cloud_run_v2_service.syncbot.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.cloud_run.email}"
}

resource "google_service_account_iam_member" "scheduler_act_as_cloud_run" {
  count              = var.enable_keep_warm ? 1 : 0
  service_account_id = google_service_account.cloud_run.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-cloudscheduler.iam.gserviceaccount.com"

  depends_on = [google_project_service.scheduler]
}

# ---------------------------------------------------------------------------
# Cloud Scheduler (keep-warm)
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
      audience              = google_cloud_run_v2_service.syncbot.uri
    }
  }

  depends_on = [
    google_project_service.scheduler,
    google_cloud_run_v2_service.syncbot,
    google_cloud_run_v2_service_iam_member.keep_warm,
    google_service_account_iam_member.scheduler_act_as_cloud_run,
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

  depends_on = [
    google_project_service.iam,
    google_project_service.iamcredentials,
    google_project_service.sts,
  ]
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
