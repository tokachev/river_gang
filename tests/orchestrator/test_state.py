"""Tests for :mod:`river_gang.orchestrator.state` (SPED §4.1.6, §4.1.8, §16.4)."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime

import pytest

from river_gang.codex import RateLimitSnapshot, RuntimeEvent, TokenSnapshot
from river_gang.orchestrator import OrchestratorState, RunningEntry
from river_gang.tracker.issue import Issue


def _make_issue(
    *,
    issue_id: str = "issue-1",
    identifier: str = "MT-1",
    state: str = "In Progress",
) -> Issue:
    return Issue(
        id=issue_id,
        identifier=identifier,
        title="title",
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


async def _noop() -> None:
    return None


def _make_entry(
    *,
    issue: Issue | None = None,
    worker_handle: asyncio.Task[None] | None = None,
) -> RunningEntry:
    if issue is None:
        issue = _make_issue()
    return RunningEntry(
        worker_handle=worker_handle,  # type: ignore[arg-type]
        monitor_handle=None,
        identifier=issue.identifier,
        issue=issue,
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


# ---------------------------------------------------------------------------
# Dataclass shape
# ---------------------------------------------------------------------------


def test_orchestrator_state_default_shape() -> None:
    state = OrchestratorState(poll_interval_ms=2000, max_concurrent_agents=3)
    assert state.running == {}
    assert state.claimed == set()
    assert state.retry_attempts == {}
    assert state.completed == set()
    assert state.codex_totals == TokenSnapshot(
        input_tokens=0, output_tokens=0, total_tokens=0
    )
    assert state.codex_rate_limits is None
    assert state.poll_interval_ms == 2000
    assert state.max_concurrent_agents == 3
    assert state.runtime_seconds_total == 0.0


def test_running_entry_has_all_required_fields() -> None:
    issue = _make_issue()
    entry = _make_entry(issue=issue)
    assert entry.worker_handle is None  # placeholder in test
    assert entry.monitor_handle is None
    assert entry.identifier == issue.identifier
    assert entry.issue is issue
    assert entry.session_id is None
    assert entry.last_reported_input_tokens == 0
    assert entry.last_reported_output_tokens == 0
    assert entry.last_reported_total_tokens == 0
    assert isinstance(entry.started_at, datetime)
    assert entry.started_at.tzinfo is not None
    assert entry.last_codex_timestamp is None
    assert entry.last_codex_event is None
    assert entry.last_codex_message is None
    assert isinstance(entry.recent_events, deque)
    assert entry.recent_events.maxlen == 50
    assert entry.last_error is None
    assert entry.restart_count == 0
    assert entry.retry_attempt == 0


def test_orchestrator_state_accepts_rate_limits_and_totals() -> None:
    totals = TokenSnapshot(input_tokens=10, output_tokens=20, total_tokens=30)
    rl = RateLimitSnapshot(limit=100, remaining=99, reset_at="2026-04-29T00:00:00Z")
    state = OrchestratorState(
        poll_interval_ms=1000,
        max_concurrent_agents=2,
        codex_totals=totals,
        codex_rate_limits=rl,
    )
    assert state.codex_totals is totals
    assert state.codex_rate_limits is rl


# ---------------------------------------------------------------------------
# recent_events ring buffer
# ---------------------------------------------------------------------------


def test_recent_events_ring_buffer_keeps_last_50() -> None:
    entry = _make_entry()
    for i in range(60):
        entry.recent_events.append(
            RuntimeEvent(
                event=f"evt-{i}",
                timestamp=datetime.now(UTC),
                codex_app_server_pid=123,
                payload={"i": i},
            )
        )
    assert len(entry.recent_events) == 50
    # Last event must be evt-59; oldest retained is evt-10.
    assert entry.recent_events[0].event == "evt-10"
    assert entry.recent_events[-1].event == "evt-59"


# ---------------------------------------------------------------------------
# Helpers / mutations
# ---------------------------------------------------------------------------


def test_available_slots_returns_max_minus_running() -> None:
    state = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=3)
    assert state.available_slots() == 3
    state.add_running(_make_entry(issue=_make_issue(issue_id="a", identifier="MT-A")))
    assert state.available_slots() == 2
    state.add_running(_make_entry(issue=_make_issue(issue_id="b", identifier="MT-B")))
    assert state.available_slots() == 1
    state.add_running(_make_entry(issue=_make_issue(issue_id="c", identifier="MT-C")))
    assert state.available_slots() == 0


def test_count_in_state_is_case_insensitive() -> None:
    state = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=5)
    state.add_running(
        _make_entry(issue=_make_issue(issue_id="a", identifier="MT-A", state="In Progress"))
    )
    state.add_running(
        _make_entry(issue=_make_issue(issue_id="b", identifier="MT-B", state="in progress"))
    )
    state.add_running(
        _make_entry(issue=_make_issue(issue_id="c", identifier="MT-C", state="Todo"))
    )
    assert state.count_in_state("in progress") == 2
    assert state.count_in_state("IN PROGRESS") == 2
    assert state.count_in_state("todo") == 1
    assert state.count_in_state("done") == 0


def test_is_claimed_reflects_claimed_set() -> None:
    state = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=2)
    assert state.is_claimed("a") is False
    state.mark_claimed("a")
    assert state.is_claimed("a") is True


def test_mark_claimed_unclaim_round_trip() -> None:
    state = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=2)
    state.mark_claimed("a")
    state.mark_claimed("b")
    assert state.claimed == {"a", "b"}
    state.unclaim("a")
    assert state.claimed == {"b"}
    # Unclaiming an absent id is a no-op.
    state.unclaim("missing")
    assert state.claimed == {"b"}


def test_add_running_remove_running_round_trip() -> None:
    state = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=2)
    issue = _make_issue(issue_id="abc", identifier="MT-1")
    entry = _make_entry(issue=issue)
    state.add_running(entry)
    assert state.running == {"abc": entry}
    removed = state.remove_running("abc")
    assert removed is entry
    assert state.running == {}
    # Removing missing returns None.
    assert state.remove_running("nope") is None


def test_record_completed_adds_to_completed() -> None:
    state = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=2)
    state.record_completed("a")
    state.record_completed("b")
    state.record_completed("a")  # idempotent
    assert state.completed == {"a", "b"}


def test_add_runtime_seconds_accumulates() -> None:
    state = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=2)
    state.add_runtime_seconds(1.5)
    state.add_runtime_seconds(2.25)
    assert state.runtime_seconds_total == pytest.approx(3.75)


# ---------------------------------------------------------------------------
# identifier_index — id ↔ identifier survives remove_running so retry/
# completed lookups via /api/v1/<identifier> still resolve (Task 43).
# ---------------------------------------------------------------------------


def test_identifier_index_populated_on_add_running() -> None:
    state = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=2)
    state.add_running(_make_entry(issue=_make_issue(issue_id="x", identifier="MT-X")))
    assert state.identifier_index == {"x": "MT-X"}


def test_identifier_index_persists_after_remove_running() -> None:
    """retry-only / completed-only lookups need historical identifier."""
    state = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=2)
    state.add_running(_make_entry(issue=_make_issue(issue_id="x", identifier="MT-X")))
    state.remove_running("x")
    # identifier_index keeps the mapping so /api/v1/MT-X still resolves
    # while the issue is in retry / completed.
    assert state.identifier_index == {"x": "MT-X"}


def test_identifier_index_overwrites_on_re_add() -> None:
    state = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=2)
    state.add_running(_make_entry(issue=_make_issue(issue_id="x", identifier="MT-X")))
    # Tracker rename — identifier changes but issue_id is stable.
    state.add_running(_make_entry(issue=_make_issue(issue_id="x", identifier="MT-Y")))
    assert state.identifier_index["x"] == "MT-Y"


def test_identifier_index_default_empty() -> None:
    state = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=2)
    assert state.identifier_index == {}
