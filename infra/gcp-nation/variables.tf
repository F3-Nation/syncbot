# Variables for SyncBot GCP Nation deployment (SQLite + Litestream)

variable "project_id" {
  type        = string
  description = "GCP project ID"
}

variable "region" {
  type        = string
  default     = "us-central1"
  description = "Primary region for Cloud Run and GCS"
}

variable "stage" {
  type        = string
  default     = "staging"
  description = "Stage name (staging or prod); used for resource naming"

  validation {
    condition     = contains(["staging", "prod"], var.stage)
    error_message = "stage must be 'staging' or 'prod'."
  }
}

# ---------------------------------------------------------------------------
# Cloud Run
# ---------------------------------------------------------------------------

variable "cloud_run_image" {
  type        = string
  description = "Container image URL for Cloud Run (required). Set after first build."

  validation {
    condition     = trimspace(var.cloud_run_image) != ""
    error_message = "cloud_run_image is required."
  }
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
  description = "Minimum number of instances (0 = scale-to-zero; 1 recommended for SQLite to avoid cold-start restores)"
}

# ---------------------------------------------------------------------------
# Keep-warm (Cloud Scheduler)
# ---------------------------------------------------------------------------

variable "enable_keep_warm" {
  type        = bool
  default     = true
  description = "Create a Cloud Scheduler job that pings the service periodically"
}

variable "keep_warm_interval_minutes" {
  type        = number
  default     = 5
  description = "Interval in minutes for keep-warm ping"
}

# ---------------------------------------------------------------------------
# App settings
# ---------------------------------------------------------------------------

variable "log_level" {
  type        = string
  default     = "INFO"
  description = "Python log level (LOG_LEVEL)"

  validation {
    condition     = contains(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], var.log_level)
    error_message = "log_level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL."
  }
}

variable "require_admin" {
  type        = string
  default     = "true"
  description = "REQUIRE_ADMIN: true or false"

  validation {
    condition     = contains(["true", "false"], var.require_admin)
    error_message = "require_admin must be true or false."
  }
}

variable "soft_delete_retention_days" {
  type        = number
  default     = 30
  description = "SOFT_DELETE_RETENTION_DAYS"

  validation {
    condition     = var.soft_delete_retention_days >= 1
    error_message = "soft_delete_retention_days must be at least 1."
  }
}

variable "syncbot_federation_enabled" {
  type        = bool
  default     = false
  description = "SYNCBOT_FEDERATION_ENABLED"
}

variable "syncbot_instance_id" {
  type        = string
  default     = ""
  description = "SYNCBOT_INSTANCE_ID; leave empty for app auto-generation"
}

variable "syncbot_public_url_override" {
  type        = string
  default     = ""
  description = "SYNCBOT_PUBLIC_URL (HTTPS base, no path)"
}

variable "primary_workspace" {
  type        = string
  default     = ""
  description = "PRIMARY_WORKSPACE Slack Team ID"
}

variable "enable_db_reset" {
  type        = string
  default     = ""
  description = "ENABLE_DB_RESET: 'true' to enable"
}

variable "slack_user_scopes" {
  type        = string
  default     = "chat:write,channels:history,channels:read,files:read,files:write,groups:history,groups:read,groups:write,im:write,reactions:read,reactions:write,team:read,users:read,users:read.email"
  description = "Comma-separated user OAuth scopes (SLACK_USER_SCOPES)"
}

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

variable "secret_slack_signing_secret" {
  type    = string
  default = "syncbot-slack-signing-secret"
}

variable "secret_slack_client_id" {
  type    = string
  default = "syncbot-slack-client-id"
}

variable "secret_slack_client_secret" {
  type    = string
  default = "syncbot-slack-client-secret"
}

variable "secret_slack_bot_scopes" {
  type    = string
  default = "syncbot-slack-scopes"
}

variable "secret_token_encryption_key" {
  type    = string
  default = "syncbot-token-encryption-key"
}

variable "token_encryption_key_override" {
  type        = string
  default     = ""
  sensitive   = true
  description = "DR override for TOKEN_ENCRYPTION_KEY"
}

# ---------------------------------------------------------------------------
# GitHub Actions OIDC (Workload Identity Federation)
# ---------------------------------------------------------------------------

variable "github_repo" {
  type        = string
  default     = ""
  description = "GitHub repo in 'owner/repo' format for WIF (e.g. 'F3-Nation/syncbot'). Empty skips WIF setup."
}
