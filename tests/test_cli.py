"""Tests for ``river_gang.cli`` (SPED §17.7)."""

from __future__ import annotations

import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from river_gang import cli

# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------


def test_parse_args_default_workflow_path() -> None:
    ns = cli.parse_args([])
    assert ns.workflow_path is None
    assert ns.port is None


def test_parse_args_positional_workflow_path() -> None:
    ns = cli.parse_args(["custom/WORKFLOW.md"])
    assert ns.workflow_path == "custom/WORKFLOW.md"


def test_parse_args_port_flag() -> None:
    ns = cli.parse_args(["--port", "9000"])
    assert ns.port == 9000


def test_parse_args_port_with_workflow_path() -> None:
    ns = cli.parse_args(["wf.md", "--port", "8080"])
    assert ns.workflow_path == "wf.md"
    assert ns.port == 8080


def test_parse_args_port_non_int_raises_systemexit() -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["--port", "abc"])


def test_parse_args_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.parse_args(["--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "river-gang" in captured.out
    assert "workflow_path" in captured.out
    assert "--port" in captured.out


# ---------------------------------------------------------------------------
# main — exit code 0 on valid path
# ---------------------------------------------------------------------------


def test_main_explicit_path_returns_zero(
    tmp_path: Path,
) -> None:
    """Path resolution + start_service hand-off, both succeed."""
    from unittest.mock import patch

    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text("---\n---\nbody\n")

    async def _fake_start(**_kwargs: object) -> int:
        return 0

    with patch("river_gang.cli.start_service", _fake_start):
        rc = cli.main([str(workflow)])
    assert rc == 0


def test_main_default_path_returns_zero_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No positional given → default ./WORKFLOW.md resolved + used."""
    from unittest.mock import patch

    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text("---\n---\nbody\n")
    monkeypatch.chdir(tmp_path)

    async def _fake_start(**_kwargs: object) -> int:
        return 0

    with patch("river_gang.cli.start_service", _fake_start):
        rc = cli.main([])
    assert rc == 0


def test_main_with_port_flag_propagates(
    tmp_path: Path,
) -> None:
    """``--port`` value reaches start_service."""
    from unittest.mock import patch

    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text("---\n---\n")

    captured: dict[str, object] = {}

    async def _fake_start(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    with patch("river_gang.cli.start_service", _fake_start):
        rc = cli.main([str(workflow), "--port", "12345"])
    assert rc == 0
    assert captured["port"] == 12345


# ---------------------------------------------------------------------------
# main — exit code 1 on missing files
# ---------------------------------------------------------------------------


def test_main_explicit_missing_path_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "no-such.md"
    rc = cli.main([str(missing)])
    assert rc == 1
    captured = capsys.readouterr()
    assert "not found" in captured.err.lower()
    assert str(missing) in captured.err


def test_main_default_missing_path_returns_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No positional + no ./WORKFLOW.md in cwd → error mentions default."""
    monkeypatch.chdir(tmp_path)
    rc = cli.main([])
    assert rc == 1
    captured = capsys.readouterr()
    err = captured.err.lower()
    assert "not found" in err
    # Default path mentioned and a hint about passing an explicit path.
    assert "workflow.md" in err
    assert "explicit" in err or "pass" in err or "specify" in err


# ---------------------------------------------------------------------------
# Subprocess smoke — confirms the package is invokable end-to-end.
# ---------------------------------------------------------------------------


def test_subprocess_help_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "river_gang", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "river-gang" in result.stdout
    assert "workflow_path" in result.stdout


def test_subprocess_missing_path_exits_one(tmp_path: Path) -> None:
    bogus = tmp_path / "nope.md"
    result = subprocess.run(
        [sys.executable, "-m", "river_gang", str(bogus)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "not found" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Namespace shape (defensive — guard against silent attr renames)
# ---------------------------------------------------------------------------


def test_namespace_carries_expected_attrs() -> None:
    ns = cli.parse_args(["w.md", "--port", "1"])
    assert isinstance(ns, Namespace)
    assert hasattr(ns, "workflow_path")
    assert hasattr(ns, "port")


# ---------------------------------------------------------------------------
# Default path resolution — absolute vs cwd-relative
# ---------------------------------------------------------------------------


def test_default_path_resolves_against_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``./WORKFLOW.md`` is resolved against cwd at invocation time."""
    from unittest.mock import patch

    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text("---\n---\n")
    monkeypatch.chdir(tmp_path)

    captured: dict[str, object] = {}

    async def _fake_start(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    with patch("river_gang.cli.start_service", _fake_start):
        rc = cli.main([])
    assert rc == 0
    # Resolved absolute path is what the CLI hands to start_service.
    assert captured["workflow_path"] == workflow.resolve()
    # Pacify potential leftover env warnings.
    _ = os.environ.get("DUMMY")
