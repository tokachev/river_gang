"""SPED §17.7 conformance: CLI and Host Lifecycle."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from river_gang import cli

pytestmark = pytest.mark.conformance


_VALID_WORKFLOW = """---
tracker:
  kind: linear
  api_key: lit_secret
  project_slug: river-gang
codex:
  command: codex app-server
---
prompt body
"""

_INVALID_WORKFLOW_NO_API_KEY = """---
tracker:
  kind: linear
  project_slug: river-gang
codex:
  command: codex app-server
---
"""


def test_cli_accepts_positional_workflow_path(tmp_path: Path) -> None:
    """Conformance §17.7: CLI accepts a positional workflow path argument
    (``path-to-WORKFLOW.md``)."""
    ns = cli.parse_args([str(tmp_path / "wf.md")])
    assert ns.workflow_path == str(tmp_path / "wf.md")


def test_cli_uses_workflow_md_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Conformance §17.7: CLI uses ``./WORKFLOW.md`` when no workflow path
    argument is provided."""
    monkeypatch.chdir(tmp_path)
    resolved = cli._resolve_workflow_path(None)
    assert resolved.name == "WORKFLOW.md"
    assert resolved.parent == tmp_path.resolve()


def test_cli_errors_on_nonexistent_explicit_path(tmp_path: Path) -> None:
    """Conformance §17.7: CLI errors on nonexistent explicit workflow
    path."""
    rc = cli.main([str(tmp_path / "no-such.md")])
    assert rc == 1


def test_cli_errors_on_missing_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Conformance §17.7: CLI errors on missing default ``./WORKFLOW.md``."""
    monkeypatch.chdir(tmp_path)
    rc = cli.main([])
    assert rc == 1


def test_cli_surfaces_startup_failure_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Conformance §17.7: CLI surfaces startup failure cleanly."""
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text(_INVALID_WORKFLOW_NO_API_KEY)

    rc = cli.main([str(workflow)])
    assert rc == 1
    captured = capsys.readouterr()
    # Validation error reported via logging (caplog/stderr); stdout/stderr
    # contain the operator-visible summary.
    combined = captured.err + captured.out
    assert "tracker.api_key" in combined or "api_key" in combined


def test_cli_exits_zero_when_application_shuts_down_normally(
    tmp_path: Path,
) -> None:
    """Conformance §17.7: CLI exits with success when application starts
    and shuts down normally."""
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text(_VALID_WORKFLOW)

    async def _fake_start(**_kwargs: object) -> int:
        return 0

    with patch("river_gang.cli.start_service", _fake_start):
        rc = cli.main([str(workflow)])
    assert rc == 0


def test_cli_exits_nonzero_on_startup_or_abnormal_failure(
    tmp_path: Path,
) -> None:
    """Conformance §17.7: CLI exits nonzero when startup fails or the
    host process exits abnormally."""
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text(_VALID_WORKFLOW)

    async def _fake_start_fail(**_kwargs: object) -> int:
        return 1

    with patch("river_gang.cli.start_service", _fake_start_fail):
        rc = cli.main([str(workflow)])
    assert rc == 1

    async def _fake_start_raise(**_kwargs: object) -> int:
        raise RuntimeError("kaboom")

    with patch("river_gang.cli.start_service", _fake_start_raise):
        rc = cli.main([str(workflow)])
    assert rc == 2  # top-level fault barrier


# ---------------------------------------------------------------------------
# End-to-end subprocess smoke (POSIX-only) — bounded SIGINT shutdown.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="SIGINT subprocess test is POSIX-only")
def test_subprocess_sigint_bounded_exit(tmp_path: Path) -> None:
    """Conformance §17.7 (e2e): SIGINT yields a bounded exit (0/1/2),
    never a hang."""
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text(_VALID_WORKFLOW)

    proc = subprocess.Popen(
        [sys.executable, "-m", "river_gang", str(workflow)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        time.sleep(0.5)
        proc.send_signal(signal.SIGINT)
        try:
            stdout, stderr = proc.communicate(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            raise
        assert proc.returncode in (0, 1, 2), (
            f"unexpected exit {proc.returncode}: "
            f"stdout={stdout!r} stderr={stderr!r}"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2.0)
