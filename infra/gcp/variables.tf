# GCP Terraform variables for SyncBot (see docs/INFRA_CONTRACT.md)
#
# Sections: project / region / stage → database_backend → Cloud Run → keep-warm →
# GitHub WIF → sensitive app secrets → runtime plain env.

variable "project_id" {
  type        = string
  description = "GCP project ID"
}

variable "region" {
  type        = string
  default     = "us-central1"
  description = "Primary region for Cloud Run and optional GCS Litestream replica"
}

variable "stage" {
  type        = string
  default     = "test"
  description = "Stage name (test or prod); used for resource naming"

  validation {
    condition     = contains(["test", "prod"], var.stage)
    error_message = "stage must be test or prod."
  }
}

# ---------------------------------------------------------------------------
# Database: sqlite (default, Litestream + GCS) or mysql / postgresql
# database_mode and existing_db_* are aliases (remove in 2.0.0).
# ---------------------------------------------------------------------------

variable "database_mode" {
  type        = string
  default     = "sqlite"
  description = "Alias for database_backend (sqlite or existing). Prefer database_backend."

  validation {
    condition     = contains(["sqlite", "existing"], var.database_mode)
    error_message = "database_mode must be sqlite or existing."
  }
}

variable "existing_db_host" {
  type        = string
  default     = ""
  description = "Alias for database_host."
}

variable "existing_db_schema" {
  type        = string
  default     = ""
  description = "Alias for database_schema. Empty defaults to syncbot_$${stage} (for example syncbot_test)."
}

variable "existing_db_user" {
  type        = string
  default     = ""
  description = "Alias for database_user."
}

variable "database_host" {
  type        = string
  default     = ""
  description = "DATABASE_HOST. Required when database_backend is mysql or postgresql."
}

variable "database_schema" {
  type        = string
  default     = ""
  description = "DATABASE_SCHEMA. Empty uses existing_db_schema, then syncbot_$${stage}."
}

# ---------------------------------------------------------------------------
# Cloud Run
# ---------------------------------------------------------------------------

variable "cloud_run_image" {
  type        = string
  default     = "gcr.io/cloudrun/hello"
  description = "Container image URL. Bootstrap default is a public hello image; CI updates the live service (Terraform ignores image changes after apply)."
}

variable "cloud_run_cpu" {
  type        = string
  default     = "1"
  description = "CPU allocation for Cloud Run service"
}

variable "cloud_run_memory" {
  type        = string
  default     = "512Mi"
  description = "Memory allocation for Cloud Run service"
}

variable "cloud_run_min_instances" {
  type        = number
  default     = 0
  description = "Minimum instances. 0 = free/best-effort scale-to-zero (default). 1 = paid always-on (Slack 3s guarantee)."

  validation {
    condition     = contains([0, 1], var.cloud_run_min_instances)
    error_message = "cloud_run_min_instances must be 0 or 1."
  }
}

variable "cloud_run_max_instances" {
  type        = number
  default     = 10
  description = "Maximum Cloud Run instances (sqlite always forces 1)."
}

variable "log_level" {
  type        = string
  default     = "INFO"
  description = "Python logging level for the app (LOG_LEVEL). DEBUG, INFO, WARNING, ERROR, or CRITICAL."

  validation {
    condition     = contains(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], var.log_level)
    error_message = "log_level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL."
  }
}

# ---------------------------------------------------------------------------
# Keep-warm (Cloud Scheduler)
# ---------------------------------------------------------------------------

variable "enable_keep_warm" {
  type        = bool
  default     = true
  description = "Create a Cloud Scheduler job that pings GET /health periodically (free-tier friendly)"
}

variable "keep_warm_interval_minutes" {
  type        = number
  default     = 5
  description = "Interval in minutes for keep-warm ping"
}

# ---------------------------------------------------------------------------
# GitHub Actions OIDC (Workload Identity Federation)
# ---------------------------------------------------------------------------

variable "github_repo" {
  type        = string
  default     = ""
  description = "GitHub repo in owner/repo format for WIF (must be the deploying repo, e.g. your fork). Empty skips WIF."

  validation {
    condition     = var.github_repo == "" || can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repo))
    error_message = "github_repo must be empty or 'owner/repo'."
  }
}

# ---------------------------------------------------------------------------
# Sensitive app secrets (passed as Terraform variables; injected as plain env)
# ---------------------------------------------------------------------------

variable "slack_signing_secret" {
  type        = string
  sensitive   = true
  description = "SLACK_SIGNING_SECRET for request verification"
}

variable "slack_client_id" {
  type        = string
  description = "SLACK_CLIENT_ID (OAuth app Client ID)"
}

variable "slack_client_secret" {
  type        = string
  sensitive   = true
  description = "SLACK_CLIENT_SECRET (OAuth client secret)"
}

variable "slack_bot_scopes" {
  type        = string
  default     = "app_mentions:read,channels:history,channels:join,channels:read,channels:manage,chat:write,chat:write.customize,emoji:read,files:read,files:write,groups:history,groups:read,groups:write,im:write,reactions:read,reactions:write,team:read,usergroups:read,users:read,users:read.email"
  description = "Comma-separated Slack OAuth bot scopes (SLACK_BOT_SCOPES)"
}

variable "slack_user_scopes" {
  type        = string
  default     = "chat:write,channels:history,channels:read,files:read,files:write,groups:history,groups:read,groups:write,reactions:read,reactions:write,team:read,users:read,users:read.email"
  description = "Comma-separated user OAuth scopes for Cloud Run (SLACK_USER_SCOPES). Must match slack-manifest.json oauth_config.scopes.user."
}

variable "data_encryption_key" {
  type        = string
  sensitive   = true
  description = "DATA_ENCRYPTION_KEY for Fernet data-at-rest encryption. Generate with: python3 -c \"import secrets; print(secrets.token_urlsafe(36))\""
}

variable "database_password" {
  type        = string
  sensitive   = true
  default     = ""
  description = "DATABASE_PASSWORD for the app DB user. Required when database_backend is mysql or postgresql; unused for sqlite."
}

variable "database_user" {
  type        = string
  default     = ""
  description = "DATABASE_USER (full username, including any TiDB cluster prefix). Required when database_backend is mysql or postgresql if existing_db_user is empty."
}

# ---------------------------------------------------------------------------
# Runtime plain env (Cloud Run) — parity with infra/aws/template.yaml
# ---------------------------------------------------------------------------

variable "database_backend" {
  type        = string
  default     = ""
  description = "DATABASE_BACKEND: mysql, postgresql, or sqlite. Empty falls through to database_mode (default sqlite)."

  validation {
    condition     = contains(["", "mysql", "postgresql", "sqlite"], var.database_backend)
    error_message = "database_backend must be empty, mysql, postgresql, or sqlite."
  }
}

variable "database_port" {
  type        = string
  default     = ""
  description = "DATABASE_PORT. Empty uses the engine default (3306 MySQL, 5432 PostgreSQL). Set for a non-standard port (e.g. TiDB Cloud 4000). Unused for sqlite."
}

variable "primary_workspace" {
  type        = string
  default     = ""
  description = "PRIMARY_WORKSPACE Slack Team ID; required for backup/restore to appear. Empty omits the env var and hides backup/restore."
}

variable "enable_db_reset" {
  type        = string
  default     = ""
  description = "ENABLE_DB_RESET: set to \"true\" for Reset Database when PRIMARY_WORKSPACE matches; empty omits."
}

variable "database_tls_enabled" {
  type        = string
  default     = ""
  description = "DATABASE_TLS_ENABLED; empty = app default (TLS on outside local dev)."

  validation {
    condition     = contains(["", "true", "false"], var.database_tls_enabled)
    error_message = "database_tls_enabled must be empty, true, or false."
  }
}

variable "database_ssl_ca_path" {
  type        = string
  default     = ""
  description = "DATABASE_SSL_CA_PATH when TLS is on; empty omits (app default CA path)."
}
