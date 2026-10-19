# Example Terraform variables for infra/gcp (do not commit real secrets).
# Copy to terraform.tfvars locally if you apply by hand instead of ./deploy.sh.

# region defaults to us-central1 if omitted
project_id = "YOUR_PROJECT_ID"
region     = "YOUR_GCP_REGION"
stage      = "test"

# sqlite (default, Litestream + GCS), mysql, or postgresql
database_backend = "sqlite"

# Bootstrap only. ./deploy.sh builds SyncBot and updates Cloud Run after apply.
cloud_run_image = "gcr.io/cloudrun/hello"

# 0 = scale-to-zero (cheaper). 1 = always-on (default, Slack 3s).
cloud_run_min_instances = 1
enable_keep_warm        = true

# Optional GitHub Actions WIF. Empty skips WIF; local ./deploy.sh still builds and pushes.
github_repo = ""

# Optional Secret Manager for Slack secrets and DATA_ENCRYPTION_KEY (billed). Default false.
use_secret_manager = false

slack_signing_secret = "replace-me"
slack_client_id      = "111.222"
slack_client_secret  = "replace-me"
data_encryption_key  = "replace-with-token-urlsafe-36"

# Required only when database_backend is mysql or postgresql
# database_host     = "YOUR_DATABASE_HOST"
# database_user     = "YOUR_FULL_USERNAME"
# database_password = "replace-me"
# Leave database_port unset for 3306 (MySQL) or 5432 (PostgreSQL); TiDB Cloud is 4000.
