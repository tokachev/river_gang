"""Tests for ``Orchestrator`` poll-and-dispatch tick loop (§16.2)."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from river_gang.codex import RuntimeEvent, TokenSnapshot
from river_gang.config import EffectiveConfig
from river_gang.config.defaults import apply_defaults
from river_gang.orchestrator import (
    CodexUpdate,
    Mailbox,
    OrchestratorState,
    PollTick,
    RetryQueue,
    RetryTimerFired,
    RunningEntry,
    Shutdown,
    WorkerExit,
)
from river_gang.orchestrator.loop import Orchestrator
from river_gang.tracker.errors import LinearError
from river_gang.tracker.issue import Issue
from tests.codex.fakes import FakeCodexClient
from tests.tracker.fakes import FakeTracker
from tests.workspace.fakes import FakeWorkspaceManager


def _issue(
    *,
    id: str,
    state: str = "Todo",
    priority: int | None = 1,
    created_at: datetime | None = None,
) -> Issue:
    return Issue(
        id=id,
        identifier=f"MT-{id}",
        title=f"title {id}",
        state=state,
        description=None,
        priority=priority,
        branch_name=None,
        url=None,
        labels=(),
        blocked_by=(),
        created_at=created_at or datetime(2026, 4, 28, tzinfo=UTC),
        updated_at=None,
    )


def _running_entry(
    issue: Issue,
    *,
    started_at: datetime | None = None,
    last_codex_timestamp: datetime | None = None,
    worker_handle: asyncio.Task[None] | None = None,
) -> RunningEntry:
    if worker_handle is None:
        async def _idle() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                return
        worker_handle = asyncio.create_task(_idle())
    return RunningEntry(
        worker_handle=worker_handle,
        monitor_handle=None,
        identifier=issue.identifier,
        issue=issue,
        session_id=None,
        last_reported_input_tokens=0,
        last_reported_output_tokens=0,
        last_reported_total_tokens=0,
        started_at=started_at or datetime.now(UTC),
        last_codex_timestamp=last_codex_timestamp,
        last_codex_event=None,
        last_codex_message=None,
        recent_events=deque(maxlen=50),
        last_error=None,
        restart_count=0,
        retry_attempt=0,
    )


def _make_config(
    *,
    poll_ms: int = 30_000,
    max_concurrent: int = 5,
    by_state: dict[str, int] | None = None,
    stall_ms: int = 0,
) -> EffectiveConfig:
    raw: dict[str, Any] = {
        "tracker": {"kind": "linear", "active_states": ["Todo", "In Progress"]},
        "polling": {"interval_ms": poll_ms},
        "agent": {
            "max_concurrent_agents": max_concurrent,
            "max_concurrent_agents_by_state": by_state or {},
        },
        "codex": {"stall_timeout_ms": stall_ms},
    }
    return apply_defaults(raw)


def _make_orchestrator(
    *,
    state: OrchestratorState | None = None,
    config: EffectiveConfig | None = None,
    tracker: FakeTracker | None = None,
    codex_client: FakeCodexClient | None = None,
    workspace_manager: FakeWorkspaceManager | None = None,
    workflow_loader: Callable[[], EffectiveConfig | None] | None = None,
    tmp_path: Path | None = None,
) -> Orchestrator:
    config = config or _make_config()
    state = state or OrchestratorState(
        poll_interval_ms=config.polling.interval_ms,
        max_concurrent_agents=config.agent.max_concurrent_agents,
    )
    mailbox = Mailbox()
    retry_queue = RetryQueue(loop=asyncio.get_running_loop())
    tracker = tracker or FakeTracker()
    codex_client = codex_client or FakeCodexClient()
    workspace_manager = workspace_manager or FakeWorkspaceManager(
        root_path=tmp_path or Path("/tmp/symphony-test")
    )

    def _default_loader() -> EffectiveConfig | None:
        return config

    return Orchestrator(
        state=state,
        mailbox=mailbox,
        retry_queue=retry_queue,
        tracker=tracker,
        codex_client=codex_client,
        workspace_manager=workspace_manager,
        prompt_template="prompt for {{ issue.identifier }}",
        config=config,
        workflow_loader=workflow_loader or _default_loader,
    )


# ---------------------------------------------------------------------------
# on_tick — happy path: dispatch eligible issues
# ---------------------------------------------------------------------------


async def test_on_tick_dispatches_eligible_candidates(tmp_path: Path) -> None:
    candidates = [_issue(id="a"), _issue(id="b")]
    tracker = FakeTracker(candidates=candidates)
    orch = _make_orchestrator(tracker=tracker, tmp_path=tmp_path)

    await orch.on_tick()

    assert set(orch.state.running.keys()) == {"a", "b"}
    # Cleanup spawned worker tasks.
    await orch.shutdown_workers()


async def test_on_tick_reschedules_next_tick(tmp_path: Path) -> None:
    """Next tick handle is recorded on self for shutdown cancellation."""
    orch = _make_orchestrator(
        tracker=FakeTracker(), tmp_path=tmp_path,
        config=_make_config(poll_ms=10_000),
    )

    await orch.on_tick()

    handle = orch._next_tick_handle  # noqa: SLF001 -- test inspects internal
    assert handle is not None
    assert not handle.cancelled()
    handle.cancel()


# ---------------------------------------------------------------------------
# on_tick — fetch failure → no dispatch, no crash
# ---------------------------------------------------------------------------


async def test_on_tick_fetch_failure_skips_dispatch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    tracker = FakeTracker()
    tracker.fail_next_candidates(LinearError("upstream 500"))
    orch = _make_orchestrator(tracker=tracker, tmp_path=tmp_path)

    with caplog.at_level(logging.WARNING, logger="river_gang.orchestrator.loop"):
        await orch.on_tick()

    assert orch.state.running == {}
    # Next tick still scheduled even after fetch failure.
    assert orch._next_tick_handle is not None  # noqa: SLF001
    assert any("fetch" in rec.message.lower() for rec in caplog.records), caplog.text
    orch._next_tick_handle.cancel()  # noqa: SLF001


# ---------------------------------------------------------------------------
# on_tick — workflow_loader returns None (validation failure)
# ---------------------------------------------------------------------------


async def test_on_tick_loader_returns_none_skips_dispatch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    tracker = FakeTracker(candidates=[_issue(id="a")])

    def _loader_none() -> EffectiveConfig | None:
        return None

    orch = _make_orchestrator(
        tracker=tracker, workflow_loader=_loader_none, tmp_path=tmp_path
    )

    with caplog.at_level(logging.ERROR, logger="river_gang.orchestrator.loop"):
        await orch.on_tick()

    assert orch.state.running == {}
    assert orch._next_tick_handle is not None  # noqa: SLF001
    orch._next_tick_handle.cancel()  # noqa: SLF001
    assert any(
        "workflow_loader" in rec.message or "validation" in rec.message.lower()
        for rec in caplog.records
    ), caplog.text


async def test_on_tick_loader_raises_skips_dispatch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    tracker = FakeTracker(candidates=[_issue(id="a")])

    def _loader_raises() -> EffectiveConfig | None:
        raise RuntimeError("loader explosion")

    orch = _make_orchestrator(
        tracker=tracker, workflow_loader=_loader_raises, tmp_path=tmp_path
    )

    with caplog.at_level(logging.ERROR, logger="river_gang.orchestrator.loop"):
        await orch.on_tick()

    assert orch.state.running == {}
    assert orch._next_tick_handle is not None  # noqa: SLF001
    orch._next_tick_handle.cancel()  # noqa: SLF001


# ---------------------------------------------------------------------------
# on_tick — defensive reload swaps config
# ---------------------------------------------------------------------------


async def test_on_tick_reloaded_config_replaces_self_config(tmp_path: Path) -> None:
    initial = _make_config(poll_ms=30_000)
    new = _make_config(poll_ms=5_000, max_concurrent=2)
    states = iter([initial, new])

    def _switching_loader() -> EffectiveConfig | None:
        return next(states, new)

    tracker = FakeTracker(candidates=[_issue(id="a")])
    orch = _make_orchestrator(
        tracker=tracker,
        config=initial,
        workflow_loader=_switching_loader,
        tmp_path=tmp_path,
    )

    # First tick consumes `initial`.
    await orch.on_tick()
    await orch.shutdown_workers()
    orch._next_tick_handle.cancel()  # noqa: SLF001
    assert orch.config is initial  # first call returned same config

    # Second tick — loader returns `new`, swap fires.
    await orch.on_tick()
    assert orch.config is new
    await orch.shutdown_workers()
    if orch._next_tick_handle is not None:  # noqa: SLF001
        orch._next_tick_handle.cancel()  # noqa: SLF001


# ---------------------------------------------------------------------------
# on_tick — stall detection terminates worker + posts WorkerExit
# ---------------------------------------------------------------------------


async def test_on_tick_stall_detection_cancels_worker_and_posts_exit(
    tmp_path: Path,
) -> None:
    config = _make_config(stall_ms=1_000, max_concurrent=2)
    state = OrchestratorState(
        poll_interval_ms=config.polling.interval_ms,
        max_concurrent_agents=config.agent.max_concurrent_agents,
    )
    stalled_issue = _issue(id="stalled", state="In Progress")
    # started long enough ago to trip stall detection.
    entry = _running_entry(
        stalled_issue,
        started_at=datetime.now(UTC) - timedelta(seconds=60),
    )
    state.add_running(entry)

    orch = _make_orchestrator(
        state=state, config=config, tmp_path=tmp_path,
        tracker=FakeTracker(),
    )

    await orch.on_tick()
    # Give the cancellation a chance to propagate.
    await asyncio.sleep(0)

    assert entry.worker_handle.cancelled() or entry.worker_handle.done()

    # WorkerExit synthesised onto the mailbox.
    msg = await asyncio.wait_for(orch.mailbox.recv(), timeout=0.5)
    assert isinstance(msg, WorkerExit)
    assert msg.issue_id == "stalled"
    assert msg.ok is False
    assert msg.reason == "stall_terminated"

    orch._next_tick_handle.cancel()  # noqa: SLF001


# ---------------------------------------------------------------------------
# on_tick — reconcile terminal: cleanup scheduled (fire-and-forget)
# ---------------------------------------------------------------------------


async def test_on_tick_reconcile_terminal_schedules_cleanup(tmp_path: Path) -> None:
    config = _make_config(max_concurrent=2)
    state = OrchestratorState(
        poll_interval_ms=config.polling.interval_ms,
        max_concurrent_agents=config.agent.max_concurrent_agents,
    )
    issue = _issue(id="done", state="In Progress")
    entry = _running_entry(issue)
    state.add_running(entry)
    state.mark_claimed("done")

    workspace_manager = FakeWorkspaceManager(root_path=tmp_path)
    # Mark the workspace as existing so cleanup_for_issue actually records it.
    workspace_manager._existing.add(issue.identifier)  # noqa: SLF001

    tracker = FakeTracker(state_refreshes={"done": "Done"})
    orch = _make_orchestrator(
        state=state, config=config,
        tracker=tracker, workspace_manager=workspace_manager,
        tmp_path=tmp_path,
    )

    await orch.on_tick()
    # Fire-and-forget cleanup task — yield until it runs.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Worker cancelled.
    assert entry.worker_handle.cancelled() or entry.worker_handle.done()

    # cleanup_for_issue invoked on the workspace.
    assert any(
        call[0] == "cleanup_for_issue" and call[1]["identifier"] == issue.identifier
        for call in workspace_manager.calls
    ), workspace_manager.calls

    orch._next_tick_handle.cancel()  # noqa: SLF001


async def test_on_tick_reconcile_active_updates_snapshot(tmp_path: Path) -> None:
    config = _make_config(max_concurrent=2)
    state = OrchestratorState(
        poll_interval_ms=config.polling.interval_ms,
        max_concurrent_agents=config.agent.max_concurrent_agents,
    )
    issue = _issue(id="a", state="Todo")
    entry = _running_entry(issue)
    state.add_running(entry)

    tracker = FakeTracker(state_refreshes={"a": "In Progress"})
    orch = _make_orchestrator(
        state=state, config=config, tracker=tracker, tmp_path=tmp_path
    )

    await orch.on_tick()

    assert entry.issue.state == "In Progress"
    # Worker still alive — snapshot update doesn't terminate.
    assert not entry.worker_handle.done()

    await orch.shutdown_workers()
    orch._next_tick_handle.cancel()  # noqa: SLF001


async def test_on_tick_reconcile_refresh_failure_keeps_workers(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = _make_config(max_concurrent=2)
    state = OrchestratorState(
        poll_interval_ms=config.polling.interval_ms,
        max_concurrent_agents=config.agent.max_concurrent_agents,
    )
    issue = _issue(id="a", state="Todo")
    entry = _running_entry(issue)
    state.add_running(entry)

    tracker = FakeTracker()
    tracker.fail_next_state_refreshes(LinearError("refresh failed"))
    orch = _make_orchestrator(
        state=state, config=config, tracker=tracker, tmp_path=tmp_path
    )

    with caplog.at_level(logging.WARNING, logger="river_gang.orchestrator.loop"):
        await orch.on_tick()

    # Worker NOT terminated — keep running per task spec.
    assert not entry.worker_handle.done()
    assert "a" in orch.state.running
    assert any("refresh" in rec.message.lower() for rec in caplog.records), caplog.text

    await orch.shutdown_workers()
    orch._next_tick_handle.cancel()  # noqa: SLF001


# ---------------------------------------------------------------------------
# on_tick — slot exhaustion
# ---------------------------------------------------------------------------


async def test_on_tick_slot_exhaustion_breaks_loop(tmp_path: Path) -> None:
    """Global cap=1 with one already running → no new dispatches."""
    config = _make_config(max_concurrent=1)
    state = OrchestratorState(
        poll_interval_ms=config.polling.interval_ms,
        max_concurrent_agents=config.agent.max_concurrent_agents,
    )
    running = _issue(id="incumbent", state="In Progress")
    state.add_running(_running_entry(running))

    candidates = [_issue(id=f"c{i}", state="Todo") for i in range(5)]
    tracker = FakeTracker(
        candidates=candidates,
        state_refreshes={"incumbent": "In Progress"},
    )
    orch = _make_orchestrator(
        state=state, config=config, tracker=tracker, tmp_path=tmp_path
    )

    await orch.on_tick()

    # Only the original incumbent — no new dispatches.
    assert set(orch.state.running.keys()) == {"incumbent"}

    await orch.shutdown_workers()
    orch._next_tick_handle.cancel()  # noqa: SLF001


# ---------------------------------------------------------------------------
# run() — drains mailbox and exits on Shutdown
# ---------------------------------------------------------------------------


async def test_run_processes_poll_tick_then_shutdown(tmp_path: Path) -> None:
    tracker = FakeTracker(candidates=[_issue(id="a")])
    orch = _make_orchestrator(tracker=tracker, tmp_path=tmp_path)

    await orch.mailbox.send(PollTick())
    await orch.mailbox.send(Shutdown())

    await asyncio.wait_for(orch.run(), timeout=2.0)

    # Tick processed → dispatched.
    assert "a" in orch.state.running

    await orch.shutdown_workers()
    if orch._next_tick_handle is not None:  # noqa: SLF001
        orch._next_tick_handle.cancel()  # noqa: SLF001


async def test_run_routes_codex_update_to_handler(tmp_path: Path) -> None:
    config = _make_config()
    state = OrchestratorState(
        poll_interval_ms=config.polling.interval_ms,
        max_concurrent_agents=config.agent.max_concurrent_agents,
    )
    issue = _issue(id="a", state="In Progress")
    entry = _running_entry(issue)
    state.add_running(entry)

    orch = _make_orchestrator(state=state, config=config, tmp_path=tmp_path)

    event = RuntimeEvent(
        event="agent_message",
        timestamp=datetime.now(UTC),
        codex_app_server_pid=42,
        payload={"message": "hi"},
        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )
    await orch.mailbox.send(CodexUpdate(issue_id="a", event=event))
    await orch.mailbox.send(Shutdown())

    await asyncio.wait_for(orch.run(), timeout=2.0)

    assert orch.state.codex_totals == TokenSnapshot(10, 5, 15)
    assert entry.last_codex_event == "agent_message"
    assert entry.last_codex_message == "hi"

    await orch.shutdown_workers()


async def test_run_routes_worker_exit_to_handler(tmp_path: Path) -> None:
    config = _make_config()
    state = OrchestratorState(
        poll_interval_ms=config.polling.interval_ms,
        max_concurrent_agents=config.agent.max_concurrent_agents,
    )
    issue = _issue(id="a", state="In Progress")
    entry = _running_entry(issue)
    state.add_running(entry)
    state.mark_claimed("a")

    orch = _make_orchestrator(state=state, config=config, tmp_path=tmp_path)

    await orch.mailbox.send(
        WorkerExit(
            issue_id="a", reason="normal", ok=True, runtime_seconds=1.5,
        )
    )
    await orch.mailbox.send(Shutdown())
    await asyncio.wait_for(orch.run(), timeout=2.0)

    assert "a" not in orch.state.running
    assert "a" in orch.state.completed
    assert "a" in orch.retry_queue
    orch.retry_queue.cancel("a")


async def test_run_routes_retry_timer_to_handler(tmp_path: Path) -> None:
    """RetryTimerFired triggers fetch_candidates path; eligible → dispatch."""
    config = _make_config()
    state = OrchestratorState(
        poll_interval_ms=config.polling.interval_ms,
        max_concurrent_agents=config.agent.max_concurrent_agents,
    )
    candidate = _issue(id="a", state="Todo")
    state.mark_claimed("a")

    orch = _make_orchestrator(
        state=state, config=config,
        tracker=FakeTracker(candidates=[candidate]),
        tmp_path=tmp_path,
    )

    # Schedule a retry first so on_retry_timer has an entry to pop.
    orch.retry_queue.schedule(
        issue_id="a",
        attempt=2,
        kind="failure",
        max_cap_ms=300_000,
        on_fire=lambda _id: None,
        last_error=None,
    )

    await orch.mailbox.send(RetryTimerFired(issue_id="a"))
    await orch.mailbox.send(Shutdown())

    await asyncio.wait_for(orch.run(), timeout=2.0)

    # Retry path dispatched the issue (slots available, eligible).
    assert "a" in orch.state.running

    await orch.shutdown_workers()


async def test_run_routes_config_reloaded(tmp_path: Path) -> None:
    from river_gang.orchestrator import ConfigReloaded

    initial = _make_config(poll_ms=30_000)
    new = _make_config(poll_ms=5_000)
    orch = _make_orchestrator(config=initial, tmp_path=tmp_path)

    await orch.mailbox.send(ConfigReloaded(config=new))
    await orch.mailbox.send(Shutdown())
    await asyncio.wait_for(orch.run(), timeout=2.0)

    assert orch.config is new


async def test_config_reloaded_updates_orchestrator_state_caps(
    tmp_path: Path,
) -> None:
    """Reload must propagate the new ``polling.interval_ms`` and
    ``agent.max_concurrent_agents`` onto :class:`OrchestratorState` so the
    next tick + the next concurrency check pick them up."""
    from river_gang.orchestrator import ConfigReloaded

    initial = _make_config(poll_ms=30_000, max_concurrent=2)
    new = _make_config(poll_ms=5_000, max_concurrent=7)
    orch = _make_orchestrator(config=initial, tmp_path=tmp_path)

    assert orch.state.poll_interval_ms == 30_000
    assert orch.state.max_concurrent_agents == 2

    await orch.mailbox.send(ConfigReloaded(config=new))
    await orch.mailbox.send(Shutdown())
    await asyncio.wait_for(orch.run(), timeout=2.0)

    assert orch.config is new
    assert orch.state.poll_interval_ms == 5_000
    assert orch.state.max_concurrent_agents == 7


# ---------------------------------------------------------------------------
# schedule_initial_tick + cancel_next_tick
# ---------------------------------------------------------------------------


async def test_schedule_initial_tick_posts_polltick(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path=tmp_path)
    orch.schedule_initial_tick()

    msg = await asyncio.wait_for(orch.mailbox.recv(), timeout=0.5)
    assert isinstance(msg, PollTick)


async def test_cancel_next_tick_clears_handle(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path=tmp_path)
    await orch.on_tick()
    assert orch._next_tick_handle is not None  # noqa: SLF001

    orch.cancel_next_tick()
    assert orch._next_tick_handle is None  # noqa: SLF001
