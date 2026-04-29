"""Tests for :class:`CodexProcess` (SPED §10.1, §10.6)."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from river_gang.codex.errors import (
    CodexError,
    CodexNotFound,
    InvalidWorkspaceCwd,
    PortExit,
    ResponseError,
)
from river_gang.codex.process import MAX_FRAME_BYTES, CodexProcess
from tests.codex.fakes import FakeCodexProcess

# All real-subprocess tests assume POSIX bash availability.
posix_only = pytest.mark.skipif(
    sys.platform.startswith("win") or shutil.which("bash") is None,
    reason="codex.process requires POSIX bash",
)


# ---------------------------------------------------------------------------
# Errors module shape
# ---------------------------------------------------------------------------


def test_codex_error_hierarchy_is_complete() -> None:
    """SPED §10.6 mandates these normalized categories — fail loud if any
    subclass is missing so a later task cannot silently regress the surface.
    """
    from river_gang.codex import errors as e

    expected = {
        "CodexNotFound",
        "InvalidWorkspaceCwd",
        "ResponseTimeout",
        "TurnTimeout",
        "PortExit",
        "ResponseError",
        "TurnFailed",
        "TurnCancelled",
        "TurnInputRequired",
    }
    for name in expected:
        cls = getattr(e, name)
        assert issubclass(cls, e.CodexError), f"{name} not subclass of CodexError"


def test_max_frame_bytes_is_10mb() -> None:
    assert MAX_FRAME_BYTES == 10 * 1024 * 1024


# ---------------------------------------------------------------------------
# cwd validation
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform.startswith("win") or shutil.which("bash") is None,
    reason="bash required to even reach the cwd check past launch",
)
async def test_launch_rejects_cwd_outside_workspace_root(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    with pytest.raises(InvalidWorkspaceCwd):
        await CodexProcess.launch(
            "echo hi",
            cwd=outside,
            workspace_root=root,
        )


@pytest.mark.skipif(
    sys.platform.startswith("win") or shutil.which("bash") is None,
    reason="bash required",
)
async def test_launch_rejects_absolute_cwd_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()

    with pytest.raises(InvalidWorkspaceCwd):
        await CodexProcess.launch(
            "echo hi",
            cwd=Path("/etc"),
            workspace_root=root,
        )


# ---------------------------------------------------------------------------
# Real-subprocess smoke (POSIX bash)
# ---------------------------------------------------------------------------


@posix_only
async def test_launch_reads_two_frames_from_real_bash(tmp_path: Path) -> None:
    proc = await CodexProcess.launch(
        # printf is more reliable than echo for embedded JSON
        r'printf %s\\n "{\"x\":1}" "{\"x\":2}"',
        cwd=tmp_path,
        workspace_root=tmp_path,
    )
    try:
        f1 = await proc.read_frame()
        f2 = await proc.read_frame()
    finally:
        await proc.aclose()

    assert f1 == {"x": 1}
    assert f2 == {"x": 2}


@posix_only
async def test_launch_async_context_manager_cleans_up(tmp_path: Path) -> None:
    async with await CodexProcess.launch(
        r'printf %s\\n "{\"a\":1}"',
        cwd=tmp_path,
        workspace_root=tmp_path,
    ) as proc:
        frame = await proc.read_frame()
    assert frame == {"a": 1}
    # after __aexit__, returncode must be set
    assert proc.returncode is not None


@posix_only
async def test_read_frame_after_eof_raises_port_exit(tmp_path: Path) -> None:
    """Process emits one frame then exits cleanly. The next ``read_frame``
    must surface :class:`PortExit` (no silent ``None``)."""
    proc = await CodexProcess.launch(
        r'printf %s\\n "{\"a\":1}"',
        cwd=tmp_path,
        workspace_root=tmp_path,
    )
    try:
        await proc.read_frame()
        with pytest.raises(PortExit):
            await proc.read_frame()
    finally:
        await proc.aclose()


@posix_only
async def test_subprocess_exit_mid_stream_raises_port_exit(tmp_path: Path) -> None:
    """Process exits without emitting any frame at all → first ``read_frame``
    surfaces :class:`PortExit` rather than hanging."""
    proc = await CodexProcess.launch(
        "exit 0",
        cwd=tmp_path,
        workspace_root=tmp_path,
    )
    try:
        with pytest.raises(PortExit):
            await proc.read_frame()
    finally:
        await proc.aclose()


@posix_only
async def test_subprocess_nonzero_exit_surfaces_in_port_exit(
    tmp_path: Path,
) -> None:
    proc = await CodexProcess.launch(
        "exit 42",
        cwd=tmp_path,
        workspace_root=tmp_path,
    )
    try:
        with pytest.raises(PortExit):
            await proc.read_frame()
    finally:
        await proc.aclose()
    assert proc.returncode == 42


@posix_only
async def test_read_frame_skips_many_blank_lines_without_recursion(
    tmp_path: Path,
) -> None:
    """Stream with 5000 blank lines before a frame must not blow the stack.

    Regression test for the prior :meth:`read_frame` implementation that
    recursed on each blank line — at this volume the recursive form would
    raise ``RecursionError`` (default sys.recursion limit is 1000).
    """
    blank_count = 5000
    cmd = (
        f"yes '' | head -n {blank_count}; "
        r'printf %s\\n "{\"after\":\"blanks\"}"'
    )
    proc = await CodexProcess.launch(cmd, cwd=tmp_path, workspace_root=tmp_path)
    try:
        frame = await proc.read_frame()
    finally:
        await proc.aclose()
    assert frame == {"after": "blanks"}


@posix_only
async def test_invalid_json_line_raises_response_error(tmp_path: Path) -> None:
    proc = await CodexProcess.launch(
        r'printf %s\\n "not-json"',
        cwd=tmp_path,
        workspace_root=tmp_path,
    )
    try:
        with pytest.raises(ResponseError):
            await proc.read_frame()
    finally:
        await proc.aclose()


@posix_only
async def test_oversized_frame_raises_response_error(tmp_path: Path) -> None:
    """A line that exceeds 10 MiB before the newline must raise
    :class:`ResponseError` per §10.1 max-line-size guidance.

    We use a configurable cap injected into ``CodexProcess`` so we can
    assert the behaviour without actually streaming 10 MiB of bytes.
    """
    proc = await CodexProcess.launch(
        # 200 KiB of A's followed by newline, no closing brace -> can't decode,
        # but more importantly exceeds the 100 KiB cap we override below.
        "head -c 204800 /dev/zero | tr '\\0' 'A'; printf '\\n'",
        cwd=tmp_path,
        workspace_root=tmp_path,
        max_frame_bytes=100 * 1024,
    )
    try:
        with pytest.raises(ResponseError):
            await proc.read_frame()
    finally:
        await proc.aclose()


@posix_only
async def test_stderr_does_not_corrupt_protocol_stream(tmp_path: Path) -> None:
    """stderr noise must not be parsed as protocol frames."""
    proc = await CodexProcess.launch(
        r'echo "noise on stderr" 1>&2; printf %s\\n "{\"a\":1}"',
        cwd=tmp_path,
        workspace_root=tmp_path,
    )
    try:
        frame = await proc.read_frame()
    finally:
        await proc.aclose()
    assert frame == {"a": 1}


@posix_only
async def test_command_not_found_raises_codex_not_found(tmp_path: Path) -> None:
    """An unknown command run through ``bash -lc`` exits with code 127.
    We classify that as :class:`CodexNotFound`, not a generic ``PortExit``,
    so operators see the actionable error."""
    proc = await CodexProcess.launch(
        "definitely_not_a_real_command_xyz_42",
        cwd=tmp_path,
        workspace_root=tmp_path,
    )
    try:
        with pytest.raises(CodexNotFound):
            await proc.read_frame()
    finally:
        await proc.aclose()


@posix_only
async def test_launch_respects_provided_cwd(tmp_path: Path) -> None:
    """The launched shell sees the workspace dir as its cwd."""
    sub = tmp_path / "ws"
    sub.mkdir()
    proc = await CodexProcess.launch(
        r'printf %s\\n "{\"pwd\":\"$PWD\"}"',
        cwd=sub,
        workspace_root=tmp_path,
    )
    try:
        frame = await proc.read_frame()
    finally:
        await proc.aclose()
    assert Path(frame["pwd"]).resolve() == sub.resolve()


# ---------------------------------------------------------------------------
# aclose() must SIGTERM the whole process group so grandchildren don't leak.
# ---------------------------------------------------------------------------


@posix_only
async def test_aclose_kills_child_subprocess_in_process_group(
    tmp_path: Path,
) -> None:
    """``CodexProcess.launch`` uses ``start_new_session=True`` so children
    form their own process group. ``aclose()`` MUST SIGTERM the group so
    a sleep grandchild dies with the parent.
    """
    import asyncio
    import os
    import signal

    # Bash launches a background ``sleep 60`` and prints its pid as JSON,
    # then waits forever. ``aclose()`` should kill the bash session AND
    # the sleep grandchild. We probe the grandchild with signal 0 to
    # check liveness without raising on a zombie.
    script = (
        r'sleep 60 & '
        r'CHILD_PID=$!; '
        r'printf %s\\n "{\"child\":$CHILD_PID}"; '
        r'wait'
    )
    proc = await CodexProcess.launch(
        script,
        cwd=tmp_path,
        workspace_root=tmp_path,
    )
    frame = await proc.read_frame()
    child_pid = int(frame["child"])

    # Sanity: grandchild is alive before aclose().
    os.kill(child_pid, 0)

    await proc.aclose()

    import contextlib

    # After aclose() the grandchild MUST be dead. Kernel may take a moment
    # to reap, so retry briefly.
    for _ in range(50):
        try:
            os.kill(child_pid, 0)
        except (ProcessLookupError, PermissionError):
            break
        await asyncio.sleep(0.02)
    else:  # pragma: no cover - regression failure path
        # Last-ditch cleanup so a regression doesn't leak a sleep.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(child_pid, signal.SIGKILL)
        raise AssertionError(
            f"grandchild pid {child_pid} survived CodexProcess.aclose() — "
            "process-group kill regressed"
        )


# ---------------------------------------------------------------------------
# FakeCodexProcess
# ---------------------------------------------------------------------------


async def test_fake_returns_queued_frames_in_order() -> None:
    fake = FakeCodexProcess([{"n": 1}, {"n": 2}, {"n": 3}])
    async with fake:
        assert await fake.read_frame() == {"n": 1}
        assert await fake.read_frame() == {"n": 2}
        assert await fake.read_frame() == {"n": 3}
    assert fake.frames_consumed == 3
    assert fake.closed is True


async def test_fake_read_frame_after_exhaustion_raises_port_exit() -> None:
    fake = FakeCodexProcess([{"n": 1}])
    async with fake:
        await fake.read_frame()
        with pytest.raises(PortExit):
            await fake.read_frame()


async def test_fake_terminal_error_overrides_eof() -> None:
    err = ResponseError("boom")
    fake = FakeCodexProcess(terminal_error=err)
    async with fake:
        with pytest.raises(ResponseError):
            await fake.read_frame()


async def test_fake_records_written_frames() -> None:
    fake = FakeCodexProcess()
    async with fake:
        await fake.write_frame({"out": 1})
        await fake.write_frame({"out": 2})
    assert fake.written_frames == [{"out": 1}, {"out": 2}]


async def test_fake_queue_appends_after_construction() -> None:
    fake = FakeCodexProcess()
    fake.queue({"a": 1}, {"a": 2})
    async with fake:
        assert await fake.read_frame() == {"a": 1}
        assert await fake.read_frame() == {"a": 2}


def test_fake_returncode_none_until_closed() -> None:
    fake = FakeCodexProcess(exit_code=7)
    assert fake.returncode is None


async def test_fake_returncode_after_close() -> None:
    fake = FakeCodexProcess(exit_code=7)
    async with fake:
        pass
    assert fake.returncode == 7


# ---------------------------------------------------------------------------
# Misc invariants
# ---------------------------------------------------------------------------


def test_codex_error_base_is_exception() -> None:
    assert issubclass(CodexError, Exception)
