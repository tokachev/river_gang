"""Codex app-server subprocess wrapper (SPED §10.1).

Async context manager around ``bash -lc <codex.command>``. Reads
newline-delimited JSON frames from stdout (max 10 MiB per line per §10.1),
discards stderr (diagnostic only — never parsed as protocol), and surfaces
process exit / framing failures as :mod:`river_gang.codex.errors` types.

POSIX-only (relies on bash and process groups).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from pathlib import Path
from types import TracebackType
from typing import Any

from river_gang.codex.errors import (
    CodexError,
    CodexNotFound,
    InvalidWorkspaceCwd,
    PortExit,
    ResponseError,
)
from river_gang.workspace.safety import (
    WorkspaceOutsideRoot,
    validate_within_root,
)

logger = logging.getLogger(__name__)

MAX_FRAME_BYTES: int = 10 * 1024 * 1024  # SPED §10.1 RECOMMENDED max line size
_EXIT_NOT_FOUND: int = 127  # bash exit code when the inner command is missing


class CodexProcess:
    """Wraps a launched Codex app-server subprocess."""

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        *,
        max_frame_bytes: int,
        stderr_task: asyncio.Task[None] | None,
    ) -> None:
        self._proc = proc
        self._max_frame_bytes = max_frame_bytes
        self._stderr_task = stderr_task
        self._closed = False

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    async def launch(
        cls,
        command: str,
        *,
        cwd: Path,
        workspace_root: Path,
        max_frame_bytes: int = MAX_FRAME_BYTES,
    ) -> CodexProcess:
        """Spawn ``bash -lc <command>`` with the given ``cwd``.

        Raises:
            InvalidWorkspaceCwd: ``cwd`` is not inside ``workspace_root``.
            CodexNotFound: ``bash`` itself was not found on PATH.
        """
        try:
            resolved_cwd = validate_within_root(workspace_root, cwd)
        except WorkspaceOutsideRoot as exc:
            raise InvalidWorkspaceCwd(str(exc)) from exc

        try:
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-lc",
                command,
                cwd=str(resolved_cwd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=max_frame_bytes,  # StreamReader internal buffer
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise CodexNotFound(f"bash not found: {exc}") from exc

        # Drain stderr in the background; only log at DEBUG.
        stderr_task: asyncio.Task[None] | None = None
        if proc.stderr is not None:
            stderr_task = asyncio.create_task(
                _drain_stderr(proc.stderr), name="codex-stderr-drain"
            )

        return cls(
            proc,
            max_frame_bytes=max_frame_bytes,
            stderr_task=stderr_task,
        )

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> CodexProcess:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # IO
    # ------------------------------------------------------------------

    async def write_frame(self, payload: dict[str, Any]) -> None:
        """Write one JSON-line frame to the subprocess's stdin.

        Serialises ``payload`` as compact JSON, appends a single newline,
        and writes it to ``stdin`` followed by ``drain()`` so the bytes
        are flushed to the subprocess. Mirrors :meth:`read_frame`'s
        framing contract (one JSON object per line).

        Raises:
            PortExit: ``stdin`` is closed or the subprocess has exited.
        """
        if self._proc.stdin is None or self._proc.stdin.is_closing():
            raise PortExit("subprocess stdin is unavailable")
        line = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        self._proc.stdin.write(line)
        try:
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise PortExit(f"subprocess stdin pipe broken: {exc}") from exc

    async def read_frame(self) -> dict[str, Any]:
        """Read one JSON-line frame from stdout.

        Raises:
            ResponseError: line is not valid JSON or exceeds ``max_frame_bytes``.
            CodexNotFound: subprocess exited 127 (bash signalling "command not
                found").
            PortExit: subprocess exited (any other code) before a frame
                arrived, or stdout closed mid-stream.
        """
        if self._proc.stdout is None:
            raise PortExit("subprocess has no stdout pipe")

        # Loop instead of recursing on blank lines — a stream of pure
        # newlines must not blow the Python stack.
        while True:
            try:
                line = await self._proc.stdout.readuntil(b"\n")
            except asyncio.LimitOverrunError as exc:
                raise ResponseError(
                    f"frame exceeds max line size "
                    f"({self._max_frame_bytes} bytes): {exc}"
                ) from exc
            except asyncio.IncompleteReadError as exc:
                # EOF before newline. If the subprocess has terminated,
                # surface CodexNotFound for the bash 127 exit; otherwise
                # PortExit.
                returncode = await self._proc.wait()
                if returncode == _EXIT_NOT_FOUND:
                    raise CodexNotFound(
                        "codex.command not found (bash exit 127)"
                    ) from exc
                raise PortExit(
                    f"subprocess exited mid-frame with code {returncode}",
                    returncode=returncode,
                ) from exc

            # readuntil includes the delimiter — strip the trailing newline.
            if line.endswith(b"\n"):
                line = line[:-1]
            if line == b"":
                # Blank lines in the stream are ignored to be tolerant of
                # well-meaning ``echo``-style hooks emitting trailing newlines.
                continue

            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ResponseError(
                    f"frame is not valid JSON: {exc.msg} "
                    f"(line len={len(line)})"
                ) from exc

            if not isinstance(payload, dict):
                raise ResponseError(
                    f"frame must be a JSON object, got "
                    f"{type(payload).__name__}"
                )
            return payload

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def wait_for_exit(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for the subprocess to exit.

        Returns ``True`` if the process exited within the window (or has
        already exited), ``False`` if the timeout elapsed first. Used by
        :meth:`CodexClient.stop_session` for graceful shutdown.
        """
        if self._proc.returncode is not None:
            return True
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True

        # Close stdin so a well-behaved app-server exits naturally.
        if self._proc.stdin is not None and not self._proc.stdin.is_closing():
            with contextlib.suppress(Exception):
                self._proc.stdin.close()

        # If still running, signal the entire process group; on TimeoutError
        # escalate to SIGKILL of the group. ``launch`` uses
        # ``start_new_session=True`` so children form their own process
        # group — without ``killpg`` any grandchildren spawned by the
        # app-server would leak past ``aclose``.
        if self._proc.returncode is None:
            pgid: int | None
            try:
                pgid = os.getpgid(self._proc.pid)
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pgid = None
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except TimeoutError:
                if pgid is not None:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(pgid, signal.SIGKILL)
                await self._proc.wait()

        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    @property
    def pid(self) -> int:
        """OS pid of the launched ``bash -lc <command>`` child.

        Returns ``0`` when no underlying process is attached (defensive —
        :meth:`launch` always sets ``self._proc``, so this guard only
        matters if someone constructs ``CodexProcess`` by hand).
        """
        proc = getattr(self, "_proc", None)
        if proc is None:
            return 0
        pid = proc.pid
        return pid if pid is not None else 0


async def _drain_stderr(reader: asyncio.StreamReader) -> None:
    """Consume the stderr pipe until EOF, emitting each line at DEBUG."""
    while True:
        try:
            line = await reader.readline()
        except asyncio.LimitOverrunError:
            # An overlong stderr line is benign — discard chunk and continue.
            with contextlib.suppress(Exception):
                await reader.read(1024)
            continue
        if not line:
            return
        logger.debug("codex stderr: %s", line.rstrip(b"\n").decode("utf-8", "replace"))


__all__ = [
    "CodexError",
    "CodexProcess",
    "MAX_FRAME_BYTES",
]
