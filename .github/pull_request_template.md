## Summary

<!-- A few short bullets. What changed, not Slack API background. -->

## PR title (Conventional Commit)

<!-- One line; squash merge uses this as the commit subject. Example: feat: add channel sync toggle -->

## How to test

<!-- Steps or scenarios reviewers can run -->

## AI-authored?

<!-- If an agent helped: which platform (Cursor / Copilot / other) and link the issue. -->

## Checklist

- [ ] PR title is a valid **Conventional Commit** (see [CONTRIBUTING.md](../CONTRIBUTING.md))
- [ ] No `pyproject.toml` version bump and no `## [X.Y.Z]` CHANGELOG heading (releases own those). `## [Unreleased]` notes below `<!-- version list -->` are OK. Do not hand-edit `*requirements.txt` (poetry export owns those).
- [ ] CI passes (`ci-gate`: requirements sync, forbidden path checks, ruff, SAM lint / terraform-validate / docker-build-gcp when those paths change, pip-audit when deps change, tests)
- [ ] Docs updated if behavior or deploy steps changed
- [ ] No new cloud-provider-specific code under `syncbot/` (keep infra in `infra/` and workflows)

## Commits

Copy this list into the squash merge body. Keep **subject lines only**. GitHub will add one Co-authored-by trailer.

<!-- commits -->
<!-- /commits -->
