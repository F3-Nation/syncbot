# Example: copy to staging.tfvars / prod.tfvars and fill in values.

project_id = "your-gcp-project-id"
region     = "us-central1"
stage      = "staging"   # or "prod"

cloud_run_image = ""     # Set after first image push (e.g. us-central1-docker.pkg.dev/PROJECT/syncbot-staging-images/syncbot:SHA)

# Keep one instance warm so SQLite doesn't need a cold-start restore every request.
# Set to 0 for scale-to-zero (cheaper but slower cold starts).
cloud_run_min_instances = 1

# GitHub repo for Workload Identity Federation (OIDC).
# Set to "" to skip WIF and configure auth manually.
github_repo = "F3-Nation/syncbot"

# App settings
log_level         = "INFO"
require_admin     = "true"
primary_workspace = ""   # Slack Team ID (T...) of your primary workspace
