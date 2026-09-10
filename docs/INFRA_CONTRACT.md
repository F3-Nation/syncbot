# Infrastructure Contract (Provider-Agnostic)

This document is the runtime contract: what any cloud provider (AWS, GCP, Azure, or your own) must supply so SyncBot runs. Forks can swap the IaC under `infra/<provider>/` as long as they meet this contract.

How you deploy is in [DEPLOY.md](DEPLOY.md) (the root `./deploy.sh` / `.\deploy.ps1` flags, and GitHub vs local apply). Those details are not repeated here.

Schema changes use Alembic (`alembic upgrade head`). On **AWS Lambda**, migrations are **not** run on every Slack cold start. After deploy, invoke `{"action":"migrate"}` (GitHub Actions does this after `sam deploy`; the guided script does the same). On **Cloud Run**, local, or a container, migrations still run at process startup before HTTP (that path has no Slack ack timeout).

## Runtime Environment Variables

The application reads configuration from environment variables. Providers must inject these at runtime (e.g. Lambda env, Cloud Run env, or a compatible secret/config layer).

## Toolchain Baseline

- Runtime baseline: **Python 3.12**.
- Keep runtime/tooling aligned across:
  - Lambda/Cloud Run runtime configuration
  - CI Python version
  - `pyproject.toml` Python constraint
  - `syncbot/requirements.txt` deployment pins
- When dependency constraints change in `pyproject.toml`, refresh the lockfile and deployment requirements. The **pre-commit `sync-requirements` hook** regenerates **`syncbot/requirements.txt`** from `poetry.lock` when you commit lockfile changes. Manually: `poetry lock`, then `poetry export -f requirements.txt --without-hashes -o syncbot/requirements.txt`.

### Database (backend-agnostic)

| Variable | Description |
|----------|-------------|
| `DATABASE_BACKEND` | `mysql`, `postgresql`, or `sqlite`. The **application** default (if unset) is `mysql`. **AWS SAM** also defaults to `mysql`. **GCP** injects `sqlite` unless you set otherwise. |
| `DATABASE_URL` | Full SQLAlchemy URL. When set, it overrides host, user, password, and schema. **Required for SQLite** (for example `sqlite:///path/to/syncbot.db`). For `mysql` or `postgresql` you can omit it and use the host, user, password, and schema variables below. |
| `DATABASE_HOST` | Database hostname (IP or FQDN). Required when backend is `mysql` or `postgresql` and `DATABASE_URL` is unset. |
| `DATABASE_PORT` | Optional. Defaults to **5432** for `postgresql`, **3306** for `mysql`. Set explicitly for external providers that use a non-standard port (e.g. TiDB Cloud **4000**). |
| `DATABASE_USER` | Username. Required when backend is `mysql` or `postgresql` and `DATABASE_URL` is unset. Some providers (e.g. TiDB Cloud Serverless) require a cluster-specific prefix on **every** SQL user — include that prefix in this value (full username). The app and deploy tooling do not prepend a prefix or create users. |
| `DATABASE_PASSWORD` | Password. Required when backend is `mysql` or `postgresql` and `DATABASE_URL` is unset. |
| `DATABASE_SCHEMA` | Database name (MySQL) or PostgreSQL database name (same convention as MySQL). Create this database before first migrate; the app does not `CREATE DATABASE`. SAM default is `syncbot` if the parameter is omitted. Empty env or GitHub reuses the live stack parameter, or infers `syncbot_test` / `syncbot_prod` only when creating a new stack. |
| `DATABASE_TLS_ENABLED` | Optional TLS toggle (`true`/`false`). Defaults to enabled outside local dev. |
| `DATABASE_SSL_CA_PATH` | Optional CA bundle path when TLS is enabled. If unset, the app uses the first existing file among common OS locations (Amazon Linux, Debian, Alpine); PostgreSQL omits `sslrootcert` when none exist so libpq uses the system trust store. |

**SQLite:** Set `DATABASE_BACKEND=sqlite` and `DATABASE_URL=sqlite:///path/to/file.db`. Single-writer; suitable for small teams and dev. Durability is **provider-specific**: **GCP Cloud Run** injects `DATABASE_URL=sqlite:////data/syncbot.db` (four slashes = absolute `/data/syncbot.db`) plus Litestream → GCS (`LITESTREAM_GCS_BUCKET` is container/infra-only). **AWS Lambda** sqlite mode injects `DATABASE_URL=sqlite:////tmp/syncbot.db` plus Litestream → S3 (`LITESTREAM_S3_BUCKET` is wrapper/infra-only). Local sqlite has no Litestream. Horizontal scaling is not supported with SQLite (`max_instances=1` / reserved concurrency 1 on those providers).

**MySQL (default on AWS):** Set `DATABASE_BACKEND=mysql` (or rely on the AWS default) and either `DATABASE_URL` (`mysql+pymysql://...`) or the four host, user, password, and schema variables. The AWS SAM parameter `DatabaseBackend=mysql` (default) matches this. That includes operator-owned RDS or TiDB Cloud — the SAM stack does not create RDS.

**PostgreSQL:** Set `DATABASE_BACKEND=postgresql` and either `DATABASE_URL` (`postgresql+psycopg2://...`) or `DATABASE_HOST`, `DATABASE_USER`, `DATABASE_PASSWORD`, and `DATABASE_SCHEMA`. PostgreSQL is not created by IaC. The runtime user must already exist; there is no admin bootstrap.

### Required in production (non–local)

| Variable | Description |
|----------|-------------|
| `SLACK_SIGNING_SECRET` | Slack request verification (Basic Information → App Credentials). |
| `SLACK_CLIENT_ID` | Slack OAuth client ID. |
| `SLACK_CLIENT_SECRET` | Slack OAuth client secret. |
| `SLACK_BOT_SCOPES` | Comma-separated OAuth **bot** scopes. Must match `slack-manifest.json` `oauth_config.scopes.bot` and `syncbot/slack_manifest_scopes.py` `BOT_SCOPES`. |
| `SLACK_USER_SCOPES` | Comma-separated OAuth **user** scopes. Must match `oauth_config.scopes.user` and `syncbot/slack_manifest_scopes.py` `USER_SCOPES`. If this env requests scopes that are not declared on the Slack app, install fails with `invalid_scope`. |
| `DATA_ENCRYPTION_KEY` | **Required** in production; must be a strong, random value (e.g. 16+ characters). Auto-generated by the deploy script if empty and saved back to the `.env.deploy` file. Back up the key after first deploy — if lost, all workspaces must reinstall. In local dev you may set it manually or leave unset. |

**Reference wiring (SAM / Terraform → app env):** Slack Event / Interactivity / Redirect URLs come from the **deploy receipt** and `slack-manifest_test.json` or `slack-manifest_prod.json`. They are not a separate public-URL env var.

| SAM parameter / TF variable | App env | Notes |
|-----------------------------|---------|--------|
| `SlackOauthBotScopes` / `slack_bot_scopes` | `SLACK_BOT_SCOPES` | Defaults match `BOT_SCOPES` |
| `SlackOauthUserScopes` / `slack_user_scopes` | `SLACK_USER_SCOPES` | Defaults match `USER_SCOPES` |
| `LogLevel` / `log_level` | `LOG_LEVEL` | |
| `PrimaryWorkspace` / `primary_workspace` | `PRIMARY_WORKSPACE` | Hidden Backup/Restore until set **and redeployed**. AWS `--setup-github` copies it when it is set in the env file. |
| `EnableDbReset` / `enable_db_reset` | `ENABLE_DB_RESET` | Boolean; also gated by `PRIMARY_WORKSPACE` |
| `DatabaseTlsEnabled` / `DatabaseSslCaPath` (and TF equivalents) | `DATABASE_TLS_*` | Omit when empty so app defaults apply |
| `DatabaseBackend=mysql` / `postgresql` + `DatabaseHost` / port / user / password / schema | `DATABASE_*` | Deploy scripts map `DATABASE_HOST`, `DATABASE_USER`, `DATABASE_PASSWORD`, `DATABASE_SCHEMA` |
| `DatabaseBackend=sqlite` (AWS) | `DATABASE_BACKEND=sqlite`, `DATABASE_URL=sqlite:////tmp/syncbot.db` | `LITESTREAM_S3_BUCKET` wrapper-only |
| `database_backend=sqlite` (GCP, default) | `DATABASE_BACKEND=sqlite`, `DATABASE_URL=sqlite:////data/syncbot.db` | `LITESTREAM_GCS_BUCKET` container-only. Cloud SQL is not created. |

Deploy-only warmth knobs are **not** app runtime env: **`GCP_CLOUD_RUN_MIN_INSTANCES`** (`0` or `1`) and **`ENABLE_KEEP_WARM`**. **AWS:** EventBridge ScheduleV2 **invokes the Lambda** (not HTTP). **GCP:** Cloud Scheduler **`GET /health`**. Do not inject these into the Slack process as if they were the same mechanism.

### Optional

| Variable | Description |
|----------|-------------|
| `SLACK_BOT_TOKEN` | Set by OAuth flow; placeholder until first install. |
| `PRIMARY_WORKSPACE` | Slack Team ID of the primary workspace. Required for backup/restore to be visible. DB reset (if enabled) is also scoped to this workspace. |
| `ENABLE_DB_RESET` | When `true` / `1` / `yes` and `PRIMARY_WORKSPACE` matches the current workspace, shows the Reset Database button. Not prompted during deploy; set it in the env file (AWS `--setup-github` copies it when present), or in SAM / Terraform. |
| `LOCAL_DEVELOPMENT` | `true` only for local dev; disables token verification and enables dev shortcuts. |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` (default `INFO`). |
| `PORT` | HTTP listen port for container entrypoint (`python app.py` / Cloud Run). Cloud Run injects this (typically `8080`); default `3000` when unset. |

**Leftover env (not deploy inputs):** `REQUIRE_ADMIN`, `SYNCBOT_FEDERATION_ENABLED`, `SYNCBOT_PUBLIC_URL`, `SYNCBOT_INSTANCE_ID`, `ALLOW_PRIVATE_CHANNELS`, `BROADCAST_ALLOWED_WORKSPACES`, and `SOFT_DELETE_RETENTION_DAYS` are ignored if still set on an old process; the app logs a warning. Federation, private-channel policy, broadcast allowlists, soft-delete retention, and admin policy belong in **Settings**, not env. This instance's federation id is the SHA-256 hex fingerprint of its Ed25519 public key (64 characters), not a deploy-time UUID.

### Settings modal

Operational policy is edited in the **Settings** modal on the SyncBot Home tab. Slack **admins and owners** on any installed workspace can open it. Each workspace always sees its own fields: extra managers (members who can configure groups and syncs without opening Settings), and whether private Channels may be published in that workspace (default off). Those workspace keys live in `workspace_settings`. Saving a value writes it to the database, so it takes effect without a redeploy.

Instance-wide fields appear only when `PRIMARY_WORKSPACE` is set and matches the acting workspace: enable federation (default off), how long uninstalled workspace data is kept (default 30 days), and which workspaces may publish a Broadcast (empty means any; this list is stored now and is used when broadcast channels ship). Those instance keys live in `instance_settings`. The last public Host is stored there too (`public_base_url`) for federation connection codes; it is not a Settings field. If `PRIMARY_WORKSPACE` is unset, those three blocks are hidden everywhere. Backup/Restore and Reset Database stay on the primary workspace (reset also needs `ENABLE_DB_RESET`).

Private Channels and writes as a mapped person need per-user authorization, which is not a configuration value and needs no new environment variable. Slack will not let an app add itself to a private Channel. Target messages, files, and native reactions use the mapped person's user token for that target workspace when it is available; message and file writes otherwise fall back to the workspace bot token with author customization, while reaction behavior follows the selected Hybrid, Direct, or Off type. SyncBot never uses another member's token and never sends a user token over federation. Those tokens come from the OAuth install this instance already serves and are stored by Bolt in `slack_installations`; the **Authorize SyncBot** button on the Home tab is that same install flow for one more person.

`PRIMARY_WORKSPACE` and `ENABLE_DB_RESET` stay environment-only: they decide which workspace sees instance Settings fields, Backup/Restore, and Reset Database, so they should only be changeable by whoever can deploy the instance.

## Platform Capabilities

The provider must deliver:

1. **Public HTTPS endpoint**
   Slack sends events and interactivity to a single base URL. The app expects:
   - `POST /slack/events` — events and actions
   - `GET /slack/install` — OAuth start (302 to Slack; sets the state cookie Bolt checks on callback)
   - `GET /slack/oauth_redirect` — OAuth callback; on success, that user's Home tab is published so Authorize SyncBot can disappear
   - `GET /health` — liveness (JSON `{"status":"ok"}`) for keep-warm probes
   Any path under `/api/federation` is used for federation when enabled.

2. **Secret injection**
   Slack and DB credentials must be available as environment variables (or equivalent) at process start. No assumption of a specific secret store; the provider injects them (for example Lambda env or Cloud Run env).

3. **Database**
   **PostgreSQL / MySQL:** In non–local environments the app uses TLS by default; allow outbound TCP to the DB host (typically **5432** for PostgreSQL, **3306** for MySQL, **4000** for TiDB Cloud). The operator creates the database and app user; the app only runs Alembic. **SQLite:** No SQL network; the app uses a local file. Single-writer; production durability is provider-specific (GCP: Litestream replica in GCS; AWS: Litestream replica in S3). Cloud SQL / stack RDS are not required.

4. **Keep-warm / scheduled ping (optional but recommended)**
   To avoid cold-start latency, the provider should ping the service on an interval (for example every 5 minutes). **AWS (SAM):** EventBridge Scheduler invokes the Lambda directly; the Lambda handler returns a small JSON success for `source` `aws.scheduler` / `aws.events` without treating the payload as a Slack request. **GCP:** Cloud Scheduler `GET /health` (Terraform `enable_keep_warm`, default on). Keep `cpu_idle=true` (request-based billing) so the ping is a tiny request, not 24/7 CPU.

5. **Stateless execution**
   The app is stateless; state lives in the configured database (PostgreSQL, MySQL, or SQLite). Horizontal scaling is supported with PostgreSQL/MySQL as long as all instances share the same DB and env; SQLite is single-writer. The public origin used for OAuth install and federation webhooks is the Host of incoming Slack requests (the Event URL), not a separate env var.

6. **At-least-once Slack delivery**
   The Events API may deliver the same envelope more than once (Slack retries, queued cold starts). Message and reaction sync is idempotent on envelope ``event_id`` + ``team_id`` (table ``processed_events``). Providers must not assume exactly-once HTTP delivery.

## CI Auth Model

- **Preferred:** Short-lived federation (e.g. OIDC for AWS, Workload Identity Federation for GCP). No long-lived API keys in GitHub Secrets for deploy.
- **Bootstrap:** One-time creation of a deploy role (or service account) with least-privilege permissions for deploying the app and its resources.
- **Outputs:** Bootstrap should expose values needed for CI (see below) so users can plug them into GitHub variables.

## Bootstrap Output Contract

After running provider-specific bootstrap (e.g. AWS CloudFormation bootstrap stack, GCP Terraform), the following outputs should be available so users can configure GitHub Actions and/or local deploy:

| Output key | Description | Typical use |
|------------|-------------|-------------|
| `deploy_role` | ARN or identifier of the role/identity that CI (or local) uses to deploy | GitHub variable for OIDC/WIF role-to-assume |
| `artifact_bucket` (or equivalent) | Bucket or registry where deploy artifacts (packages, images) are stored | GitHub variable; deploy step uploads here |
| `region` | Primary region for the deployment | GitHub variable (e.g. `AWS_REGION`, `GCP_REGION`) |
| `service_url` | Public base URL of the deployed app (optional at bootstrap; may come from app stack) | For Slack app configuration and docs |
| `workload_identity_provider` (GCP) | Full WIF provider resource name (`projects/…/providers/…`) | GitHub variable `GCP_WORKLOAD_IDENTITY_PROVIDER` |

**AWS:** `artifact_bucket` is `DeploymentBucketName` in bootstrap outputs; this repo stores it as the GitHub variable `AWS_S3_BUCKET` (SAM/CI packaging for `sam deploy` only; not Slack or app media).

**GCP:** `artifact_bucket` equivalent is Artifact Registry (`artifact_registry_repository`). `deploy_role` equivalent is `deploy_service_account_email`. Terraform state is local by default; GitHub never runs `terraform apply`.

Provider-specific implementations may use different names (e.g. `GitHubDeployRoleArn`, `DeploymentBucketName`) but should document the mapping to this contract.

## Swapping Providers

To use a different cloud or IaC stack:

1. Keep `syncbot/` and app behavior unchanged.
2. Add or replace contents of `infra/<provider>/` with templates/scripts that satisfy the contract above.
   - To integrate with the repo-level launcher (`./deploy.sh` and `.\deploy.ps1`), provide `infra/<provider>/scripts/deploy.sh` only. On Windows, `deploy.ps1` invokes that bash script via Git Bash or WSL; do not add a separate `deploy.ps1` under `infra/`.
3. Point CI (e.g. `.github/workflows/deploy-<provider>.yml`) at the new infra paths and provider-specific auth (OIDC, WIF, etc.).
4. Update [DEPLOY.md](DEPLOY.md) (or provider-specific README under `infra/<provider>/`) with bootstrap and deploy steps that emit the bootstrap output contract.

No application code changes are required when swapping infra as long as the runtime environment variables and platform capabilities are met.

## Fork Compatibility Policy

To keep forks easy to rebase and upstream contributions easy to merge:

1. Keep provider-specific changes under `infra/<provider>/` and `.github/workflows/deploy-<provider>.yml`.
2. Do not couple `syncbot/` application code to a cloud provider (AWS/GCP/Azure-specific SDK calls, metadata assumptions, or IAM wiring). The optional `slack_bolt.adapter.aws_lambda.SlackRequestHandler` import in `app.py` is a justified exception so Cloud Run images built from `syncbot/requirements.txt` (no boto3) can start; `handler()` still requires the adapter on Lambda.
3. Treat this file as the source of truth for runtime env contract; if a fork adds infra behavior, map it back to this contract.
4. Upstream PRs should include only provider-neutral app changes unless a provider-specific file is explicitly being updated.
5. The following files are **canonical-upstream automation** on [F3Nation-Community/slack-syncbot](https://github.com/F3Nation-Community/slack-syncbot), not a per-fork runtime contract: `.github/workflows/release.yml`, `.github/workflows/dependabot-auto-merge.yml`, `.github/workflows/pr-title.yml`, `.github/dependabot.yml`, `.github/CODEOWNERS`. The GitHub App **`f3n-community-automation`** and repository secrets `AUTOMATION_APP_ID` / `AUTOMATION_APP_PRIVATE_KEY` (Actions **and** Dependabot stores) are also canonical-upstream; forks do not install the App. Forks should pull `main` and deploy with their own Environments; they must **not** run a second python-semantic-release. See [AI_AGENTS.md](AI_AGENTS.md) and [DEVELOPMENT.md](DEVELOPMENT.md).
