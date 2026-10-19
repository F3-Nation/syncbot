# SyncBot on GCP (Terraform)

This module runs SyncBot on Cloud Run. **SQLite + Litestream to GCS** is the default. You can instead point at **MySQL, PostgreSQL, or TiDB Cloud**. **Cloud SQL is not created.** Secrets are Terraform variables injected as Cloud Run env, or optional Secret Manager when `use_secret_manager` is true.

GitHub Actions never runs `terraform apply`. Keep `infra/gcp/terraform.tfstate` (local by default). A remote GCS backend is optional later. Local `./deploy.sh` builds and pushes the Cloud Run image; GitHub is optional.

First-time install is the root [README](../../README.md). AWS vs GCP database defaults, optional GitHub image CI, and the one-state-file trap are in [DEPLOY.md](../../docs/DEPLOY.md).

## Default warmth vs scale-to-zero

`GCP_CLOUD_RUN_MIN_INSTANCES=1` is the **default** (always-on Cloud Run, billed) so Slack's 3s interactivity budget is met. Combined with keep-warm (`GET /health` every 5 minutes, on by default), the instance stays ready.

- Set `GCP_CLOUD_RUN_MIN_INSTANCES=0` to scale to zero (cheaper). On a cold start, Slack **events** (messages, reactions) are **queued by Cloud Run and/or retried by Slack**, so sync still happens — sometimes a few seconds later. Message and reaction handlers are idempotent on Slack envelope `event_id`, so retries recover a failed first delivery without double-posting. Interactivity (buttons, modals, slash commands) may need a second click until the instance is warm.
- Keep `cpu_idle=true` (request-based billing). If you turn CPU always-on, keep-warm pings become about as expensive as `min_instances=1`.
- Litestream streams WAL while CPU is allocated (during a request or a keep-warm ping). SIGTERM on the entrypoint flushes the replicator.

## Upgrading from Cloud SQL

If you previously applied this module with Cloud SQL (`db-f1-micro`), `terraform apply` **destroys** that instance. Dump or backup first. There is no in-place migrate to SQLite in this tree — Litestream is a new database. Forks that did apply Cloud SQL must backup before upgrading.

## Prerequisites

- [Terraform](https://www.terraform.io/downloads) 1.0 or newer
- The [gcloud](https://cloud.google.com/sdk/docs/install) CLI, with `gcloud auth login` and Application Default Credentials
- A GCP project with billing enabled
- Docker (for local image builds and CI)

## First time and GitHub

Do not apply production variables over a test state file. Build the container from the **repository root** (not `infra/gcp/`):

```bash
docker build -f infra/gcp/Dockerfile --platform linux/amd64 .
```

## Database backends

Do not infer the backend from `DATABASE_HOST`. Stage is only `test` or `prod`.

| `database_backend` / `DATABASE_BACKEND` | Runtime | Notes |
| --- | --- | --- |
| `sqlite` (default) | `DATABASE_BACKEND=sqlite`, `DATABASE_URL=sqlite:////data/syncbot.db`, Litestream → GCS | `max_instances=1`, concurrency 1. No `DATABASE_PASSWORD`. |
| `mysql` / `postgresql` | Host, user, password, schema | Same SQL-host contract as AWS (port 4000, full username including any TiDB prefix). No GCS bucket. Cloud Run may scale above 1 instance. |

## Variables (summary)

| Variable | Description |
|----------|-------------|
| `project_id` | GCP project ID (required) |
| `region` | Region. Terraform default if omitted is `us-central1` — do not copy that into a command unless it is your choice |
| `stage` | `test` or `prod` |
| `database_backend` | `sqlite` (default), `mysql`, or `postgresql` |
| `cloud_run_image` | Terraform bootstrap default `gcr.io/cloudrun/hello`. `./deploy.sh` builds and pushes SyncBot after apply. Optional GitHub Actions can do the same. |
| `cloud_run_min_instances` | `1` (default, billed always-on) or `0` (scale-to-zero) |
| `enable_keep_warm` | Cloud Scheduler `/health` (default `true`) |
| `use_secret_manager` | `false` (default) injects secrets as Cloud Run env. `true` stores them in Secret Manager. |
| `github_repo` | `YOUR_GITHUB_OWNER/YOUR_REPO` for WIF; empty skips WIF and is fine for local deploys |
| `slack_*` / `data_encryption_key` | App secrets (sensitive TF vars, or Secret Manager when `use_secret_manager` is true) |
| `database_host` / `database_user` / `database_password` | Required when `database_backend` is `mysql` or `postgresql` |
| `database_port` | Optional. Empty uses 3306 for MySQL and 5432 for PostgreSQL. |

See [variables.tf](variables.tf) and [example.tfvars](example.tfvars). In the deploy env file the names are `GCP_CLOUD_RUN_IMAGE`, `GCP_CLOUD_RUN_MIN_INSTANCES`, `GCP_USE_SECRET_MANAGER`, and `ENABLE_KEEP_WARM` (unprefixed; that last name is also used on AWS EventBridge).

## Outputs

After `terraform apply`, [print-bootstrap-outputs.sh](scripts/print-bootstrap-outputs.sh) prints GitHub variable suggestions, including `GCP_WORKLOAD_IDENTITY_PROVIDER`.

## HTTP port

Cloud Run sets `PORT` (typically `8080`). The container entrypoint listens on `PORT`.

Cloud Run HTTP/1 inbound requests are 32 MiB. Terraform injects `FEDERATION_HTTP_MAX_MB=30`, the same 2 MiB headroom SAM uses under the Function URL cap. Slack files stay 1 GB. Details are in [INFRA_CONTRACT.md](../../docs/INFRA_CONTRACT.md).

## Security

The Cloud Run service is publicly invokable so Slack can reach it. GitHub Actions is optional; leave `github_repo` empty for a local-only deploy. If you do use WIF, `github_repo` must match the repository that will push to `test` and `prod`.
