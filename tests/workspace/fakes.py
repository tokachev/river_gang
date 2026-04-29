"""In-memory fake workspace manager for orchestrator integration tests
(Task 24a).

Duck-typed double for :class:`river_gang.workspace.manager.WorkspaceManager`.
No real filesystem writes — all paths are synthetic and the per-issue
"existence" is tracked in a set. Passes :class:`EnsureResult` /
:class:`WorkspaceHookFailed` semantics through so orchestrator tests see
the same exit branches as production.

Configuration knobs:

- ``after_create_results``: ``{identifier: HookResult}`` overrides the
  default success outcome of the synthetic ``after_create`` hook. A
  non-ok :class:`HookResult` raises :class:`WorkspaceHookFailed` and
  unmarks the issue so the next ensure call recreates from scratch.
- ``before_remove_results``: ``{identifier: HookResult}`` records but
  doesn't block cleanup (matches §9.4: failures logged-but-ignored).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from river_gang.workspace.hooks import HookResult
from river_gang.workspace.manager import (
    EnsureResult,
    WorkspaceHookFailed,
)
from river_gang.workspace.safety import sanitize_key, validate_within_root


class FakeWorkspaceManager:
    def __init__(
        self,
        *,
        root_path: Path,
        after_create_results: dict[str, HookResult] | None = None,
        before_remove_results: dict[str, HookResult] | None = None,
    ) -> None:
        self._root_path = root_path
        self._after_create_results: dict[str, HookResult] = dict(
            after_create_results or {}
        )
        self._before_remove_results: dict[str, HookResult] = dict(
            before_remove_results or {}
        )
        self._existing: set[str] = set()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # ------------------------------------------------------------------
    # Public path computation (mirrors WorkspaceManager.path_for_issue)
    # ------------------------------------------------------------------

    def path_for_issue(self, identifier: str) -> Path:
        key = sanitize_key(identifier)
        candidate = self._root_path / key
        # Reuse production path-safety check so test assertions match the
        # real :class:`WorkspaceManager` behaviour bit-for-bit.
        return validate_within_root(self._root_path, candidate)

    # ------------------------------------------------------------------
    # Configuration mutators
    # ------------------------------------------------------------------

    def set_after_create_result(
        self, identifier: str, result: HookResult | None
    ) -> None:
        if result is None:
            self._after_create_results.pop(identifier, None)
        else:
            self._after_create_results[identifier] = result

    def set_before_remove_result(
        self, identifier: str, result: HookResult | None
    ) -> None:
        if result is None:
            self._before_remove_results.pop(identifier, None)
        else:
            self._before_remove_results[identifier] = result

    # ------------------------------------------------------------------
    # WorkspaceManager surface
    # ------------------------------------------------------------------

    async def ensure_for_issue(self, identifier: str) -> EnsureResult:
        self.calls.append(("ensure_for_issue", {"identifier": identifier}))
        path = self.path_for_issue(identifier)

        if identifier in self._existing:
            return EnsureResult(path=path, created_now=False)

        # Mark as existing BEFORE running the synthetic after_create hook,
        # then unmark on failure so the next attempt sees a clean slate.
        self._existing.add(identifier)
        result = self._after_create_results.get(identifier)
        if result is not None and not result.ok:
            self._existing.discard(identifier)
            raise WorkspaceHookFailed(script_name="after_create", result=result)

        return EnsureResult(path=path, created_now=True)

    async def cleanup_for_issue(self, identifier: str) -> None:
        self.calls.append(("cleanup_for_issue", {"identifier": identifier}))
        if identifier not in self._existing:
            return
        # before_remove failures are logged-but-ignored in production;
        # the fake just records the configured outcome and proceeds.
        self._before_remove_results.get(identifier)
        self._existing.discard(identifier)


__all__ = ["FakeWorkspaceManager"]
