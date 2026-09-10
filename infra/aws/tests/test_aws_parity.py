"""AWS SAM / Litestream / deploy-script assertions after stack-RDS removal."""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

import db as db_mod

INFRA_AWS = Path(__file__).resolve().parent.parent
REPO_ROOT = INFRA_AWS.parent.parent
TEMPLATE = (INFRA_AWS / "template.yaml").read_text(encoding="utf-8")
BOOTSTRAP = (INFRA_AWS / "template.bootstrap.yaml").read_text(encoding="utf-8")
MAKEFILE = (INFRA_AWS / "lambda" / "Makefile").read_text(encoding="utf-8")
HANDLER = (INFRA_AWS / "lambda" / "handler.py").read_text(encoding="utf-8")
LITESTREAM_YML = (INFRA_AWS / "lambda" / "litestream.yml").read_text(encoding="utf-8")
DEPLOY_SH = (INFRA_AWS / "scripts" / "deploy.sh").read_text(encoding="utf-8")
ENSURE = (INFRA_AWS / "scripts" / "ensure_bootstrap.sh").read_text(encoding="utf-8")
CI_SAM = (INFRA_AWS / "scripts" / "ci_sam_deploy_with_fallback.sh").read_text(encoding="utf-8")
WORKFLOW = (REPO_ROOT / ".github" / "workflows" / "deploy-aws.yml").read_text(encoding="utf-8")


def test_aws_region_default_is_us_east_1() -> None:
    ensure = (INFRA_AWS / "scripts" / "ensure_bootstrap.sh").read_text(encoding="utf-8")
    print_boot = (INFRA_AWS / "scripts" / "print-bootstrap-outputs.sh").read_text(encoding="utf-8")
    samconfig = (REPO_ROOT / "samconfig.toml").read_text(encoding="utf-8")
    example = (REPO_ROOT / ".env.deploy.example").read_text(encoding="utf-8")
    for text in (DEPLOY_SH, ensure, print_boot, samconfig, example):
        assert "us-east-2" not in text
    assert "${AWS_REGION:-us-east-1}" in DEPLOY_SH
    assert "${AWS_REGION:-us-east-1}" in ensure
    assert "${AWS_REGION:-us-east-1}" in print_boot
    assert 'region = "us-east-1"' in samconfig


def test_template_has_no_rds_or_vpc() -> None:
    assert "AWS::RDS::" not in TEMPLATE
    assert "RDSInstance" not in TEMPLATE
    assert "!GetAtt RDSInstance" not in TEMPLATE
    assert "DbSetup" not in TEMPLATE
    assert "CreateDatabase" not in TEMPLATE
    assert "VpcConfig" not in TEMPLATE
    assert "AWSLambdaVPCAccessExecutionRole" not in TEMPLATE
    assert "ExistingDatabaseAdmin" not in TEMPLATE
    assert "DatabaseInstanceClass" not in TEMPLATE
    assert "VpcCidr" not in TEMPLATE


def test_template_sqlite_and_keep_warm() -> None:
    assert "DatabaseBackend:" in TEMPLATE
    assert "DatabaseMode:" not in TEMPLATE
    assert "ExistingDatabaseHost:" not in TEMPLATE
    assert "ExistingDatabasePort:" not in TEMPLATE
    assert "DatabaseEngine:" not in TEMPLATE
    assert "MemorySize: 256" in TEMPLATE
    assert "MemorySize: 128" not in TEMPLATE
    assert "Timeout: 120" in TEMPLATE
    assert "Timeout: 10" not in TEMPLATE
    assert "Timeout: 30" not in TEMPLATE
    assert "sqlite:////tmp/syncbot.db" in TEMPLATE
    assert "LITESTREAM_S3_BUCKET" in TEMPLATE
    assert "ReservedConcurrentExecutions" in TEMPLATE
    assert "State: !If [KeepWarmEnabled, ENABLED, DISABLED]" in TEMPLATE
    assert "Handler: handler.handler" in TEMPLATE
    assert "CodeUri: lambda/" in TEMPLATE
    assert "BuildMethod: makefile" in TEMPLATE
    assert "DatabaseHostInUse:" in TEMPLATE
    assert "LitestreamBucketName:" in TEMPLATE
    assert "RDSEndpoint" not in TEMPLATE
    assert "VpcId" not in TEMPLATE


def test_template_has_no_syncbot_instance_id() -> None:
    assert "SyncbotInstanceId" not in TEMPLATE
    assert "SYNCBOT_INSTANCE_ID" not in TEMPLATE
    assert "SyncbotInstanceId" not in CI_SAM
    assert "SYNCBOT_INSTANCE_ID:" not in WORKFLOW
    assert "SyncbotInstanceId=" not in DEPLOY_SH
    assert "SyncbotInstanceId=" not in CI_SAM


def test_bootstrap_has_no_rds_or_vpc_create() -> None:
    assert "rds:" not in BOOTSTRAP.lower()
    assert "ec2:CreateVpc" not in BOOTSTRAP
    assert "AWS::RDS::" not in BOOTSTRAP


def test_bootstrap_oidc_trust_accepts_immutable_subject_claim() -> None:
    """Repos created or transferred after 2026-07-15 send repo:owner@id/repo@id."""
    assert "GitHubImmutableRepository:" in BOOTSTRAP
    assert 'HasImmutableRepository: !Not [!Equals [!Ref GitHubImmutableRepository, ""]]' in BOOTSTRAP
    assert '- - !Sub "repo:${GitHubRepository}:*"' in BOOTSTRAP
    assert '    - !Sub "repo:${GitHubImmutableRepository}:*"' in BOOTSTRAP
    # A wildcard under StringEquals is compared literally and never matches.
    assert "StringLike:" in BOOTSTRAP


def test_ensure_bootstrap_passes_and_refreshes_immutable_repository() -> None:
    assert "GitHubImmutableRepository=$immutable_repo" in ENSURE
    assert "actions/oidc/customization/sub" in ENSURE
    # A subject-claim change alone must still trigger a sync.
    assert '"$IMMUTABLE_REPO" == "$STACK_IMMUTABLE_REPO"' in ENSURE


def _resolve_immutable_repository(env: dict[str, str], repo: str = "owner/repo") -> str:
    """Run ensure_bootstrap.sh's helper without executing the script body."""
    definitions = ENSURE.split("stack_exists()")[0]
    result = subprocess.run(
        ["bash", "-c", f'{definitions}\nresolve_immutable_repository "{repo}"'],
        cwd=INFRA_AWS / "scripts",
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_resolve_immutable_repository_uses_actions_runner_ids() -> None:
    subject = _resolve_immutable_repository(
        {
            "GITHUB_REPOSITORY": "my-org/syncbot",
            "GITHUB_REPOSITORY_OWNER_ID": "12345678",
            "GITHUB_REPOSITORY_ID": "9876543210",
        }
    )
    assert subject == "my-org@12345678/syncbot@9876543210"


def test_resolve_immutable_repository_is_empty_without_ids_or_gh() -> None:
    assert _resolve_immutable_repository({}) == ""


def test_makefile_pins_litestream_and_installs_app() -> None:
    assert "LITESTREAM_VERSION := v0.3.13" in MAKEFILE
    assert "LITESTREAM_SHA256 := eb75a3de5cab03875cdae9f5f539e6aedadd66607003d9b1e7a9077948818ba0" in MAKEFILE
    assert 'cp -a "$(SYNCBOT_SRC)/." "$(ARTIFACTS_DIR)/"' in MAKEFILE
    assert 'pip install -r "$(LAMBDA_REQUIREMENTS)" -t "$(ARTIFACTS_DIR)"' in MAKEFILE
    assert "sha256sum -c" in MAKEFILE


def test_makefile_resolves_paths_independently_of_make_working_directory() -> None:
    """SAM runs make from a scratch directory, so $(CURDIR) is not the CodeUri."""
    assert "MAKEFILE_LIST" in MAKEFILE
    assert "$(CURDIR)" not in MAKEFILE
    assert 'cp "$(LAMBDA_DIR)/handler.py"' in MAKEFILE
    assert 'cp "$(LAMBDA_DIR)/litestream.yml"' in MAKEFILE


def test_makefile_installs_wheels_for_lambda_runtime_not_build_host() -> None:
    """A macOS or arm64 host must not leak its own wheels into the artifact."""
    assert "--only-binary=:all:" in MAKEFILE
    assert "--implementation cp" in MAKEFILE
    assert "--python-version 3.12" in MAKEFILE
    assert "--platform manylinux2014_x86_64" in MAKEFILE
    # pip evaluates markers against the host, so they are stripped before install.
    assert "sed 's/ ;.*$$//'" in MAKEFILE


def test_aws_build_is_in_source_not_containerised() -> None:
    """syncbot/ lives above CodeUri; a container build never mounts it."""
    samconfig = (REPO_ROOT / "samconfig.toml").read_text(encoding="utf-8")
    root_deploy = (REPO_ROOT / "deploy.sh").read_text(encoding="utf-8")
    assert DEPLOY_SH.count('sam build -t "$APP_TEMPLATE" --build-in-source') == 2
    assert "--use-container" not in DEPLOY_SH
    assert "sam build -t infra/aws/template.yaml --build-in-source" in WORKFLOW
    assert "--use-container" not in WORKFLOW
    assert samconfig.count("build_in_source = true") == 3
    assert "use_container" not in samconfig
    # Docker was only ever needed for the container build.
    assert "prereqs_require_cmd docker" not in DEPLOY_SH
    assert 'prereqs_print_cli_status_matrix "AWS" aws sam python3 curl' in DEPLOY_SH
    assert "prereqs_hint_docker" not in root_deploy


def test_wrapper_restore_then_init_then_replicate() -> None:
    body = HANDLER[HANDLER.find("def _bootstrap_sqlite") :]
    restore_at = body.find('"restore"')
    init_at = body.find("initialize_database")
    replicate_at = body.find('"replicate"')
    assert 0 <= restore_at < init_at < replicate_at
    assert "-if-replica-exists" in body
    assert "type: s3" in LITESTREAM_YML
    assert "sync-interval: 1s" in LITESTREAM_YML
    assert "region: ${AWS_REGION}" in LITESTREAM_YML


def test_deploy_scripts_modes_and_rds_abort() -> None:
    assert "AWS_DATABASE_MODE" in DEPLOY_SH
    assert "DATABASE_ENGINE" in DEPLOY_SH
    assert "abort_if_stack_managed_rds" in DEPLOY_SH
    assert "rds_lookup_" not in DEPLOY_SH
    assert "stack-managed RDS vs existing" not in DEPLOY_SH
    assert "Use stack-managed RDS" not in DEPLOY_SH
    assert "ENABLE_KEEP_WARM" in DEPLOY_SH
    assert "MySQL (TiDB / your own host)" in DEPLOY_SH
    assert "SQLite + Litestream to S3" in DEPLOY_SH
    assert "docs/BACKUP_AND_MIGRATION.md" in DEPLOY_SH
    assert "There is no in-place migrate from stack RDS" in DEPLOY_SH
    assert "_gh_push_from_env_file DATABASE_BACKEND" in DEPLOY_SH
    assert "push_github_aws_ci_config" in DEPLOY_SH
    assert "PRIMARY_WORKSPACE" in DEPLOY_SH
    assert "env_file_assignment_value" in DEPLOY_SH
    assert "gh_variable_set_env STAGE_NAME" not in DEPLOY_SH
    assert "gh variable set STAGE_NAME" not in DEPLOY_SH
    assert "gh variable set ENABLE_XRAY" not in DEPLOY_SH
    assert "gh variable set BOOTSTRAP_STACK_NAME" not in DEPLOY_SH
    assert "gh variable set DEPLOY_TARGET" not in DEPLOY_SH
    assert "gh_variable_set_env AWS_DATABASE_MODE" not in DEPLOY_SH
    assert "gh variable set AWS_DATABASE_MODE" not in DEPLOY_SH
    delete_fn = DEPLOY_SH[DEPLOY_SH.find("gh_delete_legacy_database_vars()") : DEPLOY_SH.find("params_to_json")]
    assert "AWS_DATABASE_MODE" not in delete_fn
    assert "DATABASE_ENGINE" not in delete_fn
    assert "DATABASE_ADMIN_USER" in delete_fn

    assert "abort_if_stack_managed_rds" in CI_SAM
    assert "AWS_DATABASE_MODE" in CI_SAM
    assert "DATABASE_ENGINE" in CI_SAM
    assert "DatabaseBackend=" in CI_SAM
    assert "DatabaseMode=" not in CI_SAM
    assert "ExistingDatabaseHost=" not in CI_SAM
    assert "resolve_database_schema" in CI_SAM
    assert 'DATABASE_SCHEMA="${DATABASE_SCHEMA:-syncbot}"' not in CI_SAM
    assert "DeploymentBucketName" in CI_SAM
    assert "AWS_S3_BUCKET is empty and bootstrap" in CI_SAM
    assert "STAGE_NAME must be test or prod" in CI_SAM
    assert "EnableKeepWarm=" in CI_SAM
    assert "ExistingDatabaseAdminUser" not in CI_SAM
    assert "invoke_lambda_migrate.sh" in DEPLOY_SH
    assert "invoke_lambda_migrate.sh" in WORKFLOW
    migrate_helper = (INFRA_AWS / "scripts" / "invoke_lambda_migrate.sh").read_text(encoding="utf-8")
    assert "FunctionError" in migrate_helper
    assert "--cli-read-timeout 180" in migrate_helper

    assert "DatabaseInstanceClass" not in CI_SAM
    assert "VpcCidr" not in CI_SAM
    assert "DATABASE_NETWORK_MODE" not in CI_SAM
    assert "docs/BACKUP_AND_MIGRATION.md" in CI_SAM


def test_deploy_script_requires_aws_session_during_prereqs() -> None:
    matrix_at = DEPLOY_SH.index('prereqs_print_cli_status_matrix "AWS"')
    session_at = DEPLOY_SH.index("ensure_aws_authenticated", matrix_at)
    split_at = DEPLOY_SH.index("Non-interactive fast path", matrix_at)
    assert session_at < split_at
    assert "no active AWS CLI session" in DEPLOY_SH
    assert "prompt_yes_no \"Run 'aws" not in DEPLOY_SH


def test_deploy_aws_workflow_drops_admin_and_passes_mode() -> None:
    assert "DATABASE_BACKEND:" in WORKFLOW
    assert "AWS_DATABASE_MODE" in WORKFLOW
    assert "DATABASE_ENGINE" in WORKFLOW
    assert "vars.DATABASE_BACKEND ||" not in WORKFLOW
    assert "vars.AWS_DATABASE_MODE ||" not in WORKFLOW
    assert "vars.DATABASE_ENGINE ||" not in WORKFLOW
    assert "ENABLE_KEEP_WARM" in WORKFLOW
    assert "DATABASE_ADMIN_USER" not in WORKFLOW
    assert "DATABASE_ADMIN_PASSWORD" not in WORKFLOW
    assert "DATABASE_NETWORK_MODE" not in WORKFLOW
    assert "DATABASE_CREATE_APP_USER" not in WORKFLOW
    assert "DATABASE_USERNAME_PREFIX" not in WORKFLOW


def test_deploy_aws_workflow_hardcodes_stage_and_prefers_prefixed_vars() -> None:
    assert "vars.STAGE_NAME" not in WORKFLOW
    assert "STAGE_NAME: test" in WORKFLOW
    assert "STAGE_NAME: prod" in WORKFLOW
    assert "vars.AWS_STACK_NAME || 'syncbot-test'" in WORKFLOW
    assert "vars.AWS_STACK_NAME || 'syncbot-prod'" in WORKFLOW
    assert "vars.AWS_BOOTSTRAP_STACK_NAME || vars.BOOTSTRAP_STACK_NAME || 'syncbot-bootstrap'" in WORKFLOW
    assert "vars.AWS_ENABLE_XRAY || vars.ENABLE_XRAY || 'false'" in WORKFLOW
    assert "vars.GITHUB_DEPLOY_TARGET || vars.DEPLOY_TARGET" in WORKFLOW
    example = (REPO_ROOT / ".env.deploy.example").read_text(encoding="utf-8")
    assert "AWS_STACK_NAME=" in example
    assert "\nSTACK_NAME=" not in example
    assert "AWS_BOOTSTRAP_STACK_NAME=" in example
    assert "\nBOOTSTRAP_STACK_NAME=" not in example
    assert "AWS_ENABLE_XRAY=" in example
    assert "\nENABLE_XRAY=" not in example
    assert "\nUPDATE_STACK=" not in example
    assert "DEPLOYMENT_S3_BUCKET" not in example
    assert "AWS_ROLE_ARN" not in example
    assert "\nCLOUD_RUN_IMAGE=" not in example


def test_deploy_aws_workflow_uses_ensure_bootstrap() -> None:
    assert WORKFLOW.count("infra/aws/scripts/ensure_bootstrap.sh") == 2
    assert "get-template" not in WORKFLOW
    assert "sha256sum infra/aws/template.bootstrap.yaml" not in WORKFLOW


def test_bootstrap_template_records_content_hash_parameter() -> None:
    assert "TemplateContentSha256:" in BOOTSTRAP
    assert "BootstrapTemplateSha256:" in BOOTSTRAP
    assert "ensure_bootstrap.sh" in DEPLOY_SH
    assert "Deploy bootstrap stack now?" not in DEPLOY_SH


def test_initialize_database_does_not_emit_create_database() -> None:
    assert not hasattr(db_mod, "_ensure_database_exists")
    source = inspect.getsource(db_mod)
    assert "def _ensure_database_exists" not in source
    assert "CREATE DATABASE IF NOT EXISTS" not in source
    init_src = inspect.getsource(db_mod.initialize_database)
    assert "_run_alembic_upgrade" in init_src


def test_bootstrap_sqlite_restore_then_migrate_then_replicate(monkeypatch, tmp_path) -> None:
    import sys
    from unittest.mock import MagicMock

    import handler as h

    monkeypatch.setattr(h, "_DB_PATH", tmp_path / "syncbot.db")
    monkeypatch.setattr(h, "_sqlite_ready", False)
    commands: list[tuple[str, list[str]]] = []

    def fake_run(cmd, check=True, **kwargs):  # noqa: ARG001
        commands.append(("run", list(cmd)))

    def fake_popen(cmd, **kwargs):  # noqa: ARG001
        commands.append(("popen", list(cmd)))
        return MagicMock()

    monkeypatch.setattr(h.subprocess, "run", fake_run)
    monkeypatch.setattr(h.subprocess, "Popen", fake_popen)
    fake_db = MagicMock()
    inits: list[int] = []
    fake_db.initialize_database = lambda: inits.append(1)
    monkeypatch.setitem(sys.modules, "db", fake_db)

    h._bootstrap_sqlite()

    assert commands[0][0] == "run"
    assert "restore" in commands[0][1]
    assert "-if-replica-exists" in commands[0][1]
    assert inits == [1]
    assert commands[1][0] == "popen"
    assert "replicate" in commands[1][1]
    assert h._sqlite_ready is True


def test_handler_without_litestream_bucket_skips_bootstrap(monkeypatch) -> None:
    import sys
    from types import SimpleNamespace

    import handler as h

    monkeypatch.delenv("LITESTREAM_S3_BUCKET", raising=False)
    boot: list[str] = []
    monkeypatch.setattr(h, "_bootstrap_sqlite", lambda: boot.append("yes"))
    monkeypatch.setitem(sys.modules, "app", SimpleNamespace(handler=lambda event, context: {"ok": True}))
    assert h.handler({"action": "ping"}, None) == {"ok": True}
    assert boot == []


def test_update_stack_params_use_previous_when_empty() -> None:
    """Empty override values must not become ParameterValue: \"\" (wipes secrets)."""
    import json
    import subprocess

    script = """
import json, sys
result = []
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    k, _, v = line.partition('=')
    if v == '':
        result.append({'ParameterKey': k, 'UsePreviousValue': True})
    else:
        result.append({'ParameterKey': k, 'ParameterValue': v})
print(json.dumps(result))
"""
    # Mirror the shipped helpers: empty DataEncryptionKey / Slack secrets keep previous.
    for path_name in ("ci_sam_deploy_with_fallback.sh", "deploy.sh"):
        text = (INFRA_AWS / "scripts" / path_name).read_text(encoding="utf-8")
        assert "UsePreviousValue" in text
        assert "if v == '':" in text
    proc = subprocess.run(
        ["python3", "-c", script],
        input="DataEncryptionKey=\nSlackClientSecret=secret\nPrimaryWorkspace=\n",
        text=True,
        capture_output=True,
        check=True,
    )
    params = json.loads(proc.stdout)
    by_key = {p["ParameterKey"]: p for p in params}
    assert by_key["DataEncryptionKey"] == {"ParameterKey": "DataEncryptionKey", "UsePreviousValue": True}
    assert by_key["SlackClientSecret"]["ParameterValue"] == "secret"
    assert by_key["PrimaryWorkspace"]["UsePreviousValue"] is True
