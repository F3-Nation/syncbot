# Contributing

Thanks for helping to improve SyncBot. This page is how we take patches: branches, Conventional Commits, and what to run before you open a pull request.

## Branching (upstream vs downstream)

The **upstream** repository ([F3Nation-Community/slack-syncbot](https://github.com/F3Nation-Community/slack-syncbot)) is the shared codebase. Each deployment maintains its own **fork**:

| Branch | Role |
|--------|------|
| **`main`** | Tracks upstream. Use it to merge PRs and to **sync with the upstream repository** (`git pull upstream main`, etc.). |
| **`test`** / **`prod`** | On your fork, use these for **deployments**: GitHub Actions deploy workflows run on **push** to `test` and `prod` (see [docs/DEPLOY.md](docs/DEPLOY.md)). |

Typical flow: develop a fix or new feature on a branch in your repo → test and deploy to your infra → open a PR to **`upstream/main`**.

**Upstream PRs:** python-semantic-release, Dependabot auto-merge, and `pr-title.yml` run on **F3Nation-Community/slack-syncbot** only (see [docs/INFRA_CONTRACT.md](docs/INFRA_CONTRACT.md) Fork Compatibility Policy). Forks should pull `main` and deploy `test`/`prod` themselves — do not run a second semantic-release on the fork.

### Branch Naming Conventions

Format: `<type>/<description>` or `<type>/<ticket>-<description>`

Types:

- feature/ New functionality
- bugfix/ Bug fixes for existing features
- hotfix/ Urgent production issues
- refactor/ Code improvements without behavior changes
- docs/ Documentation only changes
- chore/ Build process, dependency updates, etc.

Rules:

- Use lowercase
- Separate words with hyphens
- Keep descriptions under 50 characters
- Be specific: feature/user-auth not feature/auth

## Workflow

1. **Fork** the repository and create a branch from **`main`**.
2. Open a **pull request** targeting **`main`** on the upstream repo (or the repo you were asked to contribute to).
3. Keep application code **provider-neutral**: put cloud-specific logic only under `infra/<provider>/` and in `deploy-<provider>.yml` workflows. See [docs/INFRA_CONTRACT.md](docs/INFRA_CONTRACT.md) (Fork Compatibility Policy).

## Commit messages (Conventional Commits)

This repository uses [Conventional Commits](https://www.conventionalcommits.org/) for clarity and automated versioning on **`main`**.

- Use imperative mood and a type prefix, for example: `feat: add channel mute toggle`, `fix: handle missing OAuth state`, `docs: clarify deploy env vars`, `chore: bump checkout action`.
- Keep the **subject to about 72 characters**. Skip the body unless you need `BREAKING CHANGE:` or one clause that will not fit the subject. Do not explain Slack API background or prior failures in the commit.
- Allowed types commonly used here: `feat`, `fix`, `perf`, `refactor`, `docs`, `build`, `chore`, `ci`, `test`, `style`.
- **Patch** bump: `fix:`, `perf:` (and similar non-breaking fixes).
- **Minor** bump: `feat:` (user-visible additions).
- **Major** bump: add `BREAKING CHANGE:` in the commit body/footer, or use a `feat!:` / `fix!:` subject line per Conventional Commits.
- **Squash merges**: the PR title becomes the merge commit **subject** — set the PR title to a valid Conventional Commit (CI enforces this via `.github/workflows/pr-title.yml`). CI fills a **Commits** section on the PR (no `Co-authored-by` lines); keep those as subject lines only. Paste that block into the squash body if you want those messages on `main`. GitHub then adds a single `Co-authored-by` trailer.
- **Changelog / GitHub Release:** Keep a Changelog headings only (**Added** / **Changed** / **Fixed**). Copy **[1.2.0](CHANGELOG.md)** for length: one short line per bullet, what changed, not why. Put those bullets under `## [Unreleased]` below `<!-- version list -->`; the release job retitles that heading to the version and date. If Unreleased is missing, python-semantic-release still drafts from `feat` / `fix` / `perf`. Do not hand-write `## [X.Y.Z]`.

Also install the commit-msg hook so local commits are checked:

```bash
pre-commit install --hook-type commit-msg
```

## Before you submit

- Run **`pre-commit run --all-files`** (install with `pip install pre-commit && pre-commit install && pre-commit install --hook-type commit-msg` if needed).
- Ensure **CI passes**: requirements export check, SAM template lint, ruff, pip-audit, and tests (see [.github/workflows/ci.yml](.github/workflows/ci.yml)).
- If you change dependencies in `pyproject.toml`, refresh the lockfile and `syncbot/requirements.txt` as described in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Questions

Use [GitHub Issues](https://github.com/F3Nation-Community/slack-syncbot/issues) for bugs and feature ideas, or check [docs/DEPLOY.md](docs/DEPLOY.md) for deploy-related questions.

**AI / coding agents:** see [docs/AI_AGENTS.md](docs/AI_AGENTS.md) and root [AGENTS.md](AGENTS.md).
