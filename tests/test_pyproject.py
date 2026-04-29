"""Asserts on packaging metadata declared in ``pyproject.toml``."""

from __future__ import annotations

import tomllib
from importlib.metadata import version as metadata_version
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _load() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def test_project_name_and_distribution() -> None:
    data = _load()
    project = data["project"]
    # Distribution name uses a dash; import name uses underscore.
    assert project["name"] == "river-gang"
    # Wheel must ship the import-name package directory.
    wheel_packages = data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert "src/river_gang" in wheel_packages


def test_version_present() -> None:
    data = _load()
    declared = data["project"]["version"]
    assert isinstance(declared, str)
    assert declared
    # Version installed in the active environment must match pyproject.
    assert metadata_version("river-gang") == declared


def test_requires_python() -> None:
    data = _load()
    assert data["project"]["requires-python"] == ">=3.11"


def test_console_script_registered() -> None:
    data = _load()
    scripts = data["project"]["scripts"]
    assert scripts.get("river-gang") == "river_gang.cli:main"
