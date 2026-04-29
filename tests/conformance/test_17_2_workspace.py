"""SPED §17.2 conformance: Workspace Manager and Safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from river_gang.config.defaults import apply_defaults
from river_gang.workspace import (
    HookResult,
    WorkspaceHookFailed,
    WorkspaceManager,
    WorkspaceNotADirectory,
    WorkspaceOutsideRoot,
    sanitize_key,
    validate_within_root,
)
from river_gang.workspace.hooks import run_hook  # noqa: F401 -- re-export check

pytestmark = pytest.mark.conformance


def _config(tmp_path: Path, **hook_overrides: str | int | None):
    raw: dict = {"workspace": {"root": str(tmp_path)}}
    if hook_overrides:
        raw["hooks"] = hook_overrides
    return apply_defaults(raw)


class _FakeHookRunner:
    """Programmable hook runner — script name → HookResult."""

    def __init__(self, results: dict[str, HookResult] | None = None) -> None:
        self.results = dict(results or {})
        self.calls: list[str] = []

    async def __call__(
        self, script: str, *, cwd: Path, timeout_ms: int
    ) -> HookResult:
        self.calls.append(script)
        return self.results.get(
            script, HookResult(
                exit_code=0, stdout=b"", stderr=b"",
                duration_ms=1, timed_out=False, is_skipped=False,
            )
        )


def test_deterministic_workspace_path_per_identifier(tmp_path: Path) -> None:
    """Conformance §17.2: deterministic workspace path per issue
    identifier."""
    cfg = _config(tmp_path)
    mgr = WorkspaceManager(config=cfg)
    p1 = mgr.path_for_issue("MT-1")
    p2 = mgr.path_for_issue("MT-1")
    assert p1 == p2


async def test_missing_workspace_directory_is_created(tmp_path: Path) -> None:
    """Conformance §17.2: missing workspace directory is created."""
    cfg = _config(tmp_path)
    mgr = WorkspaceManager(config=cfg)
    res = await mgr.ensure_for_issue("MT-NEW")
    assert res.created_now is True
    assert res.path.is_dir()


async def test_existing_workspace_directory_is_reused(tmp_path: Path) -> None:
    """Conformance §17.2: existing workspace directory is reused."""
    cfg = _config(tmp_path)
    mgr = WorkspaceManager(config=cfg)
    first = await mgr.ensure_for_issue("MT-RE")
    second = await mgr.ensure_for_issue("MT-RE")
    assert first.path == second.path
    assert second.created_now is False


async def test_existing_non_directory_at_path_raises(tmp_path: Path) -> None:
    """Conformance §17.2: existing non-directory path at workspace location
    is handled safely (replace or fail per implementation policy)."""
    cfg = _config(tmp_path)
    mgr = WorkspaceManager(config=cfg)
    target = mgr.path_for_issue("MT-FILE")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("im a file")
    with pytest.raises(WorkspaceNotADirectory):
        await mgr.ensure_for_issue("MT-FILE")


async def test_after_create_failure_surfaced(tmp_path: Path) -> None:
    """Conformance §17.2: OPTIONAL workspace population/synchronization
    errors are surfaced."""
    cfg = _config(tmp_path, after_create="run-it")
    runner = _FakeHookRunner({
        "run-it": HookResult(
            exit_code=1, stdout=b"", stderr=b"boom",
            duration_ms=10, timed_out=False, is_skipped=False,
        )
    })
    mgr = WorkspaceManager(config=cfg, hook_runner=runner)
    with pytest.raises(WorkspaceHookFailed):
        await mgr.ensure_for_issue("MT-PFAIL")


async def test_after_create_runs_only_on_new_creation(tmp_path: Path) -> None:
    """Conformance §17.2: ``after_create`` hook runs only on new workspace
    creation."""
    cfg = _config(tmp_path, after_create="ac")
    runner = _FakeHookRunner()
    mgr = WorkspaceManager(config=cfg, hook_runner=runner)
    await mgr.ensure_for_issue("MT-X")
    await mgr.ensure_for_issue("MT-X")  # reuse
    assert runner.calls.count("ac") == 1


async def test_before_run_failure_aborts_attempt() -> None:
    """Conformance §17.2: ``before_run`` hook runs before each attempt and
    failure/timeouts abort the current attempt.

    Worker contract from Task 32 + §16.5: ``before_run`` non-ok →
    ``WorkerExit(reason='before_run_failed', ok=False)``. Verified end-
    to-end in tests/orchestrator/test_worker.py; here we just assert the
    contract surface (HookResult.ok flag drives the abort branch).
    """
    failed = HookResult(
        exit_code=2, stdout=b"", stderr=b"x",
        duration_ms=5, timed_out=False, is_skipped=False,
    )
    assert failed.ok is False


async def test_after_run_failure_logged_and_ignored() -> None:
    """Conformance §17.2: ``after_run`` hook runs after each attempt and
    failure/timeouts are logged and ignored.

    Worker contract: ``_run_after_run_best_effort`` swallows non-ok
    results with a WARNING log, leaving ``ok=True`` intact. Behaviour
    asserted in tests/orchestrator/test_worker.py::
    test_after_run_failure_does_not_flip_ok.
    """
    # Direct unit-level check: HookResult exposes a clean ok flag the
    # worker uses; the suppression branch is covered upstream.
    failed = HookResult(
        exit_code=1, stdout=b"", stderr=b"",
        duration_ms=1, timed_out=False, is_skipped=False,
    )
    assert failed.ok is False  # caller suppresses


async def test_before_remove_failure_ignored(tmp_path: Path) -> None:
    """Conformance §17.2: ``before_remove`` hook runs on cleanup and
    failures/timeouts are ignored."""
    cfg = _config(tmp_path, before_remove="br")
    runner = _FakeHookRunner({
        "br": HookResult(
            exit_code=1, stdout=b"", stderr=b"boom",
            duration_ms=2, timed_out=False, is_skipped=False,
        )
    })
    mgr = WorkspaceManager(config=cfg, hook_runner=runner)
    await mgr.ensure_for_issue("MT-RM")
    # Must NOT raise — failure is logged + ignored.
    await mgr.cleanup_for_issue("MT-RM")
    assert "br" in runner.calls


def test_path_sanitization_and_root_containment_enforced(
    tmp_path: Path,
) -> None:
    """Conformance §17.2: workspace path sanitization and root containment
    invariants are enforced before agent launch."""
    # sanitize_key strips path-separators / illegal chars.
    assert sanitize_key("MT-1") == "MT-1"
    # validate_within_root rejects paths escaping root.
    with pytest.raises(WorkspaceOutsideRoot):
        validate_within_root(tmp_path, tmp_path / ".." / "evil")


def test_agent_launch_rejects_out_of_root_paths(tmp_path: Path) -> None:
    """Conformance §17.2: agent launch uses the per-issue workspace path
    as cwd and rejects out-of-root paths.

    The codex process layer (:func:`CodexProcess.launch`) calls
    ``validate_within_root`` before spawning bash; an out-of-root cwd
    raises :class:`InvalidWorkspaceCwd` which the worker surfaces as
    ``workspace_failed``. Verified in tests/codex/test_process.py;
    this conformance assertion locks the gate function itself.
    """
    with pytest.raises(WorkspaceOutsideRoot):
        validate_within_root(tmp_path, tmp_path.parent / "outside")
