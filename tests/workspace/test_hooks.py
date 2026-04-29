"""Tests for :func:`run_hook` and :class:`HookResult` (SPED §15.4)."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from river_gang.workspace.hooks import (
    DEFAULT_LOG_TRUNCATE_BYTES,
    HookResult,
    run_hook,
    truncate_for_log,
)

# All subprocess tests assume POSIX bash availability.
pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win") or shutil.which("bash") is None,
    reason="hooks require POSIX bash",
)


# ---------------------------------------------------------------------------
# HookResult dataclass
# ---------------------------------------------------------------------------


def test_hook_result_skipped_factory() -> None:
    res = HookResult.skipped()
    assert res.is_skipped is True
    assert res.ok is False
    assert res.timed_out is False
    assert res.exit_code is None
    assert res.stdout == b""
    assert res.stderr == b""
    assert res.duration_ms == 0


def test_hook_result_success_is_ok() -> None:
    res = HookResult(
        exit_code=0,
        stdout=b"hi",
        stderr=b"",
        duration_ms=12,
        timed_out=False,
        is_skipped=False,
    )
    assert res.ok is True


def test_hook_result_non_zero_exit_is_not_ok() -> None:
    res = HookResult(
        exit_code=1,
        stdout=b"",
        stderr=b"err",
        duration_ms=10,
        timed_out=False,
        is_skipped=False,
    )
    assert res.ok is False


def test_hook_result_timeout_is_not_ok() -> None:
    res = HookResult.timeout(stdout=b"", stderr=b"", duration_ms=200)
    assert res.timed_out is True
    assert res.ok is False
    assert res.exit_code is None


# ---------------------------------------------------------------------------
# truncate_for_log
# ---------------------------------------------------------------------------


def test_truncate_for_log_under_limit_passes_through() -> None:
    out, was = truncate_for_log(b"hello", limit_bytes=64)
    assert out == "hello"
    assert was is False


def test_truncate_for_log_over_limit_marks_truncated() -> None:
    payload = b"x" * (DEFAULT_LOG_TRUNCATE_BYTES + 100)
    out, was = truncate_for_log(payload)
    assert was is True
    assert out.endswith(" [truncated]")
    assert len(out.encode("utf-8")) <= DEFAULT_LOG_TRUNCATE_BYTES + len(
        " [truncated]"
    )


def test_truncate_for_log_decodes_invalid_utf8_with_replacement() -> None:
    out, was = truncate_for_log(b"\xff\xfe\xfd", limit_bytes=64)
    assert isinstance(out, str)
    assert was is False


def test_truncate_for_log_one_megabyte_keeps_first_n_bytes() -> None:
    payload = b"A" * (1024 * 1024)
    out, was = truncate_for_log(payload, limit_bytes=4096)
    assert was is True
    assert out.startswith("A" * 4096)
    assert out.endswith(" [truncated]")


def test_truncate_for_log_empty_input() -> None:
    out, was = truncate_for_log(b"")
    assert out == ""
    assert was is False


def test_truncate_for_log_default_limit_constant() -> None:
    assert DEFAULT_LOG_TRUNCATE_BYTES == 8192


# ---------------------------------------------------------------------------
# run_hook — skipped / no-op
# ---------------------------------------------------------------------------


async def test_run_hook_none_returns_skipped(tmp_path: Path) -> None:
    res = await run_hook(None, cwd=tmp_path, timeout_ms=1000)
    assert res.is_skipped is True
    assert res.ok is False


async def test_run_hook_empty_string_returns_skipped(tmp_path: Path) -> None:
    res = await run_hook("", cwd=tmp_path, timeout_ms=1000)
    assert res.is_skipped is True


async def test_run_hook_whitespace_only_returns_skipped(tmp_path: Path) -> None:
    res = await run_hook("   \n\t ", cwd=tmp_path, timeout_ms=1000)
    assert res.is_skipped is True


# ---------------------------------------------------------------------------
# run_hook — success / failure / cwd
# ---------------------------------------------------------------------------


async def test_run_hook_success_captures_stdout(tmp_path: Path) -> None:
    res = await run_hook("echo hello", cwd=tmp_path, timeout_ms=5000)
    assert res.ok is True
    assert res.exit_code == 0
    assert b"hello" in res.stdout
    assert res.stderr == b""
    assert res.duration_ms >= 0


async def test_run_hook_failure_returns_non_zero_exit(tmp_path: Path) -> None:
    res = await run_hook("exit 7", cwd=tmp_path, timeout_ms=5000)
    assert res.ok is False
    assert res.exit_code == 7
    assert res.timed_out is False


async def test_run_hook_runs_in_provided_cwd(tmp_path: Path) -> None:
    # script prints pwd; we expect the resolved tmp_path
    res = await run_hook("pwd", cwd=tmp_path, timeout_ms=5000)
    assert res.ok is True
    pwd_seen = res.stdout.decode("utf-8").strip()
    assert Path(pwd_seen).resolve() == tmp_path.resolve()


async def test_run_hook_can_create_files_in_cwd(tmp_path: Path) -> None:
    res = await run_hook("touch marker.txt", cwd=tmp_path, timeout_ms=5000)
    assert res.ok is True
    assert (tmp_path / "marker.txt").exists()


async def test_run_hook_captures_stderr_separately(tmp_path: Path) -> None:
    res = await run_hook(
        "echo out; echo err 1>&2; exit 0",
        cwd=tmp_path,
        timeout_ms=5000,
    )
    assert b"out" in res.stdout
    assert b"err" in res.stderr


# ---------------------------------------------------------------------------
# run_hook — timeout + process-group kill
# ---------------------------------------------------------------------------


async def test_run_hook_timeout_returns_timed_out_result(tmp_path: Path) -> None:
    start = time.monotonic()
    res = await run_hook("sleep 10", cwd=tmp_path, timeout_ms=200)
    elapsed_ms = (time.monotonic() - start) * 1000

    assert res.timed_out is True
    assert res.ok is False
    assert res.exit_code is None
    # well below the 10s sleep — kill happened, no waiting for natural exit
    assert elapsed_ms < 5000


async def test_run_hook_timeout_kills_child_process_group(tmp_path: Path) -> None:
    """Hook script spawns a long-running grandchild. Timeout MUST kill the
    whole process group so the grandchild does not survive (§15.4 requires
    'avoid hanging the orchestrator')."""

    marker = tmp_path / "grandchild.pid"
    # parent writes the grandchild PID to a file, then sleeps; grandchild
    # is a `sleep 30` whose PID we capture before the parent hangs.
    script = (
        f"sleep 30 & echo $! > {marker}; "
        "wait $!"
    )
    res = await run_hook(script, cwd=tmp_path, timeout_ms=300)
    assert res.timed_out is True

    # Give the kernel a beat to deliver SIGTERM/KILL to the group.
    await asyncio.sleep(0.3)

    pid_text = marker.read_text().strip()
    assert pid_text.isdigit()
    grandchild_pid = int(pid_text)

    # PID 0 sends a probe signal (no kill); raises OSError(ESRCH) when the
    # process is gone, which is exactly what we want.
    with pytest.raises(OSError):
        os.kill(grandchild_pid, 0)


async def test_run_hook_completes_before_timeout(tmp_path: Path) -> None:
    res = await run_hook("echo fast", cwd=tmp_path, timeout_ms=5000)
    assert res.ok is True
    assert res.timed_out is False
    # should finish well under the configured timeout
    assert res.duration_ms < 4000


# ---------------------------------------------------------------------------
# run_hook — duration accounting
# ---------------------------------------------------------------------------


async def test_run_hook_duration_ms_is_non_negative(tmp_path: Path) -> None:
    res = await run_hook("true", cwd=tmp_path, timeout_ms=5000)
    assert res.duration_ms >= 0


async def test_run_hook_duration_increases_with_sleep(tmp_path: Path) -> None:
    fast = await run_hook("true", cwd=tmp_path, timeout_ms=5000)
    slow = await run_hook("sleep 0.2", cwd=tmp_path, timeout_ms=5000)
    assert slow.duration_ms >= fast.duration_ms


# ---------------------------------------------------------------------------
# run_hook — invalid timeout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_timeout", [0, -1, -100])
async def test_run_hook_non_positive_timeout_raises(
    tmp_path: Path, bad_timeout: int
) -> None:
    with pytest.raises(ValueError):
        await run_hook("echo x", cwd=tmp_path, timeout_ms=bad_timeout)


# ---------------------------------------------------------------------------
# run_hook — large output truncation pipeline
# ---------------------------------------------------------------------------


async def test_run_hook_one_megabyte_stdout_full_capture_then_log_truncate(
    tmp_path: Path,
) -> None:
    """End-to-end: hook produces 1MB of stdout. ``HookResult`` carries the
    full bytes; ``truncate_for_log`` collapses to the configured limit
    with the ``[truncated]`` marker."""
    res = await run_hook(
        # 1MiB of 'A' followed by a newline
        "head -c 1048576 /dev/zero | tr '\\0' 'A'",
        cwd=tmp_path,
        timeout_ms=10000,
    )
    assert res.ok is True
    assert len(res.stdout) >= 1048576

    text, was = truncate_for_log(res.stdout, limit_bytes=4096)
    assert was is True
    assert text.endswith(" [truncated]")
    assert text.startswith("A" * 4096)
