# F3 Nation GCP Deployment (SQLite + Litestream on Cloud Run)

This guide covers deploying SyncBot to GCP Cloud Run using **SQLite** with **Litestream** for continuous backup to GCS — no managed database required.

The infrastructure lives in `infra/gcp-nation/`. Deployments are triggered by pushing to the `nation-staging` or `nation-prod` branches.

---

## Architecture overview

```
┌───────────────┐     ┌──────────────────────────────┐     ┌─────────────┐
│  Slack API    │────▶│  Cloud Run (max_instances=1)  │────▶│  GCS Bucket │
│  (events)     │     │  ┌─────────┐  ┌───────────┐  │     │  (Litestream│
│               │     │  │ SyncBot │  │ Litestream │──│────▶│   replica)  │
│               │◀────│  │ (Python)│  │ (sidecar)  │  │     └─────────────┘
└───────────────┘     │  └─────────┘  └───────────┘  │
                      └──────────────────────────────┘
```

- **Single instance** — SQLite is single-writer; Cloud Run `max_instances=1` enforced by Terraform.
- **Litestream** continuously streams WAL changes to a GCS bucket (sub-second lag).
- On cold start, the entrypoint restores the DB from GCS before the app boots.
- **Keep-warm** Cloud Scheduler pings `/health` every 5 minutes to minimize cold starts.

---

## Prerequisites

- **GCP account** with a project and billing enabled
- **gcloud** CLI authenticated (`gcloud auth login && gcloud auth application-default login`)
- **Terraform** >= 1.0
- **Docker** (for local image builds; CI builds in GitHub Actions)
- A Slack workspace where you can create apps

---

## Step 1: Slack app setup

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From an app manifest**.
2. Paste the contents of [`slack-manifest.json`](../../slack-manifest.json). You'll update the URLs after the first deploy.
3. Upload [`assets/icon.png`](../../assets/icon.png) under **Basic Information → Display Information**.
4. Note down these values (you'll need them for Secret Manager):
   - **Signing Secret** (Basic Information → App Credentials)
   - **Client ID** (Basic Information → App Credentials)
   - **Client Secret** (Basic Information → App Credentials)
5. Under **OAuth & Permissions → Scopes**, verify the bot and user scopes match [`syncbot/slack_manifest_scopes.py`](../../syncbot/slack_manifest_scopes.py).

---

## Step 2: GCP project setup

```bash
# Set your project
export PROJECT_ID="your-project-id"
export REGION="us-central1"
gcloud config set project "$PROJECT_ID"

# Enable required APIs (Terraform does this too, but useful for bootstrapping)
gcloud services enable \
  run.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  cloudscheduler.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com
```

---

## Step 3: First Terraform apply (bootstrap)

The first apply creates all resources but uses a placeholder image. You'll update the image after the first build.

Staging and prod can share the same GCP project. Terraform **workspaces** keep their state separate so resources don't collide.

```bash
cd infra/gcp-nation

# Copy and edit the example tfvars
cp example.tfvars staging.tfvars
# Edit staging.tfvars: set project_id, region, stage = "staging", github_repo

terraform init

# Create a workspace for staging (each stage gets its own state)
terraform workspace new staging

# First apply needs a throwaway image since the real one doesn't exist yet.
# Use a minimal public image as a placeholder:
terraform apply \
  -var-file=staging.tfvars \
  -var='cloud_run_image=gcr.io/cloudrun/hello'
```

After apply, note the outputs:

```bash
terraform output
# → service_url, artifact_registry_repository, deploy_service_account_email,
#   workload_identity_provider, litestream_bucket
```

> **Important:** Always select the correct workspace before running `terraform apply`:
>
> ```bash
> terraform workspace select staging   # or: terraform workspace select prod
> ```

---

## Step 4: Set Slack secrets in Secret Manager

```bash
STAGE="staging"  # or "prod"

# Signing Secret
printf '%s' 'YOUR_SIGNING_SECRET' | \
  gcloud secrets versions add "syncbot-${STAGE}-syncbot-slack-signing-secret" --data-file=-

# Client ID
printf '%s' 'YOUR_CLIENT_ID' | \
  gcloud secrets versions add "syncbot-${STAGE}-syncbot-slack-client-id" --data-file=-

# Client Secret
printf '%s' 'YOUR_CLIENT_SECRET' | \
  gcloud secrets versions add "syncbot-${STAGE}-syncbot-slack-client-secret" --data-file=-

# Bot Scopes (comma-separated, must match slack-manifest.json)
printf '%s' 'app_mentions:read,channels:history,channels:join,channels:manage,channels:read,channels:write.invites,channels:write.topic,chat:write,chat:write.customize,commands,files:read,files:write,groups:history,groups:read,groups:write,groups:write.invites,groups:write.topic,im:write,links:read,links:write,reactions:read,reactions:write,team:read,users:read,users:read.email,users.profile:read' | \
  gcloud secrets versions add "syncbot-${STAGE}-syncbot-slack-scopes" --data-file=-
```

`TOKEN_ENCRYPTION_KEY` is auto-generated by Terraform. **Back it up** from the Terraform output or via:

```bash
gcloud secrets versions access latest --secret="syncbot-${STAGE}-syncbot-token-encryption-key"
```

---

## Step 5: Build and push the first image

```bash
REGION="us-central1"
PROJECT_ID="your-project-id"
STAGE="staging"
REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/syncbot-${STAGE}-images"
TAG="$(git rev-parse --short HEAD)"

# Build using the Nation Dockerfile (from repo root)
docker build -f infra/gcp-nation/Dockerfile -t "${REPO}/syncbot:${TAG}" .
docker push "${REPO}/syncbot:${TAG}"

# Update Terraform with the real image
cd infra/gcp-nation
terraform apply \
  -var-file=staging.tfvars \
  -var="cloud_run_image=${REPO}/syncbot:${TAG}"
```

---

## Step 6: Update Slack app URLs

After the first deploy, get the service URL:

```bash
terraform output -raw service_url
# e.g. https://syncbot-staging-abc123-uc.a.run.app
```

Go to [api.slack.com/apps](https://api.slack.com/apps) → your app:

1. **Event Subscriptions → Request URL**: `https://YOUR_URL/slack/events`
2. **Interactivity & Shortcuts → Request URL**: `https://YOUR_URL/slack/events`
3. **OAuth & Permissions → Redirect URLs**: add `https://YOUR_URL/slack/oauth_redirect`

Or re-run the deploy script to update via the manifest API:

```bash
# From repo root
./deploy.sh   # select GCP, then the Slack API task
```

---

## Step 7: Install the Slack app

Visit `https://YOUR_URL/slack/install` in your browser to install SyncBot into your workspace via OAuth.

---

## Step 8: Set up GitHub Actions (CI/CD)

### GitHub repo variables

Set these in **Settings → Secrets and variables → Actions → Variables**:

| Variable                         | Value                                                |
| -------------------------------- | ---------------------------------------------------- |
| `GCP_PROJECT_ID`                 | Your GCP project ID                                  |
| `GCP_REGION`                     | e.g. `us-central1`                                   |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | From `terraform output workload_identity_provider`   |
| `GCP_SERVICE_ACCOUNT`            | From `terraform output deploy_service_account_email` |

No secrets are needed in GitHub — Slack credentials live in GCP Secret Manager.

### Branch strategy

```
main (upstream trunk)
  │
  ├──▶ nation-staging   (merge from main → auto-deploys to staging)
  └──▶ nation-prod      (merge from main → auto-deploys to prod)
```

Create the branches:

```bash
git checkout main
git checkout -b nation-staging
git push origin nation-staging

git checkout main
git checkout -b nation-prod
git push origin nation-prod
```

To deploy: merge `main` into `nation-staging` or `nation-prod` and push. The `.github/workflows/deploy-nation.yml` workflow builds the image and updates Cloud Run.

---

## Step 9: Repeat for prod

```bash
cd infra/gcp-nation
cp example.tfvars prod.tfvars
# Edit prod.tfvars: stage = "prod", set project_id, region, github_repo

# Create a separate workspace for prod (keeps state isolated from staging)
terraform workspace new prod

terraform apply \
  -var-file=prod.tfvars \
  -var='cloud_run_image=gcr.io/cloudrun/hello'
# Then set secrets (Step 4), build/push image (Step 5),
# update Slack app URLs (Step 6), and install (Step 7) for prod.
```

To switch between environments later:

```bash
terraform workspace select staging   # switch to staging state
terraform workspace select prod      # switch to prod state
terraform workspace list             # show all workspaces
```

> Each workspace has its own `terraform.tfstate`, so `terraform apply -var-file=prod.tfvars` in the `prod` workspace will never touch staging resources, and vice versa.

---

## Environment variables reference

These are set automatically by Terraform. You do not need to set them manually.

| Variable                | Value                        | Source                     |
| ----------------------- | ---------------------------- | -------------------------- |
| `DATABASE_BACKEND`      | `sqlite`                     | Terraform                  |
| `DATABASE_URL`          | `sqlite:////data/syncbot.db` | Terraform                  |
| `LITESTREAM_GCS_BUCKET` | auto-created bucket name     | Terraform                  |
| `LITESTREAM_GCS_PATH`   | `syncbot.db`                 | Terraform                  |
| `SLACK_SIGNING_SECRET`  | from Secret Manager          | Terraform                  |
| `SLACK_CLIENT_ID`       | from Secret Manager          | Terraform                  |
| `SLACK_CLIENT_SECRET`   | from Secret Manager          | Terraform                  |
| `SLACK_BOT_SCOPES`      | from Secret Manager          | Terraform                  |
| `TOKEN_ENCRYPTION_KEY`  | from Secret Manager          | Terraform (auto-generated) |

---

## Backup & recovery

**Litestream handles continuous backup automatically.** The GCS bucket has versioning enabled (keeps 3 versions).

### Manual restore (disaster recovery)

If you need to restore to a specific point:

```bash
# Install litestream locally
brew install litestream  # or download from https://litestream.io

# Restore from GCS
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa-key.json
litestream restore -o ./restored.db \
  gcs://BUCKET_NAME/syncbot.db
```

### Token encryption key

If the `TOKEN_ENCRYPTION_KEY` is lost, all stored bot tokens become unrecoverable. Workspaces will need to re-install the app. Back up this key:

```bash
gcloud secrets versions access latest --secret="syncbot-STAGE-syncbot-token-encryption-key"
```

---

## Cost estimate

| Resource                                    | Monthly cost                                       |
| ------------------------------------------- | -------------------------------------------------- |
| Cloud Run (1 min instance, 1 vCPU, 512 MiB) | ~$0–8 (free tier covers 2M requests + 360k vCPU-s) |
| GCS bucket (Litestream replica, <100 MB)    | ~$0.02                                             |
| Cloud Scheduler (keep-warm)                 | Free (3 free jobs)                                 |
| Secret Manager (5 secrets)                  | Free (6 free active versions)                      |
| Artifact Registry (images)                  | ~$0.10/GB                                          |
| **Total**                                   | **~$0–10/month**                                   |

---

## Troubleshooting

**Cold start is slow:**
Set `cloud_run_min_instances = 1` in your tfvars and enable keep-warm (default). This keeps one instance alive.

**"Database is locked" errors:**
This means multiple Cloud Run instances are running. Verify `max_instances = 1` in Terraform. Check: `gcloud run services describe syncbot-STAGE --region REGION --format='value(spec.template.metadata.annotations)'`.

**Litestream restore fails on startup:**
Check the Cloud Run SA has `roles/storage.objectAdmin` on the Litestream bucket. View logs: `gcloud run services logs read syncbot-STAGE --region REGION --limit=50`.

**App won't start (migration error):**
SQLite doesn't support all ALTER TABLE operations. If an Alembic migration fails, check the migration scripts in `syncbot/db/alembic/versions/` for SQLite compatibility.
