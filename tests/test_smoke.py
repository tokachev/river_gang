"""Smoke test — package imports and exposes a version string."""

from __future__ import annotations

import river_gang


def test_package_imports() -> None:
    assert river_gang is not None


def test_version_field_present() -> None:
    assert hasattr(river_gang, "__version__")
    assert isinstance(river_gang.__version__, str)
    assert river_gang.__version__  # non-empty
