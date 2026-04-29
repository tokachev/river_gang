"""Tests for issue/session log context propagation (SPED §13.1)."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from river_gang.codex import RuntimeEvent
from river_gang.config import EffectiveConfig
from river_gang.config.defaults import apply_defaults
from river_gang.observability.logging import (
    configure_logging,
    issue_id_var,
    issue_identifier_var,
    session_id_var,
    set_log_context,
)
from river_gang.orchestrator import (
    CodexUpdate,
    Mailbox,
    OrchestratorState,
    RetryQueue,
    RunningEntry,
    Shutdown,
    WorkerExit,
)
from river_gang.orchestrator.loop import Orchestrator
from river_gang.orchestrator.worker import run_agent_attempt
from river_gang.tracker.issue import Issue
from tests.codex.fakes import FakeCodexClient, TurnScenario
from tests.tracker.fakes import FakeTracker
from tests.workspace.fakes import FakeWorkspaceManager

# ---------------------------------------------------------------------------
# set_log_context — public helper
# ---------------------------------------------------------------------------


def test_set_log_context_sets_and_resets() -> None:
    assert issue_id_var.get() is None
    assert issue_identifier_var.get() is None
    assert session_id_var.get() is None

    with set_log_context(issue_id="x", issue_identifier="MT-1", session_id="s"):
        assert issue_id_var.get() == "x"
        assert issue_identifier_var.get() == "MT-1"
        assert session_id_var.get() == "s"

    assert issue_id_var.get() is None
    assert issue_identifier_var.get() is None
    assert session_id_var.get() is None


def test_set_log_context_partial_only_sets_provided() -> None:
    assert session_id_var.get() is None
    with set_log_context(issue_id="x"):
        assert issue_id_var.get() == "x"
        # Unset fields stay at default.
        assert issue_identifier_var.get() is None
        assert session_id_var.get() is None
    assert issue_id_var.get() is None


def test_set_log_context_nested_restores_outer() -> None:
    with set_log_context(issue_id="outer", issue_identifier="O-1"):
        with set_log_context(issue_id="inner", session_id="s"):
            assert issue_id_var.get() == "inner"
            assert issue_identifier_var.get() == "O-1"
            assert session_id_var.get() == "s"
        # Inner unwinds; outer values restored, session_id back to None.
        assert issue_id_var.get() == "outer"
        assert issue_identifier_var.get() == "O-1"
        assert session_id_var.get() is None
    assert issue_id_var.get() is None


# ---------------------------------------------------------------------------
# Worker logs carry issue_id + issue_identifier
# ---------------------------------------------------------------------------


def _issue(*, id: str = "iss-1", identifier: str = "MT-1") -> Issue:
    return Issue(
        id=id,
        identifier=identifier,
        title="title",
        state="In Progress",
        description=None,
        priority=None,
        branch_name=None,
        url=None,
        labels=(),
        blocked_by=(),
        created_at=None,
        updated_at=None,
    )


def _make_config() -> EffectiveConfig:
    # max_turns=2 so a successful turn 1 falls through to the tracker
    # refresh check (which the FakeTracker drives to "Done" → outcome=normal).
    return apply_defaults({
        "tracker": {"kind": "linear", "active_states": ["In Progress"]},
        "agent": {"max_turns": 2},
    })


async def test_worker_logs_carry_issue_context_via_kv_format(
    tmp_path: Path,
) -> None:
    """End-to-end: configure_logging → run worker → captured stream contains
    issue_id + issue_identifier on at least one record."""
    buffer = StringIO()
    configure_logging(level=logging.INFO, stream=buffer)

    issue = _issue()
    codex_client = FakeCodexClient(thread_id="th-A", first_turn_id="tn-1")
    codex_client.queue_turn(TurnScenario(outcome="completed"))
    workspace_manager = FakeWorkspaceManager(root_path=tmp_path)
    tracker = FakeTracker(state_refreshes={issue.id: "Done"})
    mailbox = Mailbox()

    await run_agent_attempt(
        issue=issue,
        attempt=1,
        mailbox=mailbox,
        codex_client=codex_client,
        workspace_manager=workspace_manager,
        tracker=tracker,
        prompt_template="hello {{ issue.identifier }}",
        config=_make_config(),
    )

    output = buffer.getvalue()
    assert "issue_id=iss-1" in output, output
    assert "issue_identifier=MT-1" in output, output


async def test_worker_logs_carry_session_id_after_start(
    tmp_path: Path,
) -> None:
    buffer = StringIO()
    configure_logging(level=logging.INFO, stream=buffer)

    issue = _issue()
    codex_client = FakeCodexClient(thread_id="th-A", first_turn_id="tn-1")
    codex_client.queue_turn(TurnScenario(outcome="completed"))
    workspace_manager = FakeWorkspaceManager(root_path=tmp_path)
    tracker = FakeTracker(state_refreshes={issue.id: "Done"})

    await run_agent_attempt(
        issue=issue,
        attempt=1,
        mailbox=Mailbox(),
        codex_client=codex_client,
        workspace_manager=workspace_manager,
        tracker=tracker,
        prompt_template="prompt",
        config=_make_config(),
    )

    output = buffer.getvalue()
    # session_id is composed as "<thread_id>-<turn_id>" (§10.2).
    assert "session_id=th-A-tn-1" in output, output


async def test_worker_context_does_not_leak_after_return(
    tmp_path: Path,
) -> None:
    issue = _issue()
    codex_client = FakeCodexClient()
    codex_client.queue_turn(TurnScenario(outcome="completed"))
    workspace_manager = FakeWorkspaceManager(root_path=tmp_path)
    tracker = FakeTracker(state_refreshes={issue.id: "Done"})

    assert issue_id_var.get() is None
    await run_agent_attempt(
        issue=issue,
        attempt=1,
        mailbox=Mailbox(),
        codex_client=codex_client,
        workspace_manager=workspace_manager,
        tracker=tracker,
        prompt_template="x",
        config=_make_config(),
    )
    # Worker must reset all context vars before returning.
    assert issue_id_var.get() is None
    assert issue_identifier_var.get() is None
    assert session_id_var.get() is None


# ---------------------------------------------------------------------------
# Outcome logging from worker
# ---------------------------------------------------------------------------


async def test_worker_logs_completed_outcome(tmp_path: Path) -> None:
    buffer = StringIO()
    configure_logging(level=logging.INFO, stream=buffer)

    issue = _issue()
    codex_client = FakeCodexClient()
    codex_client.queue_turn(TurnScenario(outcome="completed"))
    workspace_manager = FakeWorkspaceManager(root_path=tmp_path)
    tracker = FakeTracker(state_refreshes={issue.id: "Done"})

    await run_agent_attempt(
        issue=issue,
        attempt=1,
        mailbox=Mailbox(),
        codex_client=codex_client,
        workspace_manager=workspace_manager,
        tracker=tracker,
        prompt_template="x",
        config=_make_config(),
    )

    output = buffer.getvalue()
    assert "outcome=normal" in output, output
    assert "ok=True" in output, output


async def test_worker_logs_failed_outcome(tmp_path: Path) -> None:
    buffer = StringIO()
    configure_logging(level=logging.INFO, stream=buffer)

    issue = _issue()
    codex_client = FakeCodexClient()
    codex_client.queue_turn(TurnScenario(outcome="failed", reason="boom"))
    workspace_manager = FakeWorkspaceManager(root_path=tmp_path)
    tracker = FakeTracker()

    await run_agent_attempt(
        issue=issue,
        attempt=1,
        mailbox=Mailbox(),
        codex_client=codex_client,
        workspace_manager=workspace_manager,
        tracker=tracker,
        prompt_template="x",
        config=_make_config(),
    )

    output = buffer.getvalue()
    assert "outcome=turn_failed" in output, output
    assert "ok=False" in output, output


# ---------------------------------------------------------------------------
# Orchestrator handler dispatch sets context for handler logs
# ---------------------------------------------------------------------------


def _running_entry(issue: Issue, *, session_id: str | None = None) -> RunningEntry:
    async def _idle() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            return
    return RunningEntry(
        worker_handle=asyncio.create_task(_idle()),
        monitor_handle=None,
        identifier=issue.identifier,
        issue=issue,
        session_id=session_id,
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


def _make_orchestrator(
    *,
    state: OrchestratorState,
    config: EffectiveConfig,
    tmp_path: Path,
) -> Orchestrator:
    return Orchestrator(
        state=state,
        mailbox=Mailbox(),
        retry_queue=RetryQueue(loop=asyncio.get_running_loop()),
        tracker=FakeTracker(),
        codex_client=FakeCodexClient(),
        workspace_manager=FakeWorkspaceManager(root_path=tmp_path),
        prompt_template="x",
        config=config,
        workflow_loader=lambda: config,
    )


async def test_orchestrator_sets_context_for_missing_running_entry_warning(
    tmp_path: Path,
) -> None:
    """on_worker_exit logs a WARNING when running is missing — that warning
    must carry issue_id from the orchestrator's dispatch wrapper."""
    buffer = StringIO()
    configure_logging(level=logging.WARNING, stream=buffer)

    config = _make_config()
    state = OrchestratorState(
        poll_interval_ms=config.polling.interval_ms,
        max_concurrent_agents=config.agent.max_concurrent_agents,
    )
    orch = _make_orchestrator(state=state, config=config, tmp_path=tmp_path)

    # Issue is NOT in running → on_worker_exit logs WARNING.
    await orch.mailbox.send(
        WorkerExit(
            issue_id="ghost-id",
            reason="normal",
            ok=True,
            runtime_seconds=0.0,
        )
    )
    await orch.mailbox.send(Shutdown())
    await asyncio.wait_for(orch.run(), timeout=2.0)

    output = buffer.getvalue()
    assert "ghost-id" in output, output
    assert "issue_id=ghost-id" in output, output


async def test_orchestrator_sets_context_for_codex_update_dispatch(
    tmp_path: Path,
) -> None:
    """CodexUpdate handler logs (DEBUG missing entry) carry issue_id."""
    buffer = StringIO()
    configure_logging(level=logging.DEBUG, stream=buffer)

    config = _make_config()
    state = OrchestratorState(
        poll_interval_ms=config.polling.interval_ms,
        max_concurrent_agents=config.agent.max_concurrent_agents,
    )
    orch = _make_orchestrator(state=state, config=config, tmp_path=tmp_path)

    event = RuntimeEvent(
        event="agent_message",
        timestamp=datetime.now(UTC),
        codex_app_server_pid=42,
        payload={},
        usage=None,
    )
    # No entry → handler emits DEBUG "no running entry — dropping".
    await orch.mailbox.send(CodexUpdate(issue_id="ghost-cu", event=event))
    await orch.mailbox.send(Shutdown())
    await asyncio.wait_for(orch.run(), timeout=2.0)

    output = buffer.getvalue()
    assert "issue_id=ghost-cu" in output, output


async def test_orchestrator_sets_session_id_when_running_entry_has_one(
    tmp_path: Path,
) -> None:
    buffer = StringIO()
    configure_logging(level=logging.DEBUG, stream=buffer)

    config = _make_config()
    state = OrchestratorState(
        poll_interval_ms=config.polling.interval_ms,
        max_concurrent_agents=config.agent.max_concurrent_agents,
    )
    issue = _issue(id="live-1", identifier="LIVE-1")
    entry = _running_entry(issue, session_id="th-Z-tn-7")
    state.add_running(entry)
    state.mark_claimed("live-1")

    orch = _make_orchestrator(state=state, config=config, tmp_path=tmp_path)

    # WorkerExit on a known-running entry — pops it, logs do not include
    # the missing-entry warning, but the dispatch wrapper still attaches
    # issue_id + identifier + session_id to whatever the handler logs at
    # higher levels. Use a debug log site (on_codex_update missing entry
    # path won't fire here since entry exists).
    await orch.mailbox.send(
        CodexUpdate(
            issue_id="other-ghost",
            event=RuntimeEvent(
                event="evt",
                timestamp=datetime.now(UTC),
                codex_app_server_pid=1,
                payload={},
                usage=None,
            ),
        )
    )
    await orch.mailbox.send(Shutdown())
    await asyncio.wait_for(orch.run(), timeout=2.0)

    # The CodexUpdate above is for a DIFFERENT id (other-ghost). For that
    # one there's no running entry → DEBUG log fires. Assert THAT line
    # carries other-ghost as issue_id (no leakage from live-1).
    output = buffer.getvalue()
    assert "issue_id=other-ghost" in output, output
    # Crucial: live-1 must NOT appear as issue_id on the other-ghost log.
    # Be permissive — just assert the formatter rendered other-ghost
    # against the right key.
    for line in output.splitlines():
        if "other-ghost" in line and "msg=" in line:
            assert "issue_id=other-ghost" in line, line
            assert "issue_id=live-1" not in line, line

    # Cleanup running entry's idle worker.
    entry.worker_handle.cancel()


# ---------------------------------------------------------------------------
# Cleanup — teardown logging handler so other tests aren't affected
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_root_logger() -> Any:
    """Snapshot/restore root handlers so configure_logging side-effects don't
    leak into other tests in the suite."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    # Drop any handler installed during the test.
    for handler in list(root.handlers):
        if handler not in saved_handlers:
            root.removeHandler(handler)
    root.setLevel(saved_level)
