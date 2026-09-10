"""Every ``__all__`` name resolves on its package.

A name left in ``__all__`` after its import is dropped still passes ruff and
the suite, but ``package.name`` raises AttributeError at runtime and
``from package import *`` fails outright.
"""

from __future__ import annotations

import importlib

import pytest

PACKAGES = ["builders", "federation", "handlers", "helpers"]


@pytest.mark.parametrize("package_name", PACKAGES)
def test_all_names_are_importable(package_name: str) -> None:
    module = importlib.import_module(package_name)
    declared = getattr(module, "__all__", None)
    assert declared, f"{package_name} declares no __all__"
    missing = [name for name in declared if not hasattr(module, name)]
    assert not missing, f"{package_name}.__all__ names missing from the module: {missing}"


@pytest.mark.parametrize("package_name", PACKAGES)
def test_all_has_no_duplicates(package_name: str) -> None:
    declared = list(getattr(importlib.import_module(package_name), "__all__", []))
    dupes = sorted({name for name in declared if declared.count(name) > 1})
    assert not dupes, f"{package_name}.__all__ lists duplicates: {dupes}"
