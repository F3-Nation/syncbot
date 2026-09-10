"""Terraform validation for the module next to this package.

``terraform init -backend=false`` may need network access to download providers.
Uses ``TF_DATA_DIR`` in a temp directory so the repo tree is not modified.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

INFRA_GCP = Path(__file__).resolve().parent.parent


def _which(name: str) -> str | None:
    return shutil.which(name)


def test_terraform_validates() -> None:
    tf = _which("terraform")
    if not tf:
        pytest.skip("terraform not on PATH")
    assert INFRA_GCP.is_dir()
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ)
        env["TF_DATA_DIR"] = tmp
        init = subprocess.run(
            [tf, "init", "-backend=false", "-input=false"],
            cwd=INFRA_GCP,
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
        )
        if init.returncode != 0:
            pytest.skip(
                f"terraform init failed (terraform missing or no network for providers?):\n{init.stdout}\n{init.stderr}"
            )
        validate = subprocess.run(
            [tf, "validate"],
            cwd=INFRA_GCP,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        assert validate.returncode == 0, f"terraform validate failed:\n{validate.stdout}\n{validate.stderr}"


def test_gcp_module_has_no_cloud_sql() -> None:
    main_tf = (INFRA_GCP / "main.tf").read_text(encoding="utf-8")
    assert "google_sql_" not in main_tf
    assert "sqladmin" not in main_tf
    assert "random_password" not in main_tf
    assert 'DATABASE_URL          = "sqlite:////data/syncbot.db"' in main_tf


def test_wif_trusts_repository_claim_not_subject_claim() -> None:
    """GitHub's immutable ``sub`` (repo:owner@id/repo@id) must not break WIF.

    Only ``sub`` gained the embedded IDs; ``repository`` is still ``owner/repo``.
    Matching on the subject instead would break every repository created or
    transferred after 2026-07-15, the way the AWS trust policy did.
    """
    main_tf = (INFRA_GCP / "main.tf").read_text(encoding="utf-8")
    assert "attribute_condition = \"assertion.repository == '${var.github_repo}'\"" in main_tf
    assert '"attribute.repository" = "assertion.repository"' in main_tf
    assert "attribute.repository/${var.github_repo}" in main_tf
    assert "assertion.sub ==" not in main_tf
    assert "google.subject/repo:" not in main_tf


def test_dockerfile_pins_litestream_sha256() -> None:
    dockerfile = (INFRA_GCP / "Dockerfile").read_text(encoding="utf-8")
    assert "LITESTREAM_VERSION=v0.3.13" in dockerfile
    assert "LITESTREAM_SHA256=eb75a3de5cab03875cdae9f5f539e6aedadd66607003d9b1e7a9077948818ba0" in dockerfile
    assert "COPY infra/gcp/litestream.yml" in dockerfile
    assert "COPY infra/gcp/entrypoint.sh" in dockerfile


def test_entrypoint_does_not_exec_python_while_litestream_runs() -> None:
    entry = (INFRA_GCP / "entrypoint.sh").read_text(encoding="utf-8")
    assert entry.count("exec python app.py") == 1
    assert "python app.py &" in entry
    assert "trap cleanup" in entry
    unset_branch, _, litestream_branch = entry.partition("litestream replicate")
    assert "exec python app.py" in unset_branch
    assert "exec python" not in litestream_branch


def test_gcp_existing_uses_full_database_user() -> None:
    main_tf = (INFRA_GCP / "main.tf").read_text(encoding="utf-8")
    vars_tf = (INFRA_GCP / "variables.tf").read_text(encoding="utf-8")
    deploy = (INFRA_GCP / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert "existing_db_username_prefix" not in vars_tf
    assert "existing_db_create_app_user" not in vars_tf
    assert "prefix.sbapp" not in main_tf
    assert "DATABASE_ADMIN" not in deploy
    assert "DATABASE_CREATE_APP_USER" not in deploy


def test_deploy_script_sqlite_skips_required_password() -> None:
    script = (INFRA_GCP / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert "DATABASE_PASSWORD:?" not in script
    assert "GCP_DATABASE_MODE" in script
    assert "DATABASE_ENGINE" in script
    assert "GCP_CLOUD_RUN_MIN_INSTANCES" in script
    assert "ENABLE_KEEP_WARM" in script
    assert "GCP_CLOUD_RUN_IMAGE" in script
    assert "-var=database_backend=" in script
    assert "-var=database_mode=" not in script
    assert "DATABASE_PORT:-3306" not in script
    assert "Database TCP port is required" not in script
    vars_tf = (INFRA_GCP / "variables.tf").read_text(encoding="utf-8")
    assert 'variable "database_mode"' in vars_tf
    assert 'variable "existing_db_host"' in vars_tf
    assert 'variable "existing_db_schema"' in vars_tf
    assert 'variable "existing_db_user"' in vars_tf
    assert '"sqlite"' in vars_tf
    mode_block = vars_tf.split('variable "database_mode"', 1)[1].split("variable ", 1)[0]
    backend_block = vars_tf.split('variable "database_backend"', 1)[1].split("variable ", 1)[0]
    port_block = vars_tf.split('variable "database_port"', 1)[1].split("variable ", 1)[0]
    assert 'default     = "sqlite"' in mode_block
    assert 'default     = ""' in backend_block
    assert 'default     = ""' in port_block
    outputs_tf = (INFRA_GCP / "outputs.tf").read_text(encoding="utf-8")
    assert 'output "database_mode"' in outputs_tf
    assert 'output "database_backend"' in outputs_tf
    main_tf = (INFRA_GCP / "main.tf").read_text(encoding="utf-8")
    assert "syncbot_database_backend" in main_tf
    assert "syncbot_database_mode" in main_tf
    assert "DATABASE_PORT              = var.database_port" not in main_tf
    assert "DATABASE_PORT = trimspace(var.database_port)" in main_tf


def test_deploy_gcp_workflow_is_gated_and_not_a_stub() -> None:
    workflow = (INFRA_GCP.parents[1] / ".github" / "workflows" / "deploy-gcp.yml").read_text(encoding="utf-8")
    assert "(vars.GITHUB_DEPLOY_TARGET || vars.DEPLOY_TARGET) == 'gcp'" in workflow
    assert "vars.DEPLOY_TARGET == 'gcp'" not in workflow
    gcp_deploy = (INFRA_GCP / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert "push_github_gcp_wif" in gcp_deploy
    assert "gh variable set GITHUB_DEPLOY_TARGET" in gcp_deploy
    assert "gh variable set DEPLOY_TARGET" not in gcp_deploy
    assert "gh variable set STAGE_NAME" not in gcp_deploy
    assert "gh_variable_set_env STAGE_NAME" not in gcp_deploy
    assert "gh variable set SLACK_CLIENT_ID" not in gcp_deploy
    assert "docker build -f infra/gcp/Dockerfile" in workflow
    assert "gcloud run services update" in workflow
    assert "GCP deploy is not implemented" not in workflow
    assert "placeholder until" not in workflow.lower()


def test_main_tf_has_no_leftover_syncbot_env() -> None:
    main_tf = (INFRA_GCP / "main.tf").read_text(encoding="utf-8")
    for name in ("SYNCBOT_INSTANCE_ID", "SYNCBOT_PUBLIC_URL", "REQUIRE_ADMIN"):
        assert name not in main_tf


def test_existing_db_schema_defaults_to_stage() -> None:
    vars_tf = (INFRA_GCP / "variables.tf").read_text(encoding="utf-8")
    schema_block = vars_tf.split('variable "existing_db_schema"', 1)[1].split("variable ", 1)[0]
    assert 'default     = ""' in schema_block
    assert "syncbot_$${stage}" in schema_block
    main_tf = (INFRA_GCP / "main.tf").read_text(encoding="utf-8")
    assert 'name_prefix = "syncbot-${var.stage}"' in main_tf
    assert '"syncbot_${var.stage}"' in main_tf
    deploy = (INFRA_GCP / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert "syncbot_${STAGE}" in deploy
    assert "slack_bot_scopes" in deploy
    assert "slack_user_scopes" in deploy
