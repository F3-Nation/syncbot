# Example Terraform variables for infra/gcp (do not commit real secrets).
# Copy to terraform.tfvars locally if you apply by hand instead of ./deploy.sh.

# region defaults to us-central1 if omitted
project_id = "YOUR_PROJECT_ID"
region     = "YOUR_GCP_REGION"
stage      = "test"

# sqlite (default, Litestream + GCS), mysql, or postgresql
database_backend = "sqlite"

# Bootstrap image; CI replaces it. Terraform ignores subsequent image changes.
cloud_run_image = "gcr.io/cloudrun/hello"

# 0 = free/best-effort (default). 1 = paid always-on for Slack 3s.
cloud_run_min_instances = 0
enable_keep_warm        = true

# owner/repo of the GitHub repo that will push to test/prod (your fork, not necessarily sprocktech/syncbot)
github_repo = "YOUR_GITHUB_OWNER/YOUR_REPO"

slack_signing_secret = "replace-me"
slack_client_id      = "111.222"
slack_client_secret  = "replace-me"
data_encryption_key  = "replace-with-token-urlsafe-36"

# Required only when database_backend is mysql or postgresql
# database_host     = "YOUR_DATABASE_HOST"
# database_user     = "YOUR_FULL_USERNAME"
# database_password = "replace-me"
# Leave database_port unset for 3306 (MySQL) or 5432 (PostgreSQL); TiDB Cloud is 4000.
