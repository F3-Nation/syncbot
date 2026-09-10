# SyncBot
<img src="assets/icon.png" alt="SyncBot Icon" width="128">

SyncBot is a Slack app for syncing messages across workspaces. Once it is configured, it syncs messages, threads, edits, deletes, reactions, images, videos, and GIFs to every channel in a SyncBot group.

> **Using SyncBot in Slack already?** See the [User Guide](docs/USER_GUIDE.md).

---

## Slack app setup

Do this before you deploy to the cloud or run locally. Placeholder URLs in the starting manifest are fine while you create the Slack app. After you deploy, paste the generated `slack-manifest_test.json` (or `slack-manifest_prod.json`) back into the app.

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From an app manifest** → paste [`slack-manifest.json`](slack-manifest.json).
2. Upload [`assets/icon.png`](assets/icon.png) under **Basic Information** → **Display Information**.
3. Copy **Signing Secret**, **Client ID**, and **Client Secret**. You need those for a cloud deploy. For **local development**, install the app under **OAuth & Permissions** and copy the **Bot User OAuth Token** (`xoxb-...`).

---

## Cloud deploy

After you have set up the Slack app, you can follow the steps below to deploy to a cloud provider. GitHub Actions is optional and can be set up with the deploy script. For a small install, we recommend AWS on the free tier with [TiDB Cloud](https://www.pingcap.com/tidb-cloud/)'s free MySQL plan. GCP is supported too; SQLite is the default there, so you do not need to create a SQL user. AWS vs GCP database defaults and the other `DATABASE_BACKEND` options are in [DEPLOY.md](docs/DEPLOY.md#database-backends).

**Prerequisites**

- Git and Bash. On Windows, use Git Bash or WSL.
- **AWS:** AWS CLI v2, SAM CLI, Python 3, `curl`, and an active `aws` login.
- **GCP:** Terraform, `gcloud`, Python 3, `curl`, and an active `gcloud` login (including Application Default Credentials).
- Optional: `gh`, if you want the script to write GitHub Environment variables for you.

1. **Set up the database** — MySQL or PostgreSQL only; skip this if you are using SQLite.

   Create a database and a least-privilege user **before** you deploy. The app does not create those for you. `DATABASE_USER` must be the **full** username (on TiDB Cloud, include the cluster prefix). Stage is only `test` or `prod`. Name the database `syncbot_test` or `syncbot_prod` so it matches `DATABASE_SCHEMA` (the convention is `syncbot_` plus the stage). Here is a MySQL example for `test`:

   ```sql
   CREATE DATABASE IF NOT EXISTS syncbot_test;
   CREATE USER 'YOUR_FULL_USERNAME'@'%' IDENTIFIED BY 'a-strong-password';
   GRANT ALL ON syncbot_test.* TO 'YOUR_FULL_USERNAME'@'%';
   FLUSH PRIVILEGES;
   ```

   PostgreSQL is the same idea (`CREATE DATABASE` / `CREATE USER` / grants). The full recipe is in [DEPLOY.md](docs/DEPLOY.md#create-the-database-and-app-user). For a cloud deploy, the host must be reachable from the public internet.

2. **Clone/download/fork the repo and set up your env file**

   Clone or download this repo locally, or fork it into your own repo. For non-interactive deploys, copy `.env.deploy.example` to `.env.deploy.test` or `.env.deploy.prod` for your deploy environment. Open this file in your editor of choice and fill in your deploy settings.

3. **Run the deploy script**

   - On **macOS / Linux**, run `./deploy.sh`.
   - On **Windows**, run `.\deploy.ps1`.
   - With no `--env` flag, the script runs interactively and prompts for inputs. Pass `--env test` or `--env prod` so it loads your deploy file and skips interactive prompts.
   - Add `--setup-github` if you want later deploys from pushes to the `test` or `prod` branches. On AWS, that copies env file keys that AWS CI actually reads (including `PRIMARY_WORKSPACE` if you set it). On GCP, it only writes Workload Identity Federation repo vars and `GITHUB_DEPLOY_TARGET`. It does not replace the first local AWS bootstrap or GCP `terraform apply`. GCP GitHub Actions never runs `terraform apply`, it only builds and pushes a container image. Run the deploy script again for infra, secrets, database, and warmth changes to GCP. If you setup GitHub and use AWS, it **does** run `sam deploy` on push. See [docs/DEPLOY.md](docs/DEPLOY.md).

4. **Update the Slack app and save secrets**

   - Paste the generated manifest (`slack-manifest_test.json` or `slack-manifest_prod.json`) into the Slack app manifest. Save so the **Event**, **Interactivity**, **Redirect**, and **Install** URLs match your new endpoint.
   - Save the **`DATA_ENCRYPTION_KEY`** from the env file or the deploy receipt somewhere safe. If you lose it, workspaces have to reinstall.
   - **Backup/Restore** on the Home tab stays hidden until you set `PRIMARY_WORKSPACE` to a Slack Team ID and **redeploy**. Put it in the env file so AWS `--setup-github` can copy it, then redeploy.

---

## Local development

For local development, run `cp .env.example .env` and set your Slack app variables. See **[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)** for the Dev Container, Docker Compose, native Python, project layout, and how to refresh `syncbot/requirements.txt` after dependency changes.

---

## Further reading

| Doc | Contents |
|-----|----------|
| [USER_GUIDE.md](docs/USER_GUIDE.md) | End-user features (Home tab, syncs, groups) |
| [DEPLOY.md](docs/DEPLOY.md) | AWS vs GCP databases, GitHub CI, manual SAM and Terraform |
| [DEVELOPMENT.md](docs/DEVELOPMENT.md) | Local dev, branching for forks, dependencies |
| [INFRA_CONTRACT.md](docs/INFRA_CONTRACT.md) | Environment variables and platform expectations |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Sync flow, AWS reference architecture |
| [BACKUP_AND_MIGRATION.md](docs/BACKUP_AND_MIGRATION.md) | Backup/restore and federation migration |
| [API_REFERENCE.md](docs/API_REFERENCE.md) | HTTP routes and Slack events |
| [CHANGELOG.md](CHANGELOG.md) | Release history (updated by python-semantic-release on F3Nation-Community `main`) |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to contribute |
| [AI_AGENTS.md](docs/AI_AGENTS.md) | AI/coding-agent workflow and CI guardrails |

## License

**AGPL-3.0** — see [LICENSE](LICENSE).
