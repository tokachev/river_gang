"""Tests for stall detection in :mod:`river_gang.orchestrator.reconcile`
(SPED §8.5 Part A)."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta

from river_gang.orchestrator import OrchestratorState, RunningEntry, detect_stalls
from river_gang.tracker.issue import Issue


def _issue(*, id: str, identifier: str = "MT-1") -> Issue:
    return Issue(
        id=id,
        identifier=identifier,
        title="t",
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


def _entry(
    *,
    id: str,
    started_at: datetime,
    last_codex_timestamp: datetime | None = None,
) -> RunningEntry:
    issue = _issue(id=id, identifier=f"MT-{id}")
    return RunningEntry(
        worker_handle=None,  # type: ignore[arg-type]
        monitor_handle=None,
        identifier=issue.identifier,
        issue=issue,
        session_id=None,
        last_reported_input_tokens=0,
        last_reported_output_tokens=0,
        last_reported_total_tokens=0,
        started_at=started_at,
        last_codex_timestamp=last_codex_timestamp,
        last_codex_event=None,
        last_codex_message=None,
        recent_events=deque(maxlen=50),
        last_error=None,
        restart_count=0,
        retry_attempt=0,
    )


def _state_with(*entries: RunningEntry) -> OrchestratorState:
    s = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=10)
    for e in entries:
        s.add_running(e)
    return s


# ---------------------------------------------------------------------------
# Disabled / empty
# ---------------------------------------------------------------------------


def test_empty_running_returns_empty_list() -> None:
    state = _state_with()
    now = datetime.now(UTC)
    assert detect_stalls(state, now=now, stall_timeout_ms=60_000) == []


def test_stall_timeout_zero_returns_empty_list() -> None:
    now = datetime.now(UTC)
    state = _state_with(
        _entry(
            id="a",
            started_at=now - timedelta(minutes=10),
            last_codex_timestamp=None,
        )
    )
    assert detect_stalls(state, now=now, stall_timeout_ms=0) == []


def test_stall_timeout_negative_returns_empty_list() -> None:
    now = datetime.now(UTC)
    state = _state_with(
        _entry(
            id="a",
            started_at=now - timedelta(minutes=10),
            last_codex_timestamp=None,
        )
    )
    assert detect_stalls(state, now=now, stall_timeout_ms=-1) == []


# ---------------------------------------------------------------------------
# Single-entry happy paths
# ---------------------------------------------------------------------------


def test_within_timeout_using_last_codex_timestamp_not_stalled() -> None:
    now = datetime.now(UTC)
    state = _state_with(
        _entry(
            id="a",
            started_at=now - timedelta(minutes=20),
            last_codex_timestamp=now - timedelta(seconds=5),
        )
    )
    assert detect_stalls(state, now=now, stall_timeout_ms=60_000) == []


def test_past_timeout_using_last_codex_timestamp_stalled() -> None:
    now = datetime.now(UTC)
    state = _state_with(
        _entry(
            id="a",
            started_at=now - timedelta(minutes=20),
            last_codex_timestamp=now - timedelta(seconds=120),
        )
    )
    assert detect_stalls(state, now=now, stall_timeout_ms=60_000) == ["a"]


def test_no_codex_events_uses_started_at_within_timeout() -> None:
    now = datetime.now(UTC)
    state = _state_with(
        _entry(
            id="a",
            started_at=now - timedelta(seconds=10),
            last_codex_timestamp=None,
        )
    )
    assert detect_stalls(state, now=now, stall_timeout_ms=60_000) == []


def test_no_codex_events_uses_started_at_past_timeout() -> None:
    now = datetime.now(UTC)
    state = _state_with(
        _entry(
            id="a",
            started_at=now - timedelta(seconds=120),
            last_codex_timestamp=None,
        )
    )
    assert detect_stalls(state, now=now, stall_timeout_ms=60_000) == ["a"]


# ---------------------------------------------------------------------------
# Boundary
# ---------------------------------------------------------------------------


def test_exact_boundary_is_stalled() -> None:
    """`should_terminate_for_stall` uses ``elapsed >= timeout``; exact match
    counts as stalled.
    """
    now = datetime.now(UTC)
    state = _state_with(
        _entry(
            id="a",
            started_at=now - timedelta(seconds=60),
            last_codex_timestamp=None,
        )
    )
    assert detect_stalls(state, now=now, stall_timeout_ms=60_000) == ["a"]


# ---------------------------------------------------------------------------
# Multi-entry — only stalled returned, insertion order preserved
# ---------------------------------------------------------------------------


def test_multiple_entries_only_stalled_returned_in_insertion_order() -> None:
    now = datetime.now(UTC)
    fresh = _entry(
        id="fresh",
        started_at=now - timedelta(seconds=10),
        last_codex_timestamp=now - timedelta(seconds=2),
    )
    stalled_a = _entry(
        id="stalled_a",
        started_at=now - timedelta(minutes=10),
        last_codex_timestamp=now - timedelta(seconds=120),
    )
    fresh_b = _entry(
        id="fresh_b",
        started_at=now - timedelta(seconds=30),
        last_codex_timestamp=None,
    )
    stalled_b = _entry(
        id="stalled_b",
        started_at=now - timedelta(minutes=5),
        last_codex_timestamp=None,
    )
    state = _state_with(fresh, stalled_a, fresh_b, stalled_b)
    out = detect_stalls(state, now=now, stall_timeout_ms=60_000)
    assert out == ["stalled_a", "stalled_b"]


def test_clock_skew_future_reference_clamps_to_zero_elapsed() -> None:
    """Mirrors `should_terminate_for_stall` clamp behavior — bad clock must
    not spuriously terminate a fresh worker.
    """
    now = datetime.now(UTC)
    state = _state_with(
        _entry(
            id="a",
            started_at=now + timedelta(seconds=30),  # in the "future"
            last_codex_timestamp=None,
        )
    )
    assert detect_stalls(state, now=now, stall_timeout_ms=60_000) == []
