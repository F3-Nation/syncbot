"""Guardrails for GitHub App CI wiring and deploy.sh lockfile hygiene."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_pr_commits_section_is_api_only() -> None:
    text = (REPO_ROOT / ".github" / "workflows" / "pr-commits-section.yml").read_text()
    template = (REPO_ROOT / ".github" / "pull_request_template.md").read_text()
    assert "pull_request_target" in text
    assert "actions/checkout" not in text
    assert "<!-- commits -->" in text
    assert "<!-- commits -->" in template
    assert "<!-- /commits -->" in template


def test_release_uses_app_token_for_git_api() -> None:
    text = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "actions/create-github-app-token@v3" in text
    assert "github-token: ${{ steps.app-token.outputs.token }}" in text
    assert "GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in text


def test_canonical_upstream_gates_use_community_repo() -> None:
    release = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text()
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    automerge = (REPO_ROOT / ".github" / "workflows" / "dependabot-auto-merge.yml").read_text()
    canonical = "F3Nation-Community/slack-syncbot"
    assert f"github.repository == '{canonical}'" in release
    assert f"github.repository == '{canonical}'" in ci
    assert f"github.repository == '{canonical}'" in automerge
    assert "owner: F3Nation-Community" in release
    assert "repositories: slack-syncbot" in release
    assert "sprocktech/syncbot" not in release
    assert "sprocktech-automation" not in ci.split("forbidden-edits:", 1)[0]
    assert "sprocktech/syncbot" not in automerge


def test_requirements_sync_pushes_with_app_token() -> None:
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    req, _rest = ci.split("forbidden-edits:", 1)
    assert "actions/create-github-app-token@v3" in req
    assert "f3n-community-automation[bot]" in req
    assert "git push" in req
    assert "actions/github-script" not in req


def test_forbidden_edits_fetches_full_base_ref() -> None:
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    _pre, rest = ci.split("forbidden-edits:", 1)
    job = rest.split("forbidden-imports:", 1)[0]
    assert "--depth=1" not in job
    assert 'git fetch origin "${BASE_REF}"' in job
    assert "<!-- version list -->" in job
    assert "must not be edited in PRs except adding" not in job


def test_dependabot_automerge_uses_app_token() -> None:
    text = (REPO_ROOT / ".github" / "workflows" / "dependabot-auto-merge.yml").read_text()
    assert "actions/create-github-app-token@v3" in text
    assert "GH_TOKEN: ${{ steps.app-token.outputs.token }}" in text


def test_release_copies_changelog_section_to_github_release() -> None:
    text = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "function notesFromChangelog" in text
    assert "steps.psr.outputs.release_notes" not in text


def test_psr_changelog_excludes_non_user_facing_commits() -> None:
    text = (REPO_ROOT / "pyproject.toml").read_text()
    assert 'template_dir = ".github/semantic-release/templates"' in text
    assert '"^chore"' in text
    assert '"^ci(?:\\\\(|:)"' in text


def test_changelog_1_2_0_is_keep_a_changelog() -> None:
    text = (REPO_ROOT / "CHANGELOG.md").read_text()
    assert "## [1.2.0] - 2026-08-27" in text
    assert "## v1.2.0" not in text
    assert "Co-authored-by:" not in text.split("## [1.1.0]", 1)[0]
    assert "### Added" in text.split("## [1.1.0]", 1)[0]


def test_deploy_sh_does_not_update_packages() -> None:
    text = (REPO_ROOT / "deploy.sh").read_text()
    assert not any(line.lstrip().startswith("poetry update") for line in text.splitlines())
