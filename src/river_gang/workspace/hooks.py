"""Workspace hook runner (SPED §9.3, §15.4).

POSIX-only.

Hooks are arbitrary shell scripts loaded from ``WORKFLOW.md``. We run them
through ``bash -lc`` in the per-issue workspace directory and enforce a
hard timeout. On timeout the entire process group is signalled with
``SIGTERM`` and, after a short grace window, ``SIGKILL`` so a hook that
spawned grandchildren cannot survive and hang the orchestrator (§15.4
"Hook timeouts are REQUIRED").

Captured stdout/stderr are returned in full inside :class:`HookResult` so
callers can assert behaviour in tests; for log emission use
:func:`truncate_for_log` to apply the §15.4 "Hook output SHOULD be
truncated in logs" guidance.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

if sys.platform.startswith("win"):  # pragma: no cover - import-time guard
    raise ImportError("river_gang.workspace.hooks requires POSIX (no win32)")


DEFAULT_LOG_TRUNCATE_BYTES: int = 8192
TRUNCATION_MARKER: str = " [truncated]"
_KILL_GRACE_SECONDS: float = 2.0


@dataclass(frozen=True)
class HookResult:
    """Outcome of a single hook invocation.

    ``stdout``/``stderr`` carry the full captured bytes; use
    :func:`truncate_for_log` for log-safe rendering.

    Note on naming: the boolean field is :attr:`is_skipped`, not
    ``skipped``. The classmethods :meth:`HookResult.skipped` and
    :meth:`HookResult.timeout` are factory constructors and would
    otherwise shadow same-named instance fields under the type checker.
    """

    exit_code: int | None
    stdout: bytes
    stderr: bytes
    duration_ms: int
    timed_out: bool
    is_skipped: bool

    @property
    def ok(self) -> bool:
        return (
            not self.is_skipped
            and not self.timed_out
            and self.exit_code == 0
        )

    @classmethod
    def skipped(cls) -> HookResult:
        return cls(
            exit_code=None,
            stdout=b"",
            stderr=b"",
            duration_ms=0,
            timed_out=False,
            is_skipped=True,
        )

    @classmethod
    def timeout(
        cls, *, stdout: bytes, stderr: bytes, duration_ms: int
    ) -> HookResult:
        return cls(
            exit_code=None,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=True,
            is_skipped=False,
        )


def truncate_for_log(
    data: bytes, *, limit_bytes: int = DEFAULT_LOG_TRUNCATE_BYTES
) -> tuple[str, bool]:
    """Render ``data`` as a UTF-8 string, truncating at ``limit_bytes``.

    Returns ``(text, was_truncated)``. Invalid UTF-8 is replaced rather
    than raised — the goal is best-effort observability, not lossless
    decoding.
    """
    if len(data) <= limit_bytes:
        return data.decode("utf-8", errors="replace"), False
    head = data[:limit_bytes].decode("utf-8", errors="replace")
    return head + TRUNCATION_MARKER, True


async def run_hook(
    script: str | None,
    *,
    cwd: Path,
    timeout_ms: int,
) -> HookResult:
    """Execute ``script`` via ``bash -lc`` inside ``cwd``.

    Returns :meth:`HookResult.skipped` when ``script`` is ``None`` or
    contains only whitespace.

    Raises:
        ValueError: ``timeout_ms`` is non-positive.
    """
    if timeout_ms <= 0:
        raise ValueError(
            f"timeout_ms must be a positive integer, got {timeout_ms}"
        )
    if script is None or script.strip() == "":
        return HookResult.skipped()

    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        "bash",
        "-lc",
        script,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # ``start_new_session=True`` puts the child in its own process group
        # so we can ``killpg`` it (and any descendants) on timeout.
        start_new_session=True,
    )

    timeout_seconds = timeout_ms / 1000.0
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds
        )
    except TimeoutError:
        stdout_bytes, stderr_bytes = await _kill_process_group(proc)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return HookResult.timeout(
            stdout=stdout_bytes,
            stderr=stderr_bytes,
            duration_ms=elapsed_ms,
        )

    elapsed_ms = int((time.monotonic() - start) * 1000)
    return HookResult(
        exit_code=proc.returncode,
        stdout=stdout or b"",
        stderr=stderr or b"",
        duration_ms=elapsed_ms,
        timed_out=False,
        is_skipped=False,
    )


async def _kill_process_group(
    proc: asyncio.subprocess.Process,
) -> tuple[bytes, bytes]:
    """SIGTERM → grace → SIGKILL the child's process group.

    Returns whatever stdout/stderr we captured before the signal so the
    log can still show partial output.
    """
    pid = proc.pid

    # First polite signal to the entire group.
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        # Process already exited or signal denied — nothing to escalate.
        pgid = None

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_KILL_GRACE_SECONDS
        )
        return stdout or b"", stderr or b""
    except TimeoutError:
        pass

    # Escalation: SIGKILL the group, then drain pipes without a deadline.
    if pgid is not None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)

    try:
        stdout, stderr = await proc.communicate()
        return stdout or b"", stderr or b""
    except Exception:  # noqa: BLE001 -- partial output is best-effort
        return b"", b""
