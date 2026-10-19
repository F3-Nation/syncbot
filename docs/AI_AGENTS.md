# AI agents on SyncBot

This repository is set up so coding agents (Cursor, GitHub Copilot, Codex, Claude) can work with clear boundaries. Start with [AGENTS.md](../AGENTS.md) for commands, pitfalls, and **docs voice**. This page is CI guardrails, issue templates, and how we review agent-authored PRs.

## Read these first

| File | Purpose |
|------|---------|
| [AGENTS.md](../AGENTS.md) | Primary guardrails, commands, pitfalls, docs voice, example names |
| [.github/copilot-instructions.md](../.github/copilot-instructions.md) | Short Copilot-specific checklist |
| [.cursor/rules/](../.cursor/rules/) | Cursor rules (architecture, short commits/changelog, helpers imports, tests, infra) |
| [.cursor/rules/75-target-pipeline.mdc](../.cursor/rules/75-target-pipeline.mdc) | Message Sync Flow `#L` links, same-instance / cross-instance |
| [.cursor/rules/45-example-names.mdc](../.cursor/rules/45-example-names.mdc) | Generic fixtures (`Workspace A`, `Ada Lovelace`; `Partner Org` is a connection label) |
| [.cursor/rules/90-slack-free-tier.mdc](../.cursor/rules/90-slack-free-tier.mdc) | Slack API research is Free-tier only |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | Short Conventional Commits + workflow |

## Plan files (never commit)

Write working plans only under **`.plans/`** at the repo root. That folder is gitignored and is not documentation. Do not park plans under `docs/` (even ignored `docs/PLAN_*.md` names look like operator docs). Cursor UI plans live outside the tree; do not copy them into `docs/`. `git status` at PR time must not show a plan file.

## How CI guards agents

On pull requests, [.github/workflows/ci.yml](../.github/workflows/ci.yml) includes:

- **`forbidden-edits`** — blocks removing the CHANGELOG `<!-- version list -->` marker, hand bumps of `version` in `pyproject.toml`, and `*requirements.txt` changes that are not paired with `poetry.lock` / `pyproject.toml`. `syncbot/constants.py` `__version__` stays in lockstep with `pyproject.toml` via the release job; a test fails if they drift.
- **`forbidden-imports`** — blocks `boto3` / `google.cloud` imports under `syncbot/`.
- **`ruff`** — `ruff check` and `ruff format --check`.
- **`pip-audit`** — exports from `poetry.lock` and audits (runs when Python dependency files change).
- **`sam-lint`** — `sam validate --lint` (runs when AWS templates / SAM workflow pins change).
- **`terraform-validate`** — `terraform init -backend=false`, `validate`, and `fmt -check` in `infra/gcp` (path-filtered; skipped is OK).
- **`docker-build-gcp`** — `docker build -f infra/gcp/Dockerfile --platform linux/amd64 .` from the repo root, no push (path-filtered).
- **`test`** — `pytest` over `tests/` and infra tests (same as local command in [AGENTS.md](../AGENTS.md)).
- **`ci-gate`** — aggregator; the check to require on `main`. Skipped path-filtered jobs count as success.
- **`requirements-sync`** — on same-repo PRs to F3Nation-Community/slack-syncbot, may commit `*requirements.txt` from `poetry.lock` as **`f3n-community-automation[bot]`** **without** `[skip ci]` so `ci-gate` / `conventional` re-run on HEAD. Forks must export themselves.

Release automation and signed bot commits are described in [DEVELOPMENT.md](DEVELOPMENT.md) — Releases & Versioning and **Automation GitHub App**. `release.yml` uses the App token for `git.updateRef`; `GITHUB_TOKEN` cannot bypass the `main` ruleset. Dependabot auto-merge also uses the App token so merge can retrigger workflows.

## Filing an AI-friendly issue

Use **AI-eligible task** in GitHub’s issue templates. Include goal, acceptance criteria, how to test, and what’s out of scope.

## Reviewing AI-authored PRs

- Confirm the PR title matches **Conventional Commits** (required for squash merges). Subject only; no essay body.
- Changelog bullets stay **1.2.0** length (one short line). Reject paragraph dumps.
- Look for forbidden-file edits; CI should fail them, but reviewers should still watch for secrets.
- Ensure tests cover behavior changes; spot-check Slack/event flows when touching handlers.
- Pytest must pass with no new warnings. Reject `pytest.skip` / `filterwarnings` used to hide SAWarning or DeprecationWarning. Fix the source.
- Pytest `pythonpath` is `syncbot/` and `infra/aws/lambda` only. Import test helpers as `from tests.event_fixtures import …`; do not add `tests` to `pythonpath`.

## When changing Slack user scopes

`USER_SCOPES` in [`syncbot/slack_manifest_scopes.py`](../syncbot/slack_manifest_scopes.py) must stay in lockstep with the manifests and `SLACK_USER_SCOPES` defaults (see that module's header). The Home tab **Authorize SyncBot** section does not list those API names. It lists `USER_PERMISSION_GROUPS`.

The 1.3.2 list was built as follows; keep new rows on the same rails:

1. Start from the manifest **user** scopes, not bot scopes.
2. Look up each scope on [Slack's scopes reference](https://docs.slack.dev/reference/scopes) for the *user-token* meaning, then write a 2–4 word label. Never paste `channels:history`. Do not start with "Can" or "Allow".
3. Fold scopes people experience as one capability (history+read of the same channel type, files read+write, reactions, users.read + email). Keep `groups:write` as its own line because inviting the bot into a private Channel is not the same as viewing one.
4. A group counts as already allowed only when every scope in it is on the stored token. First-time authorize hides the already-allowed list; a later scope add shows it with checkmarks so re-authorize does not look like a redo.

`tests/test_slack_manifest_scopes.py` asserts every `USER_SCOPE` sits in exactly one group. The recipe also lives as comments on `USER_PERMISSION_GROUPS`.

## Pitfalls live in AGENTS.md

Do not restate OAuth install, routing, the target pipeline, Hybrid probe, `#channel` / permalinks, DB identity, or User Mapping here. See [AGENTS.md](../AGENTS.md) **Common pitfalls**. Inside `helpers/*.py`, import submodules only; callers outside the package may use `import helpers` / `helpers.X`. The **Message Sync Flow** in [ARCHITECTURE.md](ARCHITECTURE.md) is a `#L` walkthrough; keep those links and the Slack → SyncBot → Slack mermaid current when the pipeline moves.

## Fork compatibility

`release.yml`, Dependabot auto-merge, and semantic-release config apply to **F3Nation-Community/slack-syncbot** only. Deploy forks keep `test`/`prod` Environments and must not mint duplicate GitHub Releases. CODEOWNERS handles are organization-specific; replace `@sprocktech-dev` on other orgs. See [INFRA_CONTRACT.md](INFRA_CONTRACT.md) Fork Compatibility Policy.

## Branch protection (F3Nation-Community/slack-syncbot)

Configure in GitHub **Settings → Rulesets / Branches** for `main`:

- Require a pull request before merging
- Required checks: **`ci-gate`**, **`conventional`**
- Do **not** require review from Code Owners (that would block Dependabot auto-merge)
- Do **not** add Dependabot, Write, or Maintain to the ruleset bypass list; `github-actions[bot]` cannot be added here. Organization admin bypass is for humans. Add GitHub App **`f3n-community-automation`** so Release `updateRef` and leftover requirements-sync pushes can land on `main` (see [DEVELOPMENT.md](DEVELOPMENT.md)).
- Allow auto-merge; squash only; do not require conversation resolution

Exact job names come from `.github/workflows/ci.yml` and `pr-title.yml`.
