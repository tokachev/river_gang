"""Tests for ``build_snapshot`` (SPED §13.3, §13.7.2)."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from river_gang.codex import RateLimitSnapshot, RuntimeEvent, TokenSnapshot
from river_gang.observability import (
    RetryRow,
    RunningRow,
    Snapshot,
    build_snapshot,
)
from river_gang.orchestrator import OrchestratorState, RetryQueue, RunningEntry
from river_gang.tracker.issue import Issue


def _issue(
    *,
    id: str = "iss-1",
    identifier: str = "MT-1",
    state: str = "In Progress",
    title: str = "title",
    priority: int | None = 2,
) -> Issue:
    return Issue(
        id=id,
        identifier=identifier,
        title=title,
        state=state,
        description=None,
        priority=priority,
        branch_name=None,
        url=None,
        labels=(),
        blocked_by=(),
        created_at=None,
        updated_at=None,
    )


def _runtime_event(name: str) -> RuntimeEvent:
    return RuntimeEvent(
        event=name,
        timestamp=datetime.now(UTC),
        codex_app_server_pid=42,
        payload={},
        usage=None,
    )


def _running_entry(
    *,
    issue: Issue | None = None,
    session_id: str | None = None,
    started_at: datetime | None = None,
    last_codex_timestamp: datetime | None = None,
    last_codex_event: str | None = None,
    last_codex_message: str | None = None,
    last_error: str | None = None,
    restart_count: int = 0,
    last_in: int = 0,
    last_out: int = 0,
    last_total: int = 0,
    events: list[RuntimeEvent] | None = None,
) -> RunningEntry:
    issue = issue or _issue()
    rec: deque[RuntimeEvent] = deque(maxlen=50)
    if events is not None:
        rec.extend(events)
    return RunningEntry(
        worker_handle=None,  # type: ignore[arg-type]
        monitor_handle=None,
        identifier=issue.identifier,
        issue=issue,
        session_id=session_id,
        last_reported_input_tokens=last_in,
        last_reported_output_tokens=last_out,
        last_reported_total_tokens=last_total,
        started_at=started_at or datetime.now(UTC),
        last_codex_timestamp=last_codex_timestamp,
        last_codex_event=last_codex_event,
        last_codex_message=last_codex_message,
        recent_events=rec,
        last_error=last_error,
        restart_count=restart_count,
        retry_attempt=0,
    )


@pytest.fixture
def state() -> OrchestratorState:
    return OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=5)


@pytest.fixture
async def retry_queue() -> RetryQueue:
    return RetryQueue(loop=asyncio.get_running_loop())


def _no_op(_id: str) -> None:
    return None


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------


async def test_empty_state(state: OrchestratorState, retry_queue: RetryQueue) -> None:
    now = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)
    snap = build_snapshot(state, retry_queue=retry_queue, now=now)

    assert snap.generated_at == now
    assert snap.counts == {"running": 0, "retrying": 0, "completed": 0}
    assert snap.running == []
    assert snap.retrying == []
    assert snap.codex_totals == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "seconds_running": 0.0,
    }
    assert snap.rate_limits is None


# ---------------------------------------------------------------------------
# Single running entry — full row population
# ---------------------------------------------------------------------------


async def test_single_running_row_full_fields(
    state: OrchestratorState, retry_queue: RetryQueue
) -> None:
    started = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)
    last_evt_ts = datetime(2026, 4, 28, 12, 0, 30, tzinfo=UTC)
    issue = _issue(
        id="abc123",
        identifier="MT-649",
        state="In Progress",
        title="Implement feature",
        priority=1,
    )
    entry = _running_entry(
        issue=issue,
        session_id="thread-1-turn-1",
        started_at=started,
        last_codex_event="turn_completed",
        last_codex_timestamp=last_evt_ts,
        last_codex_message="ok",
        last_in=1200,
        last_out=800,
        last_total=2000,
        last_error=None,
        restart_count=0,
        events=[_runtime_event("turn_started"), _runtime_event("agent_message")],
    )
    state.add_running(entry)

    now = datetime(2026, 4, 28, 12, 1, 0, tzinfo=UTC)
    snap = build_snapshot(state, retry_queue=retry_queue, now=now)

    assert len(snap.running) == 1
    row = snap.running[0]
    assert isinstance(row, RunningRow)
    assert row.issue_id == "abc123"
    assert row.identifier == "MT-649"
    assert row.title == "Implement feature"
    assert row.state == "In Progress"
    assert row.priority == 1
    assert row.session_id == "thread-1-turn-1"
    assert row.started_at == started
    assert row.last_codex_event == "turn_completed"
    assert row.last_codex_timestamp == last_evt_ts
    assert row.last_error is None
    assert row.restart_count == 0
    assert row.last_reported_input_tokens == 1200
    assert row.last_reported_output_tokens == 800
    assert row.last_reported_total_tokens == 2000
    # turn_count counts only turn_started events.
    assert row.turn_count == 1


# ---------------------------------------------------------------------------
# turn_count counting rule
# ---------------------------------------------------------------------------


async def test_turn_count_counts_turn_started_events_only(
    state: OrchestratorState, retry_queue: RetryQueue
) -> None:
    entry = _running_entry(
        events=[
            _runtime_event("turn_started"),
            _runtime_event("agent_message"),
            _runtime_event("turn.start"),  # alias also counted
            _runtime_event("turn_completed"),
            _runtime_event("turn_started"),
        ]
    )
    state.add_running(entry)

    snap = build_snapshot(
        state, retry_queue=retry_queue, now=datetime.now(UTC)
    )
    # turn_started + turn.start + turn_started = 3
    assert snap.running[0].turn_count == 3


# ---------------------------------------------------------------------------
# Multiple running rows — preserve insertion order
# ---------------------------------------------------------------------------


async def test_multiple_running_rows_preserve_order(
    state: OrchestratorState, retry_queue: RetryQueue
) -> None:
    state.add_running(
        _running_entry(issue=_issue(id="a", identifier="MT-A"))
    )
    state.add_running(
        _running_entry(issue=_issue(id="b", identifier="MT-B"))
    )
    state.add_running(
        _running_entry(issue=_issue(id="c", identifier="MT-C"))
    )

    snap = build_snapshot(
        state, retry_queue=retry_queue, now=datetime.now(UTC)
    )

    assert [r.issue_id for r in snap.running] == ["a", "b", "c"]
    assert [r.identifier for r in snap.running] == ["MT-A", "MT-B", "MT-C"]


# ---------------------------------------------------------------------------
# Single retry row
# ---------------------------------------------------------------------------


async def test_single_retry_row(
    state: OrchestratorState, retry_queue: RetryQueue
) -> None:
    retry_queue.schedule(
        issue_id="def456",
        attempt=3,
        kind="failure",
        max_cap_ms=300_000,
        on_fire=_no_op,
        last_error="no available orchestrator slots",
    )

    snap = build_snapshot(
        state, retry_queue=retry_queue, now=datetime.now(UTC)
    )

    assert len(snap.retrying) == 1
    row = snap.retrying[0]
    assert isinstance(row, RetryRow)
    assert row.issue_id == "def456"
    # No corresponding running entry → identifier is None.
    assert row.identifier is None
    assert row.attempt == 3
    assert row.kind == "failure"
    assert row.last_error == "no available orchestrator slots"
    # fire_at is a UTC datetime in the future.
    assert row.fire_at.tzinfo is not None

    retry_queue.cancel("def456")


async def test_retry_row_identifier_pulled_from_running_entry(
    state: OrchestratorState, retry_queue: RetryQueue
) -> None:
    """If a RunningEntry exists for the same issue_id, surface its identifier."""
    state.add_running(
        _running_entry(issue=_issue(id="known", identifier="MT-Known"))
    )
    retry_queue.schedule(
        issue_id="known",
        attempt=1,
        kind="continuation",
        max_cap_ms=300_000,
        on_fire=_no_op,
    )

    snap = build_snapshot(
        state, retry_queue=retry_queue, now=datetime.now(UTC)
    )

    assert len(snap.retrying) == 1
    assert snap.retrying[0].identifier == "MT-Known"
    retry_queue.cancel("known")


async def test_retry_row_identifier_resolves_after_remove_running(
    state: OrchestratorState, retry_queue: RetryQueue
) -> None:
    """Retry rows are scheduled AFTER ``remove_running`` — the original
    identifier must still appear in the snapshot row via ``identifier_index``.
    """
    state.add_running(
        _running_entry(issue=_issue(id="iss-7", identifier="MT-Seven"))
    )
    # The orchestrator removes the running entry before scheduling a retry.
    state.remove_running("iss-7")
    retry_queue.schedule(
        issue_id="iss-7",
        attempt=2,
        kind="failure",
        max_cap_ms=300_000,
        on_fire=_no_op,
    )

    snap = build_snapshot(
        state, retry_queue=retry_queue, now=datetime.now(UTC)
    )

    assert len(snap.retrying) == 1
    assert snap.retrying[0].issue_id == "iss-7"
    assert snap.retrying[0].identifier == "MT-Seven"
    retry_queue.cancel("iss-7")


# ---------------------------------------------------------------------------
# Counts are accurate
# ---------------------------------------------------------------------------


async def test_counts_running_retrying_completed(
    state: OrchestratorState, retry_queue: RetryQueue
) -> None:
    state.add_running(_running_entry(issue=_issue(id="r1", identifier="MT-1")))
    state.add_running(_running_entry(issue=_issue(id="r2", identifier="MT-2")))
    retry_queue.schedule(
        issue_id="rt1", attempt=1, kind="failure",
        max_cap_ms=300_000, on_fire=_no_op,
    )
    state.record_completed("done-1")
    state.record_completed("done-2")
    state.record_completed("done-3")

    snap = build_snapshot(
        state, retry_queue=retry_queue, now=datetime.now(UTC)
    )

    assert snap.counts == {"running": 2, "retrying": 1, "completed": 3}
    retry_queue.cancel("rt1")


# ---------------------------------------------------------------------------
# seconds_running aggregates ended + active
# ---------------------------------------------------------------------------


async def test_seconds_running_includes_active_session_elapsed(
    state: OrchestratorState, retry_queue: RetryQueue
) -> None:
    now = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)
    started = now - timedelta(seconds=30)

    state.add_runtime_seconds(100.0)  # cumulative ended-session runtime
    state.add_running(_running_entry(started_at=started))

    snap = build_snapshot(state, retry_queue=retry_queue, now=now)

    assert snap.codex_totals["seconds_running"] == pytest.approx(130.0)


async def test_seconds_running_only_cumulative_when_no_active(
    state: OrchestratorState, retry_queue: RetryQueue
) -> None:
    state.add_runtime_seconds(42.5)
    snap = build_snapshot(
        state, retry_queue=retry_queue, now=datetime.now(UTC)
    )
    assert snap.codex_totals["seconds_running"] == pytest.approx(42.5)


async def test_seconds_running_clamps_negative_clock_skew(
    state: OrchestratorState, retry_queue: RetryQueue
) -> None:
    """If now < started_at (clock skew / fakes), don't subtract."""
    now = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)
    state.add_running(
        _running_entry(started_at=now + timedelta(seconds=10))
    )
    snap = build_snapshot(state, retry_queue=retry_queue, now=now)
    assert snap.codex_totals["seconds_running"] >= 0.0


# ---------------------------------------------------------------------------
# codex_totals reflect state.codex_totals
# ---------------------------------------------------------------------------


async def test_codex_totals_reflect_state_token_snapshot(
    state: OrchestratorState, retry_queue: RetryQueue
) -> None:
    state.codex_totals = TokenSnapshot(
        input_tokens=5000, output_tokens=2400, total_tokens=7400
    )
    snap = build_snapshot(
        state, retry_queue=retry_queue, now=datetime.now(UTC)
    )
    assert snap.codex_totals["input_tokens"] == 5000
    assert snap.codex_totals["output_tokens"] == 2400
    assert snap.codex_totals["total_tokens"] == 7400


# ---------------------------------------------------------------------------
# rate_limits passthrough
# ---------------------------------------------------------------------------


async def test_rate_limits_passthrough(
    state: OrchestratorState, retry_queue: RetryQueue
) -> None:
    state.codex_rate_limits = RateLimitSnapshot(
        limit=1000, remaining=250, reset_at="2026-04-28T13:00:00Z"
    )
    snap = build_snapshot(
        state, retry_queue=retry_queue, now=datetime.now(UTC)
    )
    assert snap.rate_limits == RateLimitSnapshot(
        limit=1000, remaining=250, reset_at="2026-04-28T13:00:00Z"
    )


async def test_rate_limits_none_when_unset(
    state: OrchestratorState, retry_queue: RetryQueue
) -> None:
    snap = build_snapshot(
        state, retry_queue=retry_queue, now=datetime.now(UTC)
    )
    assert snap.rate_limits is None


# ---------------------------------------------------------------------------
# Frozen dataclass — Snapshot/RunningRow/RetryRow immutable
# ---------------------------------------------------------------------------


def test_snapshot_is_frozen() -> None:
    snap = Snapshot(
        generated_at=datetime.now(UTC),
        counts={"running": 0, "retrying": 0, "completed": 0},
        running=[],
        retrying=[],
        codex_totals={
            "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            "seconds_running": 0.0,
        },
        rate_limits=None,
    )
    with pytest.raises(FrozenInstanceError):
        snap.generated_at = datetime.now(UTC)  # type: ignore[misc]


def test_running_row_is_frozen() -> None:
    now = datetime.now(UTC)
    row = RunningRow(
        issue_id="x", identifier="X-1", title="t", state="Todo",
        priority=None, session_id=None, started_at=now,
        last_codex_event=None, last_codex_timestamp=None,
        turn_count=0, last_error=None, restart_count=0,
        last_reported_input_tokens=0,
        last_reported_output_tokens=0,
        last_reported_total_tokens=0,
    )
    with pytest.raises(FrozenInstanceError):
        row.turn_count = 5  # type: ignore[misc]


def test_retry_row_is_frozen() -> None:
    row = RetryRow(
        issue_id="x", identifier=None, attempt=1, kind="failure",
        fire_at=datetime.now(UTC), last_error=None,
    )
    with pytest.raises(FrozenInstanceError):
        row.attempt = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Shape matches §13.7.2 example (subset of REQUIRED keys)
# ---------------------------------------------------------------------------


async def test_snapshot_shape_matches_spec_example(
    state: OrchestratorState, retry_queue: RetryQueue
) -> None:
    """The Snapshot fields cover every key shown in §13.7.2."""
    state.add_running(_running_entry())
    retry_queue.schedule(
        issue_id="rt", attempt=2, kind="failure",
        max_cap_ms=300_000, on_fire=_no_op, last_error="x",
    )
    state.codex_totals = TokenSnapshot(1, 2, 3)
    state.codex_rate_limits = None

    snap = build_snapshot(
        state, retry_queue=retry_queue, now=datetime.now(UTC)
    )

    # Top-level keys present per §13.7.2.
    assert hasattr(snap, "generated_at")
    assert hasattr(snap, "counts")
    assert hasattr(snap, "running")
    assert hasattr(snap, "retrying")
    assert hasattr(snap, "codex_totals")
    assert hasattr(snap, "rate_limits")

    # Running row fields per §13.7.2.
    row = snap.running[0]
    for field in (
        "issue_id", "identifier", "state", "session_id", "turn_count",
        "last_codex_event", "started_at", "last_codex_timestamp",
        "last_reported_input_tokens", "last_reported_output_tokens",
        "last_reported_total_tokens",
    ):
        assert hasattr(row, field), field

    # Retry row fields per §13.7.2.
    rrow = snap.retrying[0]
    for field in ("issue_id", "identifier", "attempt", "fire_at", "last_error"):
        assert hasattr(rrow, field), field

    # codex_totals required keys.
    for key in ("input_tokens", "output_tokens", "total_tokens", "seconds_running"):
        assert key in snap.codex_totals

    retry_queue.cancel("rt")
