"""Cloud Run images omit boto3; app import must not require the Lambda adapter."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SYNCBOT_DIR = REPO_ROOT / "syncbot"


def test_app_imports_when_boto3_is_unavailable():
    """Import ``app`` in a subprocess that blocks boto3 (Cloud Run requirements.txt)."""
    script = r"""
import importlib.abc
import importlib.machinery
import os
import sys

class _BlockBoto3(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "boto3" or fullname.startswith("boto3."):
            raise ModuleNotFoundError(fullname)
        return None

sys.meta_path.insert(0, _BlockBoto3())
sys.modules.pop("boto3", None)

os.environ.setdefault("LOCAL_DEVELOPMENT", "true")
os.environ.setdefault("DATABASE_BACKEND", "sqlite")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-0-0")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")
os.environ.setdefault("SLACK_CLIENT_ID", "111.222")
os.environ.setdefault("SLACK_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("SLACK_BOT_SCOPES", "chat:write")
os.environ.setdefault("DATA_ENCRYPTION_KEY", "test-encryption-key-16")

import app as app_module

assert app_module.SlackRequestHandler is None, "expected Lambda adapter import to fail without boto3"
print("ok")
"""
    env = os_environ_with_pythonpath()
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(SYNCBOT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "ok" in result.stdout


def os_environ_with_pythonpath() -> dict[str, str]:
    import os

    env = dict(os.environ)
    extra = str(SYNCBOT_DIR)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = extra if not existing else f"{extra}{os.pathsep}{existing}"
    return env
