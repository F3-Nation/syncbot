"""Operator-facing docs and examples use DATABASE_BACKEND only (no alias names)."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN = (
    "AWS_DATABASE_MODE",
    "GCP_DATABASE_MODE",
    "DATABASE_ENGINE",
    "EXISTING_DATABASE_",
    "DatabaseMode",
    "ExistingDatabaseHost",
    "ExistingDatabasePort",
    "database_mode",
    "existing_db_",
    "_SM_ID",
)

FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)


def _operator_paths() -> list[Path]:
    paths = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "AGENTS.md",
        REPO_ROOT / ".env.example",
        REPO_ROOT / ".env.deploy.example",
        REPO_ROOT / "infra" / "gcp" / "README.md",
        REPO_ROOT / "infra" / "gcp" / "example.tfvars",
    ]
    paths.extend(sorted((REPO_ROOT / "docs").glob("*.md")))
    return [p for p in paths if p.name != "CHANGELOG.md"]


def test_operator_facing_files_forbid_legacy_database_names() -> None:
    hits: list[str] = []
    for path in _operator_paths():
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN:
            if needle in text:
                rel = path.relative_to(REPO_ROOT)
                hits.append(f"{rel}: {needle}")
    assert hits == [], "operator-facing files still mention legacy database names:\n" + "\n".join(hits)


def test_copy_paste_fences_use_placeholders_not_finished_values() -> None:
    markdown_paths = [
        p for p in _operator_paths() if p.suffix == ".md" or p.name.endswith(".example") or p.name == "example.tfvars"
    ]
    bad: list[str] = []
    for path in markdown_paths:
        text = path.read_text(encoding="utf-8")
        for i, block in enumerate(FENCE.findall(text), start=1):
            rel = path.relative_to(REPO_ROOT)
            if "Stage=test" in block:
                bad.append(f"{rel} fence {i}: Stage=test")
            if '-var="stage=test"' in block or "-var=stage=test" in block:
                bad.append(f"{rel} fence {i}: -var=stage=test")
            if "us-east-2" in block:
                bad.append(f"{rel} fence {i}: us-east-2")
            if "us-east-1" in block:
                bad.append(f"{rel} fence {i}: us-east-1")
            if "gateway.tidbcloud.com" in block:
                bad.append(f"{rel} fence {i}: gateway.tidbcloud.com")
    assert bad == [], "copy-paste fences still use finished values:\n" + "\n".join(bad)


def test_env_deploy_example_leaves_database_port_blank() -> None:
    text = (REPO_ROOT / ".env.deploy.example").read_text(encoding="utf-8")
    assert "DATABASE_PORT=YOUR_DATABASE_PORT" not in text
    assert "DATABASE_PORT=" in text


LEGACY_PROVIDER_TOKEN = re.compile(
    r"(?<![A-Z0-9_])("
    r"DEPLOY_TARGET|ENABLE_XRAY|BOOTSTRAP_STACK_NAME|CLOUD_RUN_IMAGE|"
    r"UPDATE_STACK|CREATE_OIDC_PROVIDER|DEPLOY_BUCKET_PREFIX|"
    r"DEPLOYMENT_S3_BUCKET|AWS_ROLE_ARN|STACK_NAME|STAGE_NAME"
    r")(?![A-Z0-9_])"
)


def test_operator_facing_files_forbid_legacy_provider_env_names() -> None:
    hits: list[str] = []
    for path in _operator_paths():
        text = path.read_text(encoding="utf-8")
        for match in LEGACY_PROVIDER_TOKEN.finditer(text):
            rel = path.relative_to(REPO_ROOT)
            start = max(0, match.start() - 20)
            snippet = text[start : match.end() + 20].replace("\n", " ")
            hits.append(f"{rel}: {match.group(1)} ({snippet!r})")
    assert hits == [], "operator-facing files still mention unprefixed provider env names:\n" + "\n".join(hits)


def test_operator_facing_files_do_not_say_setup_github_does_not_push() -> None:
    hits: list[str] = []
    needle = "`--setup-github` does not push"
    for path in _operator_paths():
        text = path.read_text(encoding="utf-8")
        if needle in text:
            hits.append(str(path.relative_to(REPO_ROOT)))
    assert hits == [], "operator-facing files still say --setup-github does not push:\n" + "\n".join(hits)
