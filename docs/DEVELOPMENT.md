# Development Guide

This page is how to run SyncBot on your laptop (Dev Container, Docker Compose, or native Python) and how to manage dependencies. For **cloud deploy** and CI/CD, see [DEPLOY.md](DEPLOY.md). For runtime env vars in any environment, see [INFRA_CONTRACT.md](INFRA_CONTRACT.md).

## Branching (upstream vs downstream)

The **upstream** repository ([F3Nation-Community/slack-syncbot](https://github.com/F3Nation-Community/slack-syncbot)) is the shared codebase. Each deployment maintains its own **fork**:

| Branch | Role |
|--------|------|
| **`main`** | Tracks upstream. Use it to merge PRs and to **sync with the upstream repository** (`git pull upstream main`, etc.). |
| **`test`** / **`prod`** | On your fork, use these for **deployments**: GitHub Actions deploy workflows run on **push** to `test` and `prod` (see [DEPLOY.md](DEPLOY.md)). |

Typical flow: develop on a feature branch → open a PR to **`main`** → merge → when you are ready to deploy, merge **`main`** into **`test`** or **`prod`** on your fork.

## Local development

### Dev Container (recommended)

**You need:** [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or Docker Engine on Linux) and the [Dev Containers](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers) extension in VS Code.

1. Run `cp .env.example .env` and set `SLACK_BOT_TOKEN` (`xoxb-...`).
2. Use **Dev Containers: Reopen in Container** — Python, MySQL, and dependencies run inside the container.
3. Run `cd syncbot && python app.py`. The app listens on **port 3000** (forwarded).
4. Expose it to Slack with **cloudflared** or **ngrok** from the host, then set Slack **Event Subscriptions**, **Interactivity**, and **Redirect** URLs to that public origin (`/slack/events` and `/slack/oauth_redirect`). If you test **Authorize SyncBot**, the Home tab button always starts at this instance's `/slack/install` on the same origin. There is no `SYNCBOT_PUBLIC_URL` to set.

Optional **SQLite**: in `.env` set `DATABASE_BACKEND=sqlite` and `DATABASE_URL=sqlite:////app/syncbot/syncbot.db`.

### Docker Compose (no Dev Container)

```bash
cp .env.example .env   # set SLACK_BOT_TOKEN
docker compose up --build
```

App on port **3000**; restart the `app` service after code changes.

### Native Python

**You need:** Python 3.12+ and Poetry. Run MySQL locally (for example `docker run ... mysql:8`) or use SQLite. See [`.env.example`](../.env.example) and [INFRA_CONTRACT.md](INFRA_CONTRACT.md).

## Configuration reference

- **[`.env.example`](../.env.example)** — local env vars with comments.
- **[INFRA_CONTRACT.md](INFRA_CONTRACT.md)** — runtime contract for any cloud (DB, Slack, OAuth, production vs local).

## Project layout

```
syncbot/
├── syncbot/           # App (app.py); slack_manifest_scopes.py = bot/user OAuth scope lists (manifest + SLACK_BOT_SCOPES / SLACK_USER_SCOPES)
├── syncbot/db/alembic/  # Migrations (bundled with app for Lambda)
├── tests/
├── docs/
├── infra/aws/         # SAM, bootstrap stack, Lambda wrapper (Litestream)
├── infra/gcp/         # Terraform
├── deploy.sh          # Root launcher (macOS / Linux / Git Bash)
├── deploy.ps1         # Windows launcher → Git Bash or WSL → infra/.../deploy.sh
├── slack-manifest.json
└── docker-compose.yml
```

## Dependency management

After `poetry add` / `poetry update`, keep `poetry.lock` and the pinned requirements files aligned:

- **Recommended:** Install [pre-commit](https://pre-commit.com) (`pip install pre-commit && pre-commit install && pre-commit install --hook-type commit-msg`). When you commit a change to `poetry.lock`, the **`sync-requirements`** hook runs `poetry export` and refreshes **`syncbot/requirements.txt`** automatically.

- **Without pre-commit:** Run the export yourself (Poetry 2.x needs the export plugin once: `poetry self add poetry-plugin-export`):

```bash
poetry export -f requirements.txt --without-hashes -o syncbot/requirements.txt
```

The root **`./deploy.sh`** does **not** run `poetry update`. If Poetry and `poetry-plugin-export` are on `PATH`, it may **warn** when committed `*requirements.txt` files differ from a lockfile export; it does not write those files or `poetry.lock`. Local and GitHub Actions deploys install the committed pins (same as `sam build`).

CI **`pip-audit`** exports from `poetry.lock` in the job (see [.github/workflows/ci.yml](../.github/workflows/ci.yml)); it does not audit the committed `*requirements.txt` files. On **same-repo** PRs to **F3Nation-Community/slack-syncbot**, **`requirements-sync`** may commit an export as **`f3n-community-automation[bot]`** (GitHub App token) **without** `[skip ci]` so required checks re-run on HEAD. A `GITHUB_TOKEN` push would not retrigger workflows. Fork PRs fail if the files are stale — run the pre-commit hook. If that bot commit lands on a Dependabot branch, comment **`@dependabot recreate`** so Dependabot can rebase (it refuses branches edited by others). On **push** to `main` / `test` / `prod`, a leftover mismatch may still commit with `[skip ci]`; the App is on the `main` ruleset bypass list so that push can succeed.

## Releases & Versioning

- **Conventional Commits** are required for squash-merged PR titles (see [CONTRIBUTING.md](../CONTRIBUTING.md)); [.github/workflows/pr-title.yml](../.github/workflows/pr-title.yml) enforces the format (Dependabot uses `.github/dependabot.yml` prefixes).
- On **[F3Nation-Community/slack-syncbot](https://github.com/F3Nation-Community/slack-syncbot)** only, pushes to **`main`** run [.github/workflows/release.yml](../.github/workflows/release.yml) (**python-semantic-release** 9.21.2 via `pip`, with GitPython pinned below 3.1.60). GitPython 3.1.60 removed `Actor.name_email_regex`, which PSR 9.x still reads ([upstream #1476](https://github.com/python-semantic-release/python-semantic-release/issues/1476)); the official Docker action rebuilds dependencies at job time and cannot pin that package. The job bumps `[tool.poetry] version` in `pyproject.toml`, tags the repo with `X.Y.Z` (no `v` prefix), and copies that version's CHANGELOG section onto the **GitHub Release**. If `## [Unreleased]` is below `<!-- version list -->`, the job retitles that heading to `## [X.Y.Z] - date` and leaves the rest of the block untouched. If Unreleased is missing, it inserts a Keep a Changelog section from **feat** / **fix** / **perf** subjects (`chore`, `ci`, `docs`, and Dependabot bodies are excluded). Headings must be **Added** / **Changed** / **Fixed** (the templates map PSR's `bug fixes` / `Features`). Match **[1.2.0](../CHANGELOG.md)** for length: one short line per bullet (what changed), not a why/how paragraph or a dump of commit subjects. If that version heading is already in CHANGELOG, PSR keeps it. Forks should **not** run a second release stream.
- Release commits, tags, and GitHub Releases are created via the **GitHub git API** as **`f3n-community-automation[bot]`** (GitHub App token) so they show as **Verified** and can bypass the `main` ruleset. `GITHUB_TOKEN` / `github-actions[bot]` cannot `updateRef` on a PR-required branch. After changing [release.yml](../.github/workflows/release.yml) or rotating the App, re-run **Actions → Release → Run workflow** if a computed release was skipped.
- Forks pull `main` and deploy **`test`** / **`prod`** themselves (see [DEPLOY.md](DEPLOY.md)). There is no automated `main` → `test` promote PR from upstream.

## Required GitHub settings (manual)

These cannot be set from a PR — configure once on **F3Nation-Community/slack-syncbot** under **Settings**:

- **Allow auto-merge**; default merge method **Squash** (use the PR title as the squash commit subject).
- **Branch protection / ruleset** on `main`: require a pull request; required checks **`ci-gate`** and **`conventional`** (the job name from [pr-title.yml](../.github/workflows/pr-title.yml), not “PR title / conventional”). Do **not** require Code Owners or resolved conversations. Prefer not requiring “branch must be up to date” until Dependabot rebase is confirmed.
- **Bypass list:** **Organization admin** (humans) and the GitHub App **`f3n-community-automation`** (see below). Do **not** add Dependabot, Write, or Maintain — Dependabot auto-merge already merges *through the PR* when checks pass; a Dependabot bypass would allow pushing to `main` without a PR. The built-in `github-actions[bot]` does **not** appear in the bypass picker (it is not an installable App).
- Dependabot **version updates** (from `.github/dependabot.yml`) and **security updates** enabled. Disable Dependabot on deploy forks (e.g. `f3-tulsa/syncbot`) so they do not open a second pile of PRs.
- **Secrets / variables** for deploy environments live on the **fork**, not on F3Nation-Community/slack-syncbot — see [DEPLOY.md](DEPLOY.md).

### Automation GitHub App (`f3n-community-automation`)

This is the org’s **GitHub bot** (git push, PR merge, Release `updateRef`, ruleset bypass). It is **not** cloud deploy — AWS/GCP stay OIDC / WIF. Workflows that use it: [release.yml](../.github/workflows/release.yml), [ci.yml](../.github/workflows/ci.yml) (`requirements-sync`), [dependabot-auto-merge.yml](../.github/workflows/dependabot-auto-merge.yml). Reuse it for later bot jobs on this repo; do not widen permissions until a job needs them.

Recreate from scratch:

1. Org **F3Nation-Community** → **Settings → Developer settings → GitHub Apps → New GitHub App**.
2. **GitHub App name:** `f3n-community-automation`. Homepage URL: `https://github.com/F3Nation-Community`. Callback URL: leave blank. **Webhook: uncheck Active** (no events).
3. Repository permissions: **Contents** Read and write; **Pull requests** Read and write; **Issues** Read and write; **Metadata** Read. No org permissions. No user permissions.
4. Where can this App be installed: **Only on this account**. Create.
5. Note **App ID** (integer on the app settings page).
6. **Generate a private key** → download the `.pem` (store in a password manager; do not commit).
7. **Install App** → only **F3Nation-Community/slack-syncbot**.
8. Repo **Settings → Secrets and variables → Actions → Repository secrets** (not Environment secrets, not org secrets): add `AUTOMATION_APP_ID` (the integer) and `AUTOMATION_APP_PRIVATE_KEY` (full PEM including `BEGIN`/`END` lines).
9. Repo **Settings → Secrets and variables → Dependabot → Repository secrets**: **the same two names and values**. Dependabot-triggered workflows cannot read Actions secrets (`requirements-sync` and auto-merge run on Dependabot PRs).
10. Repo **Settings → Rulesets → Rulesets** (the `main` ruleset): **Bypass list** → add GitHub App `f3n-community-automation`. Do **not** add Dependabot, Write, or Maintain. Keep Organization admin for humans.

If you rotate the key or recreate the App: update **both** repository secret stores; re-add the **new** App on the ruleset (the old installation id will not match). Forks do not install this App and do not need these secrets.

## Commit signing (local)

Maintainers using **GPG-signed commits** should keep `commit.gpgsign` enabled locally; automated release and requirements-sync commits are signed as **`f3n-community-automation[bot]`** via the GitHub App. See [AGENTS.md](../AGENTS.md) and [AI_AGENTS.md](AI_AGENTS.md).
