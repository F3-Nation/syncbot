"""Behavioral contract for infra/aws/scripts/resolve_database_backend.sh aliases."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVER = REPO_ROOT / "infra" / "aws" / "scripts" / "resolve_database_backend.sh"

_UNSET = (
    "DATABASE_BACKEND",
    "AWS_DATABASE_MODE",
    "GCP_DATABASE_MODE",
    "DATABASE_ENGINE",
    "EXISTING_DATABASE_HOST",
    "EXISTING_DATABASE_PORT",
    "DATABASE_HOST",
    "DATABASE_PORT",
    "DATABASE_USER",
    "DATABASE_PASSWORD",
    "GITHUB_ACTIONS",
)


def _bash(provider: str, env: dict[str, str] | None = None, extra: str = "") -> subprocess.CompletedProcess[str]:
    exports = "\n".join(f"export {k}={shlex.quote(v)}" for k, v in (env or {}).items())
    script = f"""
set -euo pipefail
{exports}
# shellcheck source=/dev/null
source {shlex.quote(str(RESOLVER))}
resolve_database_backend {shlex.quote(provider)}
{extra}
printf 'BACKEND=%s\\n' "$DATABASE_BACKEND"
printf 'HOST=%s\\n' "${{DATABASE_HOST:-}}"
printf 'PORT=%s\\n' "${{DATABASE_PORT:-}}"
"""
    clean_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
    }
    for key in _UNSET:
        clean_env.pop(key, None)
    if env:
        clean_env.update(env)
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=clean_env,
        check=False,
    )


def _backend(result: subprocess.CompletedProcess[str]) -> str:
    for line in result.stdout.splitlines():
        if line.startswith("BACKEND="):
            return line.split("=", 1)[1]
    return ""


def _host(result: subprocess.CompletedProcess[str]) -> str:
    for line in result.stdout.splitlines():
        if line.startswith("HOST="):
            return line.split("=", 1)[1]
    return ""


def test_canonical_database_backend_wins_over_aliases() -> None:
    result = _bash(
        "aws",
        {"DATABASE_BACKEND": "sqlite", "AWS_DATABASE_MODE": "existing", "DATABASE_ENGINE": "mysql"},
    )
    assert result.returncode == 0, result.stderr
    assert _backend(result) == "sqlite"
    assert "ignored because DATABASE_BACKEND=sqlite is set" in result.stderr
    assert "Warning:" in result.stderr


def test_canonical_only_has_no_deprecation_warning() -> None:
    result = _bash("aws", {"DATABASE_BACKEND": "mysql"})
    assert result.returncode == 0, result.stderr
    assert _backend(result) == "mysql"
    assert result.stderr == ""


def test_aws_database_mode_sqlite_maps() -> None:
    result = _bash("aws", {"AWS_DATABASE_MODE": "sqlite"})
    assert result.returncode == 0, result.stderr
    assert _backend(result) == "sqlite"
    assert "AWS_DATABASE_MODE=sqlite is deprecated" in result.stderr
    assert "DATABASE_BACKEND=sqlite" in result.stderr
    assert result.returncode == 0


def test_database_engine_sqlite_maps() -> None:
    result = _bash("aws", {"DATABASE_ENGINE": "sqlite"})
    assert result.returncode == 0, result.stderr
    assert _backend(result) == "sqlite"
    assert "DATABASE_ENGINE=sqlite is deprecated" in result.stderr


def test_gcp_database_mode_sqlite_maps() -> None:
    result = _bash("gcp", {"GCP_DATABASE_MODE": "sqlite"})
    assert result.returncode == 0, result.stderr
    assert _backend(result) == "sqlite"
    assert "GCP_DATABASE_MODE=sqlite is deprecated" in result.stderr


def test_existing_plus_postgresql_engine_maps() -> None:
    result = _bash("aws", {"AWS_DATABASE_MODE": "existing", "DATABASE_ENGINE": "postgresql"})
    assert result.returncode == 0, result.stderr
    assert _backend(result) == "postgresql"
    assert "AWS_DATABASE_MODE=existing is deprecated" in result.stderr
    assert "DATABASE_BACKEND=postgresql" in result.stderr


def test_existing_without_engine_defaults_mysql() -> None:
    result = _bash("aws", {"AWS_DATABASE_MODE": "existing"})
    assert result.returncode == 0, result.stderr
    assert _backend(result) == "mysql"
    assert "DATABASE_BACKEND=mysql" in result.stderr


def test_gcp_existing_defaults_mysql() -> None:
    result = _bash("gcp", {"GCP_DATABASE_MODE": "existing"})
    assert result.returncode == 0, result.stderr
    assert _backend(result) == "mysql"


def test_aws_nothing_set_defaults_mysql() -> None:
    result = _bash("aws")
    assert result.returncode == 0, result.stderr
    assert _backend(result) == "mysql"
    assert result.stderr == ""


def test_gcp_nothing_set_defaults_sqlite() -> None:
    result = _bash("gcp")
    assert result.returncode == 0, result.stderr
    assert _backend(result) == "sqlite"
    assert result.stderr == ""


def test_rejects_unknown_backend_after_mapping() -> None:
    result = _bash("aws", {"DATABASE_BACKEND": "oracle"})
    assert result.returncode != 0
    assert "must be mysql, postgresql, or sqlite" in result.stderr


def test_rejects_unknown_aws_database_mode() -> None:
    result = _bash("aws", {"AWS_DATABASE_MODE": "rds"})
    assert result.returncode != 0
    assert "AWS_DATABASE_MODE must be sqlite or existing" in result.stderr


def test_existing_database_host_fills_database_host() -> None:
    result = _bash(
        "aws",
        {"EXISTING_DATABASE_HOST": "db.example.com", "EXISTING_DATABASE_PORT": "4000"},
    )
    assert result.returncode == 0, result.stderr
    assert _backend(result) == "mysql"
    assert _host(result) == "db.example.com"
    assert "HOST=db.example.com" in result.stdout
    assert "PORT=4000" in result.stdout
    assert "EXISTING_DATABASE_HOST is deprecated" in result.stderr
    assert "DATABASE_HOST" in result.stderr


def test_github_actions_emits_workflow_warning() -> None:
    result = _bash("aws", {"AWS_DATABASE_MODE": "sqlite", "GITHUB_ACTIONS": "true"})
    assert result.returncode == 0, result.stderr
    assert "::warning::" in result.stderr
    assert "DATABASE_BACKEND=sqlite" in result.stderr


def test_require_credentials_sqlite_ok_without_host() -> None:
    result = _bash(
        "aws",
        {"DATABASE_BACKEND": "sqlite"},
        extra="require_database_credentials_for_backend",
    )
    assert result.returncode == 0, result.stderr
    assert _host(result) == ""


def test_require_credentials_mysql_needs_host_user_password() -> None:
    result = _bash(
        "aws",
        {"DATABASE_BACKEND": "mysql"},
        extra="require_database_credentials_for_backend",
    )
    assert result.returncode != 0
    assert "DATABASE_HOST is required" in result.stderr


def _source(extra: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    exports = "\n".join(f"export {k}={shlex.quote(v)}" for k, v in (env or {}).items())
    script = f"""
set -euo pipefail
{exports}
# shellcheck source=/dev/null
source {shlex.quote(str(RESOLVER))}
{extra}
"""
    clean_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
    }
    if env:
        clean_env.update(env)
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=clean_env,
        check=False,
    )


def test_aws_provider_alias_coalesce_warns_when_only_legacy_is_set() -> None:
    result = _source(
        "apply_aws_provider_env_aliases; printf 'STACK=%s\\n' \"$AWS_STACK_NAME\"",
        {"STACK_NAME": "legacy-stack"},
    )
    assert result.returncode == 0, result.stderr
    assert "STACK=legacy-stack" in result.stdout
    assert "STACK_NAME is deprecated; set AWS_STACK_NAME instead." in result.stderr


def test_aws_provider_alias_keeps_canonical_when_both_set() -> None:
    result = _source(
        "apply_aws_provider_env_aliases; printf 'STACK=%s\\n' \"$AWS_STACK_NAME\"",
        {"AWS_STACK_NAME": "canonical-stack", "STACK_NAME": "legacy-stack"},
    )
    assert result.returncode == 0, result.stderr
    assert "STACK=canonical-stack" in result.stdout
    assert "STACK_NAME is deprecated" not in result.stderr


def test_gcp_provider_alias_coalesce_cloud_run_image() -> None:
    result = _source(
        "apply_gcp_provider_env_aliases; printf 'IMAGE=%s\\n' \"$GCP_CLOUD_RUN_IMAGE\"",
        {"CLOUD_RUN_IMAGE": "gcr.io/example/syncbot:legacy"},
    )
    assert result.returncode == 0, result.stderr
    assert "IMAGE=gcr.io/example/syncbot:legacy" in result.stdout
    assert "CLOUD_RUN_IMAGE is deprecated; set GCP_CLOUD_RUN_IMAGE instead." in result.stderr


def test_env_file_assignment_value_skips_comments_and_empty(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.deploy.test"
    env_file.write_text(
        '# PRIMARY_WORKSPACE=commented\nSLACK_SIGNING_SECRET=\nENABLE_KEEP_WARM=true\nPRIMARY_WORKSPACE="T123"\n',
        encoding="utf-8",
    )
    quoted = shlex.quote(str(env_file))
    result = _source(
        f"""
val="$(env_file_assignment_value ENABLE_KEEP_WARM {quoted})"
printf 'WARM=%s\\n' "$val"
val="$(env_file_assignment_value PRIMARY_WORKSPACE {quoted})"
printf 'PRIMARY=%s\\n' "$val"
if env_file_assignment_value SLACK_SIGNING_SECRET {quoted}; then
  echo EMPTY_HIT
else
  echo EMPTY_MISS
fi
"""
    )
    assert result.returncode == 0, result.stderr
    assert "WARM=true" in result.stdout
    assert "PRIMARY=T123" in result.stdout
    assert "EMPTY_MISS" in result.stdout


def test_resolve_database_schema_keeps_explicit_value() -> None:
    result = _source(
        'resolve_database_schema stack us-east-1 test; printf "SCHEMA=%s\\n" "$DATABASE_SCHEMA"',
        {"DATABASE_SCHEMA": "keep-me"},
    )
    assert result.returncode == 0, result.stderr
    assert "SCHEMA=keep-me" in result.stdout


def test_resolve_database_schema_reuses_live_stack_then_infers(tmp_path: Path) -> None:
    aws_bin = tmp_path / "aws"
    aws_bin.write_text("#!/bin/sh\necho live_schema\n", encoding="utf-8")
    aws_bin.chmod(0o755)
    extra_path = f"{tmp_path}:{os.environ.get('PATH', '/usr/bin:/bin')}"
    result = _source(
        f"export PATH={shlex.quote(extra_path)}\n"
        "unset DATABASE_SCHEMA\n"
        "resolve_database_schema stack us-east-1 test\n"
        'printf "SCHEMA=%s\\n" "$DATABASE_SCHEMA"\n',
    )
    assert result.returncode == 0, result.stderr
    assert "SCHEMA=live_schema" in result.stdout

    aws_bin.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    result = _source(
        f"export PATH={shlex.quote(extra_path)}\n"
        "unset DATABASE_SCHEMA\n"
        "resolve_database_schema stack us-east-1 prod\n"
        'printf "SCHEMA=%s\\n" "$DATABASE_SCHEMA"\n',
    )
    assert result.returncode == 0, result.stderr
    assert "SCHEMA=syncbot_prod" in result.stdout
