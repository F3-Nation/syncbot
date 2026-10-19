"""Package version is a committed string, not importlib metadata."""

import tomllib
from pathlib import Path

import constants

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_app_version_matches_pyproject():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert constants.__version__ == data["tool"]["poetry"]["version"]
    assert constants.app_version() == constants.__version__
    assert constants.app_version() != "dev"
