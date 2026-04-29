"""Tests for the ``python -m river_gang`` and ``cli.main`` entry points."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from textwrap import dedent

from river_gang import cli


def test_python_m_river_gang_help_exits_zero() -> None:
    """``python -m river_gang --help`` prints usage and exits 0 (Task 47)."""

    proc = subprocess.run(
        [sys.executable, "-m", "river_gang", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "river-gang" in proc.stdout
    # Usage banner mentions both arguments.
    assert "workflow_path" in proc.stdout
    assert "--port" in proc.stdout


def test_cli_main_returns_int_exit_code(tmp_path: Path) -> None:
    """``river_gang.cli.main`` is a real entry point as of Task 47.

    Path-resolution success branch hands off to ``start_service`` (Task 48).
    Patch the integration to keep the test hermetic.
    """
    from unittest.mock import patch

    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text("---\n---\n")

    async def _fake_start(**_kwargs: object) -> int:
        return 0

    with patch("river_gang.cli.start_service", _fake_start):
        rc = cli.main([str(workflow)])
    assert rc == 0


def test_init_falls_back_when_metadata_missing(tmp_path: Path) -> None:
    """``__init__.py`` defends editable installs that briefly lack metadata.

    Subprocess-based: monkeypatch ``importlib.metadata.version`` BEFORE the
    first ``import river_gang`` so the fallback branch executes during the
    initial module load. Avoids ``importlib.reload`` semantics that break when
    other modules already hold references to ``river_gang`` symbols.
    """

    helper = tmp_path / "fallback_probe.py"
    helper.write_text(
        dedent(
            """
            import importlib.metadata

            def _raise(*_args, **_kwargs):
                raise importlib.metadata.PackageNotFoundError("river-gang")

            importlib.metadata.version = _raise

            import river_gang

            print(river_gang.__version__)
            """
        ).lstrip()
    )

    proc = subprocess.run(
        [sys.executable, str(helper)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "0.0.0"
