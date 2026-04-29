"""Tests for ``on_codex_update`` in :mod:`river_gang.orchestrator.lifecycle`
(SPED §7.3, §13.5)."""

from __future__ import annotations

import logging
from collections import deque
from datetime import UTC, datetime
from typing import Any

import pytest

from river_gang.codex import RateLimitSnapshot, RuntimeEvent, TokenSnapshot
from river_gang.orchestrator import (
    CodexUpdate,
    OrchestratorState,
    RunningEntry,
)
from river_gang.orchestrator.lifecycle import on_codex_update
from river_gang.tracker.issue import Issue


def _issue(*, id: str = "iss-1", state: str = "In Progress") -> Issue:
    return Issue(
        id=id,
        identifier=f"MT-{id}",
        title="t",
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


def _entry(
    *,
    issue: Issue | None = None,
    session_id: str | None = None,
    last_in: int = 0,
    last_out: int = 0,
    last_total: int = 0,
) -> RunningEntry:
    issue = issue or _issue()
    return RunningEntry(
        worker_handle=None,  # type: ignore[arg-type]
        monitor_handle=None,
        identifier=issue.identifier,
        issue=issue,
        session_id=session_id,
        last_reported_input_tokens=last_in,
        last_reported_output_tokens=last_out,
        last_reported_total_tokens=last_total,
        started_at=datetime.now(UTC),
        last_codex_timestamp=None,
        last_codex_event=None,
        last_codex_message=None,
        recent_events=deque(maxlen=50),
        last_error=None,
        restart_count=0,
        retry_attempt=0,
    )


def _event(
    *,
    name: str = "agent_message",
    payload: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    timestamp: datetime | None = None,
) -> RuntimeEvent:
    return RuntimeEvent(
        event=name,
        timestamp=timestamp or datetime.now(UTC),
        codex_app_server_pid=42,
        payload=payload or {},
        usage=usage,
    )


@pytest.fixture
def state() -> OrchestratorState:
    return OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=3)


# ---------------------------------------------------------------------------
# Missing entry → silent no-op
# ---------------------------------------------------------------------------


def test_missing_running_entry_is_silent_noop(
    state: OrchestratorState, caplog: pytest.LogCaptureFixture
) -> None:
    pre_totals = state.codex_totals
    pre_rate_limits = state.codex_rate_limits

    with caplog.at_level(logging.DEBUG, logger="river_gang.orchestrator.lifecycle"):
        on_codex_update(
            state,
            message=CodexUpdate(issue_id="ghost", event=_event(usage={
                "input_tokens": 10,
                "output_tokens": 20,
                "total_tokens": 30,
            })),
        )

    assert "ghost" not in state.running
    # No mutations to globals.
    assert state.codex_totals == pre_totals
    assert state.codex_rate_limits == pre_rate_limits


# ---------------------------------------------------------------------------
# last_codex_event / last_codex_timestamp / last_codex_message
# ---------------------------------------------------------------------------


def test_updates_last_event_timestamp_and_message_from_message_field(
    state: OrchestratorState,
) -> None:
    entry = _entry()
    state.add_running(entry)
    ts = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)

    on_codex_update(
        state,
        message=CodexUpdate(
            issue_id="iss-1",
            event=_event(
                name="agent_message",
                timestamp=ts,
                payload={"message": "hello world"},
            ),
        ),
    )

    assert entry.last_codex_event == "agent_message"
    assert entry.last_codex_timestamp == ts
    assert entry.last_codex_message == "hello world"


def test_updates_last_message_from_text_field(
    state: OrchestratorState,
) -> None:
    entry = _entry()
    state.add_running(entry)

    on_codex_update(
        state,
        message=CodexUpdate(
            issue_id="iss-1",
            event=_event(payload={"text": "tool output"}),
        ),
    )

    assert entry.last_codex_message == "tool output"


def test_payload_without_message_or_text_leaves_last_message_unchanged(
    state: OrchestratorState,
) -> None:
    entry = _entry()
    entry.last_codex_message = "previous"
    state.add_running(entry)

    on_codex_update(
        state,
        message=CodexUpdate(
            issue_id="iss-1",
            event=_event(payload={"other_field": "x"}),
        ),
    )

    assert entry.last_codex_message == "previous"


# ---------------------------------------------------------------------------
# session_id update from thread_id + turn_id (per-turn semantics)
# ---------------------------------------------------------------------------


def test_session_id_updated_when_thread_and_turn_present(
    state: OrchestratorState,
) -> None:
    entry = _entry()
    state.add_running(entry)

    on_codex_update(
        state,
        message=CodexUpdate(
            issue_id="iss-1",
            event=_event(
                payload={"thread_id": "thr-A", "turn_id": "turn-1"},
            ),
        ),
    )

    assert entry.session_id == "thr-A-turn-1"


def test_session_id_updated_per_turn_subsequent_event(
    state: OrchestratorState,
) -> None:
    entry = _entry(session_id="thr-A-turn-1")
    state.add_running(entry)

    on_codex_update(
        state,
        message=CodexUpdate(
            issue_id="iss-1",
            event=_event(
                payload={"thread_id": "thr-A", "turn_id": "turn-2"},
            ),
        ),
    )

    # Per-turn semantics: session_id must reflect the latest turn, not stay
    # pinned to the first one (otherwise dashboards show stale ids).
    assert entry.session_id == "thr-A-turn-2"


def test_session_id_camel_case_aliases(
    state: OrchestratorState,
) -> None:
    entry = _entry()
    state.add_running(entry)

    on_codex_update(
        state,
        message=CodexUpdate(
            issue_id="iss-1",
            event=_event(payload={"threadId": "thr-B", "turnId": "turn-9"}),
        ),
    )

    assert entry.session_id == "thr-B-turn-9"


def test_session_id_unchanged_when_only_one_id_present(
    state: OrchestratorState,
) -> None:
    entry = _entry(session_id="prev")
    state.add_running(entry)

    on_codex_update(
        state,
        message=CodexUpdate(
            issue_id="iss-1",
            event=_event(payload={"thread_id": "thr-A"}),
        ),
    )
    assert entry.session_id == "prev"

    on_codex_update(
        state,
        message=CodexUpdate(
            issue_id="iss-1",
            event=_event(payload={"turn_id": "turn-1"}),
        ),
    )
    assert entry.session_id == "prev"


# ---------------------------------------------------------------------------
# recent_events deque (maxlen=50)
# ---------------------------------------------------------------------------


def test_recent_events_appended_and_capped(
    state: OrchestratorState,
) -> None:
    entry = _entry()
    state.add_running(entry)

    for i in range(60):
        on_codex_update(
            state,
            message=CodexUpdate(
                issue_id="iss-1",
                event=_event(name=f"evt-{i}"),
            ),
        )

    assert len(entry.recent_events) == 50
    # Oldest dropped: first kept event is evt-10, last is evt-59.
    assert entry.recent_events[0].event == "evt-10"
    assert entry.recent_events[-1].event == "evt-59"


# ---------------------------------------------------------------------------
# Token delta accumulation
# ---------------------------------------------------------------------------


def test_first_event_with_usage_sets_totals_and_last_reported(
    state: OrchestratorState,
) -> None:
    entry = _entry()
    state.add_running(entry)

    on_codex_update(
        state,
        message=CodexUpdate(
            issue_id="iss-1",
            event=_event(usage={
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
            }),
        ),
    )

    assert state.codex_totals == TokenSnapshot(
        input_tokens=100, output_tokens=50, total_tokens=150
    )
    assert entry.last_reported_input_tokens == 100
    assert entry.last_reported_output_tokens == 50
    assert entry.last_reported_total_tokens == 150


def test_second_event_adds_only_delta(
    state: OrchestratorState,
) -> None:
    entry = _entry()
    state.add_running(entry)

    on_codex_update(
        state,
        message=CodexUpdate(
            issue_id="iss-1",
            event=_event(usage={
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
            }),
        ),
    )
    on_codex_update(
        state,
        message=CodexUpdate(
            issue_id="iss-1",
            event=_event(usage={
                "input_tokens": 130,
                "output_tokens": 80,
                "total_tokens": 210,
            }),
        ),
    )

    # Globals reflect the latest absolute (no double-count).
    assert state.codex_totals == TokenSnapshot(
        input_tokens=130, output_tokens=80, total_tokens=210
    )
    assert entry.last_reported_input_tokens == 130
    assert entry.last_reported_output_tokens == 80
    assert entry.last_reported_total_tokens == 210


def test_two_running_entries_independent_deltas(
    state: OrchestratorState,
) -> None:
    a = _entry(issue=_issue(id="a"))
    b = _entry(issue=_issue(id="b"))
    state.add_running(a)
    state.add_running(b)

    on_codex_update(
        state,
        message=CodexUpdate(
            issue_id="a",
            event=_event(usage={
                "input_tokens": 10,
                "output_tokens": 20,
                "total_tokens": 30,
            }),
        ),
    )
    on_codex_update(
        state,
        message=CodexUpdate(
            issue_id="b",
            event=_event(usage={
                "input_tokens": 5,
                "output_tokens": 15,
                "total_tokens": 20,
            }),
        ),
    )

    assert state.codex_totals == TokenSnapshot(
        input_tokens=15, output_tokens=35, total_tokens=50
    )


def test_event_without_usage_leaves_totals_unchanged(
    state: OrchestratorState,
) -> None:
    entry = _entry(last_in=10, last_out=20, last_total=30)
    state.add_running(entry)
    state.codex_totals = TokenSnapshot(
        input_tokens=10, output_tokens=20, total_tokens=30
    )

    on_codex_update(
        state,
        message=CodexUpdate(
            issue_id="iss-1",
            event=_event(usage=None),
        ),
    )

    assert state.codex_totals == TokenSnapshot(
        input_tokens=10, output_tokens=20, total_tokens=30
    )
    # Per-entry counters untouched too.
    assert entry.last_reported_input_tokens == 10
    assert entry.last_reported_output_tokens == 20
    assert entry.last_reported_total_tokens == 30


def test_usage_regression_is_clamped_to_zero(
    state: OrchestratorState,
) -> None:
    """Server reset / out-of-order event → never subtract from totals."""
    entry = _entry()
    state.add_running(entry)

    on_codex_update(
        state,
        message=CodexUpdate(
            issue_id="iss-1",
            event=_event(usage={
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
            }),
        ),
    )
    # Server "resets" to lower absolute totals.
    on_codex_update(
        state,
        message=CodexUpdate(
            issue_id="iss-1",
            event=_event(usage={
                "input_tokens": 40,
                "output_tokens": 20,
                "total_tokens": 60,
            }),
        ),
    )

    # Globals stay at the high-water mark (delta clamped to 0).
    assert state.codex_totals == TokenSnapshot(
        input_tokens=100, output_tokens=50, total_tokens=150
    )
    # last_reported updated to new absolutes so future increments work
    # against the new baseline.
    assert entry.last_reported_input_tokens == 40
    assert entry.last_reported_output_tokens == 20
    assert entry.last_reported_total_tokens == 60


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------


def test_rate_limits_set_when_payload_carries_rate_limit(
    state: OrchestratorState,
) -> None:
    entry = _entry()
    state.add_running(entry)

    on_codex_update(
        state,
        message=CodexUpdate(
            issue_id="iss-1",
            event=_event(payload={
                "rate_limit": {
                    "limit": 1000,
                    "remaining": 250,
                    "reset_at": "2026-04-28T13:00:00Z",
                },
            }),
        ),
    )

    assert state.codex_rate_limits == RateLimitSnapshot(
        limit=1000, remaining=250, reset_at="2026-04-28T13:00:00Z"
    )


def test_rate_limits_unchanged_when_payload_lacks_rate_limit(
    state: OrchestratorState,
) -> None:
    entry = _entry()
    state.add_running(entry)
    prior = RateLimitSnapshot(limit=500, remaining=100, reset_at="t")
    state.codex_rate_limits = prior

    on_codex_update(
        state,
        message=CodexUpdate(
            issue_id="iss-1",
            event=_event(payload={"message": "no rate-limit info here"}),
        ),
    )

    # Must NOT null out a prior snapshot just because this event lacks it.
    assert state.codex_rate_limits is prior
