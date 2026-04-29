"""Tests for :class:`WorkspaceManager` (SPED §9.2, §9.3, §9.4, §9.5)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from river_gang.config.defaults import apply_defaults
from river_gang.config.schema import EffectiveConfig, HooksConfig, WorkspaceConfig
from river_gang.workspace.hooks import HookResult
from river_gang.workspace.manager import (
    EnsureResult,
    WorkspaceManager,
    WorkspaceNotADirectory,
)

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


@dataclass
class _HookCall:
    script: str | None
    cwd: Path
    timeout_ms: int


class FakeHookRunner:
    """Records each ``run_hook`` invocation and returns canned results."""

    def __init__(self) -> None:
        self.calls: list[_HookCall] = []
        self._scripted: dict[str, list[HookResult]] = {}

    def script(self, script: str, *results: HookResult) -> None:
        self._scripted[script] = list(results)

    async def __call__(
        self, script: str | None, *, cwd: Path, timeout_ms: int
    ) -> HookResult:
        self.calls.append(_HookCall(script=script, cwd=cwd, timeout_ms=timeout_ms))
        if script is None or script.strip() == "":
            return HookResult.skipped()
        queued = self._scripted.get(script, [])
        if queued:
            return queued.pop(0)
        return HookResult(
            exit_code=0,
            stdout=b"",
            stderr=b"",
            duration_ms=1,
            timed_out=False,
            is_skipped=False,
        )


def _make_config(
    *,
    workspace_root: Path,
    hooks: HooksConfig | None = None,
) -> EffectiveConfig:
    base = apply_defaults({})
    return EffectiveConfig(
        tracker=base.tracker,
        polling=base.polling,
        workspace=WorkspaceConfig(root=str(workspace_root)),
        hooks=hooks or base.hooks,
        agent=base.agent,
        codex=base.codex,
    )


def _hooks_with(
    *,
    after_create: str | None = None,
    before_remove: str | None = None,
    timeout_ms: int = 60000,
) -> HooksConfig:
    return HooksConfig(
        after_create=after_create,
        before_run=None,
        after_run=None,
        before_remove=before_remove,
        timeout_ms=timeout_ms,
    )


def _failure_result() -> HookResult:
    return HookResult(
        exit_code=1,
        stdout=b"",
        stderr=b"boom",
        duration_ms=2,
        timed_out=False,
        is_skipped=False,
    )


# ---------------------------------------------------------------------------
# path_for_issue
# ---------------------------------------------------------------------------


def test_path_for_issue_sanitizes_and_joins_with_root(tmp_path: Path) -> None:
    cfg = _make_config(workspace_root=tmp_path)
    mgr = WorkspaceManager(config=cfg)
    p = mgr.path_for_issue("RG-1")
    assert p == tmp_path.resolve() / "RG-1"


def test_path_for_issue_replaces_disallowed_chars(tmp_path: Path) -> None:
    cfg = _make_config(workspace_root=tmp_path)
    mgr = WorkspaceManager(config=cfg)
    p = mgr.path_for_issue("issue/RG-1 weird")
    assert p == tmp_path.resolve() / "issue_RG-1_weird"


def test_path_for_issue_rejects_empty_identifier(tmp_path: Path) -> None:
    cfg = _make_config(workspace_root=tmp_path)
    mgr = WorkspaceManager(config=cfg)
    with pytest.raises(Exception):
        mgr.path_for_issue("")


def test_path_for_issue_rejects_dotdot_identifier(tmp_path: Path) -> None:
    cfg = _make_config(workspace_root=tmp_path)
    mgr = WorkspaceManager(config=cfg)
    with pytest.raises(Exception):
        mgr.path_for_issue("..")


# ---------------------------------------------------------------------------
# ensure_for_issue
# ---------------------------------------------------------------------------


async def test_ensure_first_call_creates_dir_returns_created_now_true(
    tmp_path: Path,
) -> None:
    cfg = _make_config(workspace_root=tmp_path)
    fake = FakeHookRunner()
    mgr = WorkspaceManager(config=cfg, hook_runner=fake)

    result = await mgr.ensure_for_issue("RG-1")

    assert isinstance(result, EnsureResult)
    assert result.created_now is True
    assert result.path == tmp_path.resolve() / "RG-1"
    assert result.path.is_dir()


async def test_ensure_second_call_reuses_dir_returns_created_now_false(
    tmp_path: Path,
) -> None:
    cfg = _make_config(workspace_root=tmp_path)
    fake = FakeHookRunner()
    mgr = WorkspaceManager(config=cfg, hook_runner=fake)

    first = await mgr.ensure_for_issue("RG-1")
    second = await mgr.ensure_for_issue("RG-1")

    assert first.created_now is True
    assert second.created_now is False
    assert first.path == second.path


async def test_ensure_does_not_call_after_create_when_no_hook_configured(
    tmp_path: Path,
) -> None:
    cfg = _make_config(workspace_root=tmp_path, hooks=_hooks_with(after_create=None))
    fake = FakeHookRunner()
    mgr = WorkspaceManager(config=cfg, hook_runner=fake)

    await mgr.ensure_for_issue("RG-1")

    # No hook configured → runner not invoked at all
    assert fake.calls == []


async def test_ensure_calls_after_create_only_when_created_now(
    tmp_path: Path,
) -> None:
    cfg = _make_config(
        workspace_root=tmp_path,
        hooks=_hooks_with(after_create="echo create", timeout_ms=1234),
    )
    fake = FakeHookRunner()
    mgr = WorkspaceManager(config=cfg, hook_runner=fake)

    await mgr.ensure_for_issue("RG-1")
    await mgr.ensure_for_issue("RG-1")  # second call must NOT run after_create

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call.script == "echo create"
    assert call.cwd == tmp_path.resolve() / "RG-1"
    assert call.timeout_ms == 1234


async def test_ensure_after_create_failure_removes_dir_and_raises(
    tmp_path: Path,
) -> None:
    """Plan implementation-defined choice #1: clean up on hook failure."""
    cfg = _make_config(
        workspace_root=tmp_path, hooks=_hooks_with(after_create="bad")
    )
    fake = FakeHookRunner()
    fake.script("bad", _failure_result())
    mgr = WorkspaceManager(config=cfg, hook_runner=fake)

    expected_path = tmp_path.resolve() / "RG-1"
    with pytest.raises(Exception):
        await mgr.ensure_for_issue("RG-1")

    assert not expected_path.exists()
    # Subsequent retry can recreate cleanly with created_now=True
    fake.script("bad", HookResult(0, b"", b"", 1, False, False))
    second = await mgr.ensure_for_issue("RG-1")
    assert second.created_now is True
    assert second.path.is_dir()


async def test_ensure_after_create_timeout_also_removes_dir(
    tmp_path: Path,
) -> None:
    cfg = _make_config(
        workspace_root=tmp_path, hooks=_hooks_with(after_create="slow")
    )
    fake = FakeHookRunner()
    fake.script("slow", HookResult.timeout(stdout=b"", stderr=b"", duration_ms=200))
    mgr = WorkspaceManager(config=cfg, hook_runner=fake)

    expected_path = tmp_path.resolve() / "RG-1"
    with pytest.raises(Exception):
        await mgr.ensure_for_issue("RG-1")
    assert not expected_path.exists()


async def test_ensure_existing_non_directory_at_path_raises(
    tmp_path: Path,
) -> None:
    cfg = _make_config(workspace_root=tmp_path)
    expected = tmp_path.resolve() / "RG-1"
    expected.write_text("I am a file, not a dir")

    mgr = WorkspaceManager(config=cfg)
    with pytest.raises(WorkspaceNotADirectory):
        await mgr.ensure_for_issue("RG-1")


async def test_ensure_creates_workspace_root_if_missing(tmp_path: Path) -> None:
    """Workspace root may not exist yet on first run — manager must create it."""
    root = tmp_path / "does_not_exist_yet" / "ws_root"
    cfg = _make_config(workspace_root=root)
    mgr = WorkspaceManager(config=cfg)

    result = await mgr.ensure_for_issue("RG-1")
    assert result.path.is_dir()
    assert root.is_dir()


# ---------------------------------------------------------------------------
# cleanup_for_issue
# ---------------------------------------------------------------------------


async def test_cleanup_runs_before_remove_then_deletes(tmp_path: Path) -> None:
    cfg = _make_config(
        workspace_root=tmp_path, hooks=_hooks_with(before_remove="echo bye")
    )
    fake = FakeHookRunner()
    mgr = WorkspaceManager(config=cfg, hook_runner=fake)

    await mgr.ensure_for_issue("RG-1")
    expected = tmp_path.resolve() / "RG-1"
    (expected / "leftover.txt").write_text("x")

    await mgr.cleanup_for_issue("RG-1")

    assert not expected.exists()
    # The runner was invoked exactly once (no after_create configured here)
    # with the before_remove script and the workspace cwd.
    assert any(c.script == "echo bye" and c.cwd == expected for c in fake.calls)


async def test_cleanup_deletes_when_no_before_remove_configured(
    tmp_path: Path,
) -> None:
    cfg = _make_config(workspace_root=tmp_path, hooks=_hooks_with(before_remove=None))
    fake = FakeHookRunner()
    mgr = WorkspaceManager(config=cfg, hook_runner=fake)

    await mgr.ensure_for_issue("RG-1")
    expected = tmp_path.resolve() / "RG-1"
    assert expected.is_dir()

    await mgr.cleanup_for_issue("RG-1")
    assert not expected.exists()
    assert fake.calls == []  # no hook configured at all


async def test_cleanup_before_remove_failure_logged_but_ignored(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _make_config(
        workspace_root=tmp_path, hooks=_hooks_with(before_remove="bad-bye")
    )
    fake = FakeHookRunner()
    fake.script("bad-bye", _failure_result())
    mgr = WorkspaceManager(config=cfg, hook_runner=fake)

    await mgr.ensure_for_issue("RG-1")
    expected = tmp_path.resolve() / "RG-1"

    caplog.set_level(logging.WARNING, logger="river_gang.workspace.manager")
    await mgr.cleanup_for_issue("RG-1")

    assert not expected.exists()
    assert any(
        r.levelno >= logging.WARNING and "before_remove" in r.message.lower()
        for r in caplog.records
    )


async def test_cleanup_missing_dir_is_noop(tmp_path: Path) -> None:
    cfg = _make_config(
        workspace_root=tmp_path, hooks=_hooks_with(before_remove="echo bye")
    )
    fake = FakeHookRunner()
    mgr = WorkspaceManager(config=cfg, hook_runner=fake)

    # Never ensured → dir does not exist. Cleanup must skip both hook AND delete.
    await mgr.cleanup_for_issue("RG-1")

    assert fake.calls == []  # before_remove SKIPPED when dir missing


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


async def test_concurrent_ensure_creates_dir_once_and_runs_hook_once(
    tmp_path: Path,
) -> None:
    """N parallel ``ensure_for_issue`` calls for the same identifier must
    converge to one created directory + one ``after_create`` invocation,
    and every caller gets the same path back."""
    cfg = _make_config(
        workspace_root=tmp_path, hooks=_hooks_with(after_create="echo create")
    )
    fake = FakeHookRunner()
    mgr = WorkspaceManager(config=cfg, hook_runner=fake)

    async def worker() -> EnsureResult:
        return await mgr.ensure_for_issue("RG-1")

    results = await asyncio.gather(*(worker() for _ in range(8)))

    # exactly one create
    created_count = sum(1 for r in results if r.created_now)
    assert created_count == 1
    assert all(r.path == results[0].path for r in results)
    # hook invoked exactly once
    after_create_calls = [c for c in fake.calls if c.script == "echo create"]
    assert len(after_create_calls) == 1


async def test_concurrent_ensure_different_identifiers_run_independently(
    tmp_path: Path,
) -> None:
    cfg = _make_config(
        workspace_root=tmp_path, hooks=_hooks_with(after_create="echo create")
    )
    fake = FakeHookRunner()
    mgr = WorkspaceManager(config=cfg, hook_runner=fake)

    async def worker(identifier: str) -> EnsureResult:
        return await mgr.ensure_for_issue(identifier)

    results = await asyncio.gather(*(worker(f"RG-{n}") for n in range(4)))

    assert len({r.path for r in results}) == 4
    assert all(r.created_now for r in results)
    after_create_calls = [c for c in fake.calls if c.script == "echo create"]
    assert len(after_create_calls) == 4


# ---------------------------------------------------------------------------
# Hook runner injection (default to real run_hook)
# ---------------------------------------------------------------------------


async def test_default_hook_runner_is_real_run_hook(tmp_path: Path) -> None:
    """Smoke check: when no runner is injected, the manager calls the real
    :func:`river_gang.workspace.hooks.run_hook`."""
    cfg = _make_config(
        workspace_root=tmp_path,
        hooks=_hooks_with(after_create="exit 0", timeout_ms=5000),
    )
    mgr = WorkspaceManager(config=cfg)  # no hook_runner kwarg
    result = await mgr.ensure_for_issue("RG-1")
    assert result.created_now is True
    assert result.path.is_dir()


async def test_manager_accepts_alternative_hook_runner_signature(
    tmp_path: Path,
) -> None:
    """The injected runner is just a callable matching the run_hook contract."""
    received: list[Any] = []

    async def custom(
        script: str | None, *, cwd: Path, timeout_ms: int
    ) -> HookResult:
        received.append((script, cwd, timeout_ms))
        return HookResult(0, b"", b"", 1, False, False)

    runner: Callable[..., Awaitable[HookResult]] = custom
    cfg = _make_config(
        workspace_root=tmp_path, hooks=_hooks_with(after_create="x", timeout_ms=42)
    )
    mgr = WorkspaceManager(config=cfg, hook_runner=runner)
    await mgr.ensure_for_issue("RG-1")

    assert received == [("x", tmp_path.resolve() / "RG-1", 42)]
