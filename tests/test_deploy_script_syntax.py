"""Smoke-check deploy shell scripts parse with bash -n."""

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

DEPLOY_SCRIPTS = [
    REPO_ROOT / "deploy.sh",
    REPO_ROOT / "infra" / "gcp" / "scripts" / "deploy.sh",
    REPO_ROOT / "infra" / "aws" / "scripts" / "deploy.sh",
    REPO_ROOT / "infra" / "aws" / "scripts" / "ensure_bootstrap.sh",
    REPO_ROOT / "infra" / "aws" / "scripts" / "ci_sam_deploy_with_fallback.sh",
    REPO_ROOT / "infra" / "aws" / "scripts" / "resolve_database_backend.sh",
    REPO_ROOT / "infra" / "aws" / "scripts" / "print-bootstrap-outputs.sh",
]


@pytest.mark.parametrize("path", DEPLOY_SCRIPTS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_bash_syntax(path: Path) -> None:
    assert path.is_file(), f"missing {path}"
    subprocess.run(
        ["bash", "-n", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_root_deploy_help_has_no_provider_positional() -> None:
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "deploy.sh"), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    help_text = result.stdout
    assert "--env test aws" not in help_text
    assert "./deploy.sh aws" not in help_text
    assert "CLOUD_PROVIDER" in help_text
    assert "[selection]" not in help_text
    assert "Force bootstrap" in help_text


@pytest.mark.parametrize("extra", ["aws", "gcp", "1"])
def test_root_deploy_rejects_provider_positional(extra: str) -> None:
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "deploy.sh"), extra],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "CLOUD_PROVIDER" in combined
    assert "unexpected argument" in combined


def test_root_deploy_rejects_env_with_provider_positional() -> None:
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "deploy.sh"), "--env", "test", "aws"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "unexpected argument" in combined
    assert "aws" in combined


def test_ensure_bootstrap_help() -> None:
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "infra" / "aws" / "scripts" / "ensure_bootstrap.sh"), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    help_text = result.stdout
    assert "--create" in help_text
    assert "--force" in help_text
    assert "--skip-sync" in help_text


def test_infra_deploy_scripts_fail_early_without_cloud_session() -> None:
    aws = (REPO_ROOT / "infra" / "aws" / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    gcp = (REPO_ROOT / "infra" / "gcp" / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    aws_prereqs = aws[aws.index("=== Prerequisites ===") : aws.index("Non-interactive fast path")]
    gcp_prereqs = gcp[gcp.index("=== Prerequisites ===") : gcp.index("prompt_line()")]
    assert "ensure_aws_authenticated" in aws_prereqs
    assert "no active AWS CLI session" in aws
    assert "ensure_gcloud_authenticated" in gcp_prereqs
    assert "no active gcloud session" in gcp
    assert "gcloud auth application-default login" in gcp
    assert "prompt_yn \"Run 'gcloud auth login' now?\"" not in gcp


def test_root_deploy_has_no_secret_manager_lookup() -> None:
    text = (REPO_ROOT / "deploy.sh").read_text(encoding="utf-8")
    assert "resolve_sm_id_vars" not in text
    assert "secretsmanager" not in text
    assert "_SM_ID" not in text
