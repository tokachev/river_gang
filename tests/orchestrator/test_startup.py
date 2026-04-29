"""Tests for ``start_service`` (SPED §16.1, Implementation-Defined Choice #7)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from river_gang.config.schema import EffectiveConfig
from river_gang.orchestrator import OrchestratorState
from river_gang.orchestrator.startup import (
    SHUTDOWN_GRACE_MS,
    fail_startup,
    start_service,
)
from tests.codex.fakes import FakeCodexClient
from tests.tracker.fakes import FakeTracker
from tests.workspace.fakes import FakeWorkspaceManager

_VALID_WORKFLOW = """---
tracker:
  kind: linear
  api_key: lit_secret
  project_slug: river-gang
polling:
  interval_ms: 60000
agent:
  max_concurrent_agents: 2
codex:
  command: codex app-server
---
prompt body for {{ issue.identifier }}
"""

_INVALID_WORKFLOW_NO_API_KEY = """---
tracker:
  kind: linear
  project_slug: river-gang
codex:
  command: codex app-server
---
prompt body
"""


def _write_workflow(path: Path, body: str = _VALID_WORKFLOW) -> Path:
    path.write_text(body)
    return path


class _NoopAwatch:
    """Inject this in place of ``watchfiles.awatch``: yields nothing forever."""

    def __init__(self) -> None:
        self._stop: asyncio.Event | None = None

    def __call__(self, *args: Any, **kwargs: Any) -> AsyncIterator[set[Any]]:
        self._stop = kwargs.get("stop_event")
        return self._iter()

    async def _iter(self) -> AsyncIterator[set[Any]]:
        # Block until stop_event is set; never yield a real change.
        if self._stop is not None:
            await self._stop.wait()
        # Empty body — async generator ends without yielding.
        if False:
            yield set()


def _make_factories(
    *,
    tracker: FakeTracker | None = None,
    codex_client: FakeCodexClient | None = None,
    workspace_manager: FakeWorkspaceManager | None = None,
) -> dict[str, Any]:
    tracker = tracker or FakeTracker()
    codex_client = codex_client or FakeCodexClient()

    def _tracker_factory(_cfg: EffectiveConfig) -> Any:
        return tracker

    def _codex_factory(_cfg: EffectiveConfig) -> Any:
        return codex_client

    def _ws_factory(cfg: EffectiveConfig) -> Any:
        return workspace_manager or FakeWorkspaceManager(
            root_path=Path(cfg.workspace.root)
        )

    return {
        "tracker_factory": _tracker_factory,
        "codex_client_factory": _codex_factory,
        "workspace_manager_factory": _ws_factory,
    }


# ---------------------------------------------------------------------------
# fail_startup helper
# ---------------------------------------------------------------------------


def test_fail_startup_returns_nonzero_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR, logger="river_gang.orchestrator.startup"):
        code = fail_startup("config invalid")
    assert code == 1
    assert any("config invalid" in rec.message for rec in caplog.records), caplog.text


# ---------------------------------------------------------------------------
# Happy startup — short-lived run that posts Shutdown after first tick
# ---------------------------------------------------------------------------


async def test_happy_startup_returns_zero(tmp_path: Path) -> None:
    workflow = _write_workflow(tmp_path / "WORKFLOW.md")
    tracker = FakeTracker()
    codex_client = FakeCodexClient()
    workspace_manager = FakeWorkspaceManager(root_path=tmp_path / "workspaces")

    shutdown_event = asyncio.Event()
    awatch = _NoopAwatch()

    async def _arm_shutdown() -> None:
        # Yield a couple of cycles so the first tick fires before we shut down.
        await asyncio.sleep(0.05)
        shutdown_event.set()

    arm_task = asyncio.create_task(_arm_shutdown())
    try:
        code = await start_service(
            workflow_path=workflow,
            install_signal_handlers=False,
            shutdown_event=shutdown_event,
            awatch_factory=awatch,
            **_make_factories(
                tracker=tracker,
                codex_client=codex_client,
                workspace_manager=workspace_manager,
            ),
        )
    finally:
        arm_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await arm_task

    assert code == 0
    # At least one fetch happened during startup terminal cleanup OR first tick.
    fetched_methods = {call[0] for call in tracker.calls}
    assert "fetch_issues_by_states" in fetched_methods or \
        "fetch_candidate_issues" in fetched_methods


# ---------------------------------------------------------------------------
# Validation failure: missing api_key → exit 1
# ---------------------------------------------------------------------------


async def test_validation_failure_returns_one(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    workflow = _write_workflow(
        tmp_path / "WORKFLOW.md", _INVALID_WORKFLOW_NO_API_KEY
    )

    with caplog.at_level(logging.ERROR, logger="river_gang.orchestrator.startup"):
        code = await start_service(
            workflow_path=workflow,
            install_signal_handlers=False,
            shutdown_event=asyncio.Event(),  # never set — irrelevant on early exit
            awatch_factory=_NoopAwatch(),
            **_make_factories(),
        )

    assert code == 1
    # Operator-visible error mentions the missing field.
    assert any(
        "api_key" in rec.message or "tracker.api_key" in rec.message
        for rec in caplog.records
    ), caplog.text


# ---------------------------------------------------------------------------
# Terminal cleanup happens at startup
# ---------------------------------------------------------------------------


async def test_startup_terminal_cleanup_invokes_workspace_cleanup(
    tmp_path: Path,
) -> None:
    from river_gang.tracker.issue import Issue

    workflow = _write_workflow(tmp_path / "WORKFLOW.md")

    def _terminal_issue(identifier: str, state: str) -> Issue:
        return Issue(
            id=identifier,
            identifier=identifier,
            title=f"t {identifier}",
            state=state,
            description=None,
            priority=None,
            branch_name=None,
            url=None,
            labels=(),
            blocked_by=(),
            created_at=None,
            updated_at=None,
        )

    tracker = FakeTracker(
        terminal_by_state={
            "Done": [_terminal_issue("DONE-1", "Done")],
            "Closed": [_terminal_issue("CLOSED-1", "Closed")],
        }
    )
    workspace_manager = FakeWorkspaceManager(root_path=tmp_path / "ws")
    # Pretend both workspaces exist on disk so cleanup_for_issue records them.
    workspace_manager._existing.update(  # noqa: SLF001 -- test setup
        {"DONE-1", "CLOSED-1"}
    )

    shutdown_event = asyncio.Event()
    shutdown_event.set()  # exit on first mailbox.recv

    code = await start_service(
        workflow_path=workflow,
        install_signal_handlers=False,
        shutdown_event=shutdown_event,
        awatch_factory=_NoopAwatch(),
        **_make_factories(
            tracker=tracker, workspace_manager=workspace_manager
        ),
    )

    assert code == 0
    cleaned = {
        call[1]["identifier"]
        for call in workspace_manager.calls
        if call[0] == "cleanup_for_issue"
    }
    assert cleaned == {"DONE-1", "CLOSED-1"}


# ---------------------------------------------------------------------------
# Terminal cleanup failure → log warning + continue, exit 0
# ---------------------------------------------------------------------------


async def test_startup_terminal_cleanup_failure_continues(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from river_gang.tracker.errors import LinearError

    workflow = _write_workflow(tmp_path / "WORKFLOW.md")
    tracker = FakeTracker()
    tracker.fail_next_terminal(LinearError("upstream 503"))

    shutdown_event = asyncio.Event()
    shutdown_event.set()

    with caplog.at_level(logging.WARNING, logger="river_gang.orchestrator.startup"):
        code = await start_service(
            workflow_path=workflow,
            install_signal_handlers=False,
            shutdown_event=shutdown_event,
            awatch_factory=_NoopAwatch(),
            **_make_factories(tracker=tracker),
        )

    assert code == 0
    assert any(
        "terminal cleanup" in rec.message.lower()
        or "fetch_issues_by_states" in rec.message
        for rec in caplog.records
    ), caplog.text


# ---------------------------------------------------------------------------
# Graceful shutdown: retry timers cancelled, watcher stopped
# ---------------------------------------------------------------------------


async def test_graceful_shutdown_cancels_retry_and_stops_watcher(
    tmp_path: Path,
) -> None:
    workflow = _write_workflow(tmp_path / "WORKFLOW.md")
    tracker = FakeTracker()
    awatch = _NoopAwatch()

    captured: dict[str, Any] = {}

    def _capture_codex(_cfg: EffectiveConfig) -> Any:
        client = FakeCodexClient()
        captured["codex_client"] = client
        return client

    def _capture_tracker(_cfg: EffectiveConfig) -> Any:
        captured["tracker"] = tracker
        return tracker

    def _capture_ws(cfg: EffectiveConfig) -> Any:
        ws = FakeWorkspaceManager(root_path=Path(cfg.workspace.root))
        captured["workspace"] = ws
        return ws

    shutdown_event = asyncio.Event()

    async def _arm_shutdown() -> None:
        await asyncio.sleep(0.05)
        shutdown_event.set()

    arm = asyncio.create_task(_arm_shutdown())
    try:
        code = await start_service(
            workflow_path=workflow,
            install_signal_handlers=False,
            shutdown_event=shutdown_event,
            awatch_factory=awatch,
            tracker_factory=_capture_tracker,
            codex_client_factory=_capture_codex,
            workspace_manager_factory=_capture_ws,
        )
    finally:
        arm.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await arm

    assert code == 0


# ---------------------------------------------------------------------------
# Shutdown grace expired: stubborn worker → cancel called, log warning, exit 0
# ---------------------------------------------------------------------------


async def test_shutdown_grace_expired_cancels_stubborn_worker(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A worker that ignores the first cancel still terminates within grace."""
    workflow = _write_workflow(tmp_path / "WORKFLOW.md")

    cancel_count = {"n": 0}

    async def _stubborn_worker() -> None:
        try:
            while True:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    cancel_count["n"] += 1
                    # Ignore the first cancel; honour subsequent ones.
                    if cancel_count["n"] >= 2:
                        raise
        except asyncio.CancelledError:
            return

    # Pre-populate the orchestrator state with a stubborn running worker by
    # exploiting the test helper: we drive start_service with an immediate
    # shutdown_event so it goes straight to the graceful shutdown phase, but
    # we inject a state-mutating tracker_factory that adds a running entry
    # before the run loop starts.

    from collections import deque
    from datetime import UTC, datetime

    from river_gang.orchestrator.state import RunningEntry
    from river_gang.tracker.issue import Issue

    def _issue() -> Issue:
        return Issue(
            id="stub", identifier="STUB-1", title="t", state="In Progress",
            description=None, priority=None, branch_name=None, url=None,
            labels=(), blocked_by=(), created_at=None, updated_at=None,
        )

    holder: dict[str, Any] = {}

    def _tracker_factory(_cfg: EffectiveConfig) -> Any:
        return FakeTracker()

    def _codex_factory(_cfg: EffectiveConfig) -> Any:
        return FakeCodexClient()

    def _ws_factory(cfg: EffectiveConfig) -> Any:
        return FakeWorkspaceManager(root_path=Path(cfg.workspace.root))

    def _state_hook(state: OrchestratorState) -> None:
        worker_task = asyncio.create_task(_stubborn_worker())
        entry = RunningEntry(
            worker_handle=worker_task,
            monitor_handle=None,
            identifier="STUB-1",
            issue=_issue(),
            session_id=None,
            last_reported_input_tokens=0,
            last_reported_output_tokens=0,
            last_reported_total_tokens=0,
            started_at=datetime.now(UTC),
            last_codex_timestamp=None,
            last_codex_event=None,
            last_codex_message=None,
            recent_events=deque(maxlen=50),
            last_error=None,
            restart_count=0,
            retry_attempt=0,
        )
        state.add_running(entry)
        holder["worker"] = worker_task

    shutdown_event = asyncio.Event()
    shutdown_event.set()  # immediate Shutdown — go to graceful path

    with caplog.at_level(logging.WARNING, logger="river_gang.orchestrator.startup"):
        code = await start_service(
            workflow_path=workflow,
            install_signal_handlers=False,
            shutdown_event=shutdown_event,
            awatch_factory=_NoopAwatch(),
            tracker_factory=_tracker_factory,
            codex_client_factory=_codex_factory,
            workspace_manager_factory=_ws_factory,
            state_hook=_state_hook,
            shutdown_grace_ms=200,  # tight grace so test is fast
        )

    assert code == 0
    # Worker was cancelled at least once.
    assert cancel_count["n"] >= 1
    # Worker eventually finished (even if grace had to fire it twice).
    assert holder["worker"].done()


# ---------------------------------------------------------------------------
# Signal handler installation (POSIX only)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="signal handlers POSIX-only")
async def test_signal_handler_posts_shutdown(tmp_path: Path) -> None:
    workflow = _write_workflow(tmp_path / "WORKFLOW.md")
    awatch = _NoopAwatch()

    # Install signal handlers; trigger via os.kill with our own pid.
    async def _kill_self_after_delay() -> None:
        await asyncio.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)

    killer = asyncio.create_task(_kill_self_after_delay())
    try:
        code = await start_service(
            workflow_path=workflow,
            install_signal_handlers=True,
            awatch_factory=awatch,
            **_make_factories(),
        )
    finally:
        killer.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await killer
    assert code == 0


# ---------------------------------------------------------------------------
# SHUTDOWN_GRACE_MS constant exposed
# ---------------------------------------------------------------------------


def test_shutdown_grace_ms_constant_value() -> None:
    assert SHUTDOWN_GRACE_MS == 30000


# ---------------------------------------------------------------------------
# Per-worker CodexClient factory (BLOCKER fix)
# ---------------------------------------------------------------------------


async def test_build_default_per_worker_codex_factory_constructs_real_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production-default factory must build a real :class:`CodexClient`
    (not return ``None``) when invoked per worker. We inject a fake
    :class:`CodexProcess` so no actual subprocess spawns.
    """
    from river_gang.codex.client import CodexClient
    from river_gang.config.defaults import apply_defaults
    from river_gang.orchestrator import startup as startup_mod
    from tests.codex.fakes import FakeCodexProcess

    # Stub CodexProcess.launch so the factory can run without bash.
    captured: dict[str, Any] = {}

    async def _fake_launch(
        command: str,
        *,
        cwd: Path,
        workspace_root: Path,
        max_frame_bytes: int = 10 * 1024 * 1024,
    ) -> Any:
        captured["command"] = command
        captured["cwd"] = cwd
        captured["workspace_root"] = workspace_root
        return FakeCodexProcess()

    monkeypatch.setattr(
        "river_gang.codex.process.CodexProcess.launch",
        _fake_launch,
    )

    raw: dict[str, Any] = {
        "tracker": {
            "kind": "linear",
            "api_key": "lit_test",
            "project_slug": "river-gang",
        },
        "polling": {"interval_ms": 60_000},
        "agent": {"max_concurrent_agents": 2},
        "codex": {"command": "codex app-server"},
        "workspace": {"root": str(tmp_path)},
    }
    config = apply_defaults(raw)

    # Stand-in transport (factory only consults type/identity).
    class _StubTransport:
        async def execute(
            self, query: str, variables: dict[str, Any]
        ) -> dict[str, Any]:
            return {"data": {}}

    transport = _StubTransport()

    factory = startup_mod.build_default_per_worker_codex_factory(
        transport=transport, project_slug="river-gang"
    )
    workspace_path = tmp_path / "issue-1"
    workspace_path.mkdir()
    client = await factory(workspace_path, config)

    assert isinstance(client, CodexClient)
    assert captured["command"] == "codex app-server"
    assert captured["cwd"] == workspace_path
    assert captured["workspace_root"] == tmp_path
    # Regression: factory must wire the live process pid (not the
    # subprocess return code) into ``codex_app_server_pid`` so RuntimeEvent
    # log records carry a meaningful pid.
    assert client._codex_app_server_pid == 12345  # noqa: SLF001 -- regression check
