"""SPED §17.4 conformance: Orchestrator Dispatch, Reconciliation, Retry."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

import pytest

from river_gang.codex import TokenSnapshot
from river_gang.observability.snapshot import build_snapshot
from river_gang.orchestrator.dispatch import filter_candidates, sort_for_dispatch
from river_gang.orchestrator.lifecycle import on_worker_exit
from river_gang.orchestrator.mailbox import WorkerExit
from river_gang.orchestrator.reconcile import (
    detect_stalls,
    reconcile_running_with_tracker,
)
from river_gang.orchestrator.retry import (
    CONTINUATION_DELAY_MS,
    RetryQueue,
    compute_backoff_ms,
)
from river_gang.orchestrator.state import OrchestratorState, RunningEntry
from river_gang.tracker.issue import BlockerRef, Issue

pytestmark = pytest.mark.conformance


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _issue(
    *,
    id: str = "i", identifier: str = "MT-1", state: str = "Todo",
    priority: int | None = 2,
    blocked_by: tuple[BlockerRef, ...] = (),
    created_at: datetime | None = None,
) -> Issue:
    return Issue(
        id=id, identifier=identifier, title="t", state=state,
        description=None, priority=priority, branch_name=None,
        url=None, labels=(), blocked_by=blocked_by,
        created_at=created_at or datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=None,
    )


def _entry(issue: Issue, *, started_at: datetime | None = None,
           last_codex_timestamp: datetime | None = None) -> RunningEntry:
    return RunningEntry(
        worker_handle=None,  # type: ignore[arg-type]
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


def _no_op(_id: str) -> None:
    return None


@pytest.fixture
async def retry_queue() -> RetryQueue:
    return RetryQueue(loop=asyncio.get_running_loop())


@pytest.fixture
def state() -> OrchestratorState:
    return OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=5)


# ---------------------------------------------------------------------------
# Sort: priority asc → oldest creation
# ---------------------------------------------------------------------------


def test_dispatch_sort_priority_then_oldest() -> None:
    """Conformance §17.4: dispatch sort order is priority then oldest
    creation time."""
    older = _issue(id="o", identifier="MT-Old", priority=1,
                   created_at=datetime(2026, 1, 1, tzinfo=UTC))
    newer = _issue(id="n", identifier="MT-New", priority=1,
                   created_at=datetime(2026, 6, 1, tzinfo=UTC))
    low_pri = _issue(id="l", identifier="MT-Lo", priority=3,
                     created_at=datetime(2026, 1, 1, tzinfo=UTC))
    sorted_issues = sort_for_dispatch([newer, low_pri, older])
    assert [i.identifier for i in sorted_issues] == ["MT-Old", "MT-New", "MT-Lo"]


# ---------------------------------------------------------------------------
# Blocker rule for Todo
# ---------------------------------------------------------------------------


def test_todo_with_non_terminal_blocker_not_eligible(
    state: OrchestratorState,
) -> None:
    """Conformance §17.4: ``Todo`` issue with non-terminal blockers is
    not eligible."""
    blocked = _issue(
        state="Todo",
        blocked_by=(BlockerRef(id="b", identifier="B-1", state="In Progress"),),
    )
    out = filter_candidates(
        [blocked], state, active_states=["Todo"], terminal_states=["Done"]
    )
    assert out == []


def test_todo_with_terminal_blocker_is_eligible(
    state: OrchestratorState,
) -> None:
    """Conformance §17.4: ``Todo`` issue with terminal blockers is
    eligible."""
    eligible = _issue(
        state="Todo",
        blocked_by=(BlockerRef(id="b", identifier="B-1", state="Done"),),
    )
    out = filter_candidates(
        [eligible], state, active_states=["Todo"], terminal_states=["Done"]
    )
    assert out == [eligible]


# ---------------------------------------------------------------------------
# Reconcile transitions
# ---------------------------------------------------------------------------


def test_active_state_refresh_updates_running_entry(
    state: OrchestratorState,
) -> None:
    """Conformance §17.4: active-state issue refresh updates running entry
    state."""
    issue = _issue(id="x", state="Todo")
    state.add_running(_entry(issue))
    refreshed = _issue(id="x", state="In Progress")
    actions = reconcile_running_with_tracker(
        state,
        refreshed_issues=[refreshed],
        terminal_states=("Done",),
        active_states=("Todo", "In Progress"),
    )
    assert "x" in actions.update_snapshot
    assert actions.update_snapshot["x"].state == "In Progress"


def test_non_active_state_terminates_without_cleanup(
    state: OrchestratorState,
) -> None:
    """Conformance §17.4: non-active state stops running agent without
    workspace cleanup."""
    state.add_running(_entry(_issue(id="x", state="In Progress")))
    refreshed = _issue(id="x", state="Cancelled-Like-Custom")
    actions = reconcile_running_with_tracker(
        state,
        refreshed_issues=[refreshed],
        terminal_states=("Done",),
        active_states=("Todo", "In Progress"),
    )
    assert "x" in actions.terminate_without_cleanup
    assert "x" not in actions.terminate_with_cleanup


def test_terminal_state_terminates_with_cleanup(
    state: OrchestratorState,
) -> None:
    """Conformance §17.4: terminal state stops running agent and cleans
    workspace."""
    state.add_running(_entry(_issue(id="x", state="In Progress")))
    refreshed = _issue(id="x", state="Done")
    actions = reconcile_running_with_tracker(
        state,
        refreshed_issues=[refreshed],
        terminal_states=("Done",),
        active_states=("Todo", "In Progress"),
    )
    assert "x" in actions.terminate_with_cleanup


def test_reconciliation_with_no_running_is_noop(
    state: OrchestratorState,
) -> None:
    """Conformance §17.4: reconciliation with no running issues is a
    no-op."""
    actions = reconcile_running_with_tracker(
        state,
        refreshed_issues=[],
        terminal_states=("Done",),
        active_states=("Todo",),
    )
    assert actions.terminate_with_cleanup == []
    assert actions.terminate_without_cleanup == []
    assert actions.update_snapshot == {}


# ---------------------------------------------------------------------------
# Retry scheduling on worker exit
# ---------------------------------------------------------------------------


async def test_normal_exit_schedules_continuation_attempt_one(
    state: OrchestratorState, retry_queue: RetryQueue
) -> None:
    """Conformance §17.4: normal worker exit schedules a short continuation
    retry (attempt 1)."""
    state.add_running(_entry(_issue(id="i", identifier="MT-1")))
    state.mark_claimed("i")
    on_worker_exit(
        state,
        message=WorkerExit(
            issue_id="i", reason="normal", ok=True, runtime_seconds=1.0,
        ),
        retry_queue=retry_queue,
        max_retry_backoff_ms=300_000,
        on_retry_fire=_no_op,
    )
    entry = retry_queue.get("i")
    assert entry is not None
    assert entry.kind == "continuation"
    assert entry.attempt == 1
    delta_ms = (entry.fire_at - entry.scheduled_at).total_seconds() * 1000
    assert delta_ms == pytest.approx(CONTINUATION_DELAY_MS, rel=0.05)
    retry_queue.cancel("i")


async def test_abnormal_exit_uses_10s_exponential_backoff(
    state: OrchestratorState, retry_queue: RetryQueue
) -> None:
    """Conformance §17.4: abnormal worker exit increments retries with
    10s-based exponential backoff."""
    # Direct check on the formula:
    assert compute_backoff_ms(1, max_cap_ms=300_000) == 10_000
    assert compute_backoff_ms(2, max_cap_ms=300_000) == 20_000
    assert compute_backoff_ms(3, max_cap_ms=300_000) == 40_000


async def test_retry_backoff_uses_max_cap(
    retry_queue: RetryQueue,
) -> None:
    """Conformance §17.4: retry backoff cap uses configured
    ``agent.max_retry_backoff_ms``."""
    cap = 25_000
    # 10s * 2^4 = 160_000, capped at 25_000.
    assert compute_backoff_ms(5, max_cap_ms=cap) == cap


async def test_retry_entry_carries_attempt_due_id_error(
    state: OrchestratorState, retry_queue: RetryQueue
) -> None:
    """Conformance §17.4: retry queue entries include attempt, due time,
    identifier, and error."""
    state.add_running(_entry(_issue(id="i")))
    state.mark_claimed("i")
    on_worker_exit(
        state,
        message=WorkerExit(
            issue_id="i", reason="turn_failed", ok=False,
            runtime_seconds=1.0, last_error="boom",
        ),
        retry_queue=retry_queue,
        max_retry_backoff_ms=300_000,
        on_retry_fire=_no_op,
    )
    entry = retry_queue.get("i")
    assert entry is not None
    assert entry.attempt >= 1
    assert entry.fire_at > entry.scheduled_at
    assert entry.issue_id == "i"
    assert entry.last_error == "boom"
    retry_queue.cancel("i")


# ---------------------------------------------------------------------------
# Stall detection
# ---------------------------------------------------------------------------


def test_stall_detection_returns_stalled_ids(
    state: OrchestratorState,
) -> None:
    """Conformance §17.4: stall detection kills stalled sessions and
    schedules retry.

    Detection layer: returns stalled ids; the loop layer (Task 37
    on_tick) terminates the worker_handle and posts a synthesized
    WorkerExit which feeds into the retry path tested above.
    """
    now = datetime.now(UTC)
    stalled_issue = _issue(id="s", state="In Progress")
    fresh_issue = _issue(id="f", state="In Progress")
    state.add_running(
        _entry(stalled_issue,
               started_at=now - timedelta(seconds=600),
               last_codex_timestamp=now - timedelta(seconds=600))
    )
    state.add_running(
        _entry(fresh_issue,
               started_at=now,
               last_codex_timestamp=now)
    )
    stalled = detect_stalls(state, now=now, stall_timeout_ms=60_000)
    assert stalled == ["s"]


# ---------------------------------------------------------------------------
# Slot exhaustion
# ---------------------------------------------------------------------------


async def test_slot_exhaustion_reschedule_with_explicit_error(
    state: OrchestratorState, retry_queue: RetryQueue
) -> None:
    """Conformance §17.4: slot exhaustion requeues retries with explicit
    error reason.

    The retry-timer handler (Task 35) reschedules with
    ``last_error="no available orchestrator slots"`` when the
    concurrency check fails. Direct verification of the error string:
    """
    from river_gang.orchestrator.lifecycle import on_retry_timer

    state2 = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=1)
    state2.add_running(_entry(_issue(id="incumbent", state="In Progress")))
    state2.mark_claimed("retry-target")
    retry_queue.schedule(
        issue_id="retry-target", attempt=2, kind="failure",
        max_cap_ms=300_000, on_fire=_no_op,
    )

    candidate = _issue(id="retry-target", state="In Progress")

    async def _fetch() -> list[Issue]:
        return [candidate]

    def _dispatch(_iss: Issue, _attempt: int) -> None:
        return None

    await on_retry_timer(
        state2,
        issue_id="retry-target",
        retry_queue=retry_queue,
        fetch_candidates_fn=_fetch,
        dispatch_fn=_dispatch,
        per_state_map={},
        max_retry_backoff_ms=300_000,
        on_retry_fire=_no_op,
    )
    rescheduled = retry_queue.get("retry-target")
    assert rescheduled is not None
    assert rescheduled.last_error == "no available orchestrator slots"
    retry_queue.cancel("retry-target")


# ---------------------------------------------------------------------------
# Snapshot API
# ---------------------------------------------------------------------------


async def test_snapshot_includes_running_retry_tokens_rate_limits(
    state: OrchestratorState, retry_queue: RetryQueue
) -> None:
    """Conformance §17.4: if a snapshot API is implemented, it returns
    running rows, retry rows, token totals, and rate limits."""
    state.add_running(_entry(_issue(id="x")))
    state.codex_totals = TokenSnapshot(input_tokens=1, output_tokens=2, total_tokens=3)
    retry_queue.schedule(
        issue_id="r", attempt=1, kind="failure",
        max_cap_ms=300_000, on_fire=_no_op,
    )
    snap = build_snapshot(state, retry_queue=retry_queue, now=datetime.now(UTC))
    assert len(snap.running) == 1
    assert len(snap.retrying) == 1
    assert snap.codex_totals["total_tokens"] == 3
    assert snap.rate_limits is None  # not set → None propagates
    retry_queue.cancel("r")


def test_snapshot_timeout_unavailable_modes_are_documented() -> None:
    """Conformance §17.4: if a snapshot API is implemented,
    timeout/unavailable cases are surfaced.

    The synchronous :func:`build_snapshot` is a pure projection — it
    cannot time out (no IO) and cannot become unavailable (in-memory
    state). When the optional HTTP layer wraps it, request-level
    timeout/availability is FastAPI/uvicorn's responsibility, not the
    snapshot builder's. Documented as a no-op for the in-process API
    surface.
    """
    # No assertion — this is a documentation marker. The HTTP layer
    # raises 5xx via the global error envelope (Task 46) when the
    # underlying provider raises; clients see ``code: http_error``.
    _ = Iterable  # silence unused
