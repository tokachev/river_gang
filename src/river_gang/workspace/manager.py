"""Workspace lifecycle (SPED §9.2, §9.3, §9.4).

:class:`WorkspaceManager` owns:

- ``ensure_for_issue`` — idempotent create/reuse of the per-issue workspace
  directory, gated by an ``after_create`` hook (run only on first creation).
- ``cleanup_for_issue`` — best-effort ``before_remove`` then directory delete.
- ``path_for_issue`` — pure path computation (sanitization + within-root).

Implementation-Defined choice #1 (plan): when ``after_create`` fails on a
*newly* created workspace, the partially-prepared directory is removed
before the exception propagates. SPED §9.3 marks this clean-up as ``MAY``;
we adopt it to avoid stale dirs across retries.

Concurrency: parallel ``ensure_for_issue`` calls for the same identifier
serialize on a per-identifier ``asyncio.Lock`` so the directory is created
exactly once and the ``after_create`` hook fires exactly once.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from river_gang.config.schema import EffectiveConfig
from river_gang.workspace.hooks import HookResult, run_hook
from river_gang.workspace.safety import sanitize_key, validate_within_root

logger = logging.getLogger(__name__)


HookRunner = Callable[..., Awaitable[HookResult]]


class WorkspaceManagerError(Exception):
    """Base class for workspace lifecycle failures."""


class WorkspaceNotADirectory(WorkspaceManagerError):  # noqa: N818 -- spec-defined
    """A non-directory file already exists at the computed workspace path."""


class WorkspaceHookFailed(WorkspaceManagerError):  # noqa: N818 -- spec-defined
    """``after_create`` (or another blocking hook) returned non-ok."""

    def __init__(self, *, script_name: str, result: HookResult) -> None:
        summary = (
            f"timeout after {result.duration_ms}ms"
            if result.timed_out
            else f"exit_code={result.exit_code}"
        )
        super().__init__(
            f"workspace hook {script_name!r} failed ({summary})"
        )
        self.script_name = script_name
        self.result = result


@dataclass(frozen=True)
class EnsureResult:
    """Outcome of :meth:`WorkspaceManager.ensure_for_issue`."""

    path: Path
    created_now: bool


class WorkspaceManager:
    def __init__(
        self,
        *,
        config: EffectiveConfig,
        hook_runner: HookRunner | None = None,
    ) -> None:
        self._config = config
        self._hook_runner: HookRunner = hook_runner or run_hook
        # One lock per identifier so different issues run in parallel; the
        # outer lock just guards lazy creation of the inner locks.
        self._locks_lock = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Pure path computation
    # ------------------------------------------------------------------

    def path_for_issue(self, identifier: str) -> Path:
        """Resolve the absolute workspace path for ``identifier``.

        Raises whatever :func:`sanitize_key` and :func:`validate_within_root`
        raise (empty/reserved key, or path outside root).
        """
        key = sanitize_key(identifier)
        root = Path(self._config.workspace.root)
        candidate = root / key
        return validate_within_root(root, candidate)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def ensure_for_issue(self, identifier: str) -> EnsureResult:
        """Create or reuse the per-issue workspace directory.

        On *first* creation runs the configured ``after_create`` hook; if
        the hook fails, the partially-prepared directory is removed and
        :class:`WorkspaceHookFailed` is raised.

        Raises:
            WorkspaceNotADirectory: a non-directory file occupies the
                expected path.
            WorkspaceHookFailed: ``after_create`` returned non-ok.
        """
        path = self.path_for_issue(identifier)

        async with await self._lock_for(identifier):
            if path.exists():
                if not path.is_dir():
                    raise WorkspaceNotADirectory(
                        f"expected workspace at {path}, found non-directory"
                    )
                return EnsureResult(path=path, created_now=False)

            # New workspace: ensure root exists, then mkdir the leaf, then
            # run after_create. Failure unwinds the leaf dir.
            path.parent.mkdir(parents=True, exist_ok=True)
            path.mkdir()
            try:
                await self._maybe_run_after_create(path)
            except Exception:
                # Implementation-Defined choice #1: remove partially-prepared
                # workspace so the next ensure call starts clean.
                shutil.rmtree(path, ignore_errors=True)
                raise

            return EnsureResult(path=path, created_now=True)

    async def cleanup_for_issue(self, identifier: str) -> None:
        """Run ``before_remove`` (if dir exists) then delete the workspace.

        ``before_remove`` failures are logged at WARNING and ignored —
        cleanup proceeds. If the directory does not exist both the hook
        and the delete are skipped.
        """
        path = self.path_for_issue(identifier)

        async with await self._lock_for(identifier):
            if not path.exists():
                return  # nothing to clean, skip hook too (§9.4)

            await self._maybe_run_before_remove(path)
            shutil.rmtree(path, ignore_errors=False)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _lock_for(self, identifier: str) -> asyncio.Lock:
        async with self._locks_lock:
            lock = self._locks.get(identifier)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[identifier] = lock
            return lock

    async def _maybe_run_after_create(self, path: Path) -> None:
        script = self._config.hooks.after_create
        if script is None or script.strip() == "":
            return
        result = await self._hook_runner(
            script,
            cwd=path,
            timeout_ms=self._config.hooks.timeout_ms,
        )
        if not result.ok:
            raise WorkspaceHookFailed(script_name="after_create", result=result)

    async def _maybe_run_before_remove(self, path: Path) -> None:
        script = self._config.hooks.before_remove
        if script is None or script.strip() == "":
            return
        result = await self._hook_runner(
            script,
            cwd=path,
            timeout_ms=self._config.hooks.timeout_ms,
        )
        if not result.ok:
            # SPED §9.4: before_remove failure is logged but ignored.
            logger.warning(
                "before_remove hook failed; continuing cleanup: "
                "exit_code=%s timed_out=%s duration_ms=%s",
                result.exit_code,
                result.timed_out,
                result.duration_ms,
            )
