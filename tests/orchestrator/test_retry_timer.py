"""Tests for ``on_retry_timer`` in :mod:`river_gang.orchestrator.lifecycle`
(SPED §16.6, §8.4)."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import deque
from datetime import UTC, datetime

import pytest

from river_gang.orchestrator import OrchestratorState, RetryQueue, RunningEntry
from river_gang.orchestrator.lifecycle import on_retry_timer
from river_gang.tracker.errors import LinearError
from river_gang.tracker.issue import Issue


def _issue(*, id: str = "iss-1", state: str = "Todo") -> Issue:
    return Issue(
        id=id,
        identifier=f"MT-{id}",
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


def _running_entry(issue: Issue) -> RunningEntry:
    return RunningEntry(
        worker_handle=None,  # type: ignore[arg-type]
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
        retry_attempt=1,
    )


def _no_op(_id: str) -> None:
    return None


@pytest.fixture
def state() -> OrchestratorState:
    return OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=3)


@pytest.fixture
async def retry_queue() -> RetryQueue:
    return RetryQueue(loop=asyncio.get_running_loop())


def _candidates_factory(
    candidates: list[Issue] | None = None,
    *,
    raise_on_call: BaseException | None = None,
):
    """Build an async fetch_candidates_fn closure."""

    async def fetch() -> list[Issue]:
        if raise_on_call is not None:
            raise raise_on_call
        return list(candidates or [])

    return fetch


# ---------------------------------------------------------------------------
# Missing retry entry — no-op
# ---------------------------------------------------------------------------


async def test_missing_retry_entry_is_no_op(
    state: OrchestratorState,
    retry_queue: RetryQueue,
    caplog: pytest.LogCaptureFixture,
) -> None:
    dispatch_calls: list[tuple[Issue, int]] = []

    def dispatch_fn(issue: Issue, attempt: int) -> None:
        dispatch_calls.append((issue, attempt))

    pre_claimed = set(state.claimed)

    with caplog.at_level(logging.DEBUG, logger="river_gang.orchestrator.lifecycle"):
        await on_retry_timer(
            state,
            issue_id="ghost",
            retry_queue=retry_queue,
            fetch_candidates_fn=_candidates_factory([_issue(id="other")]),
            dispatch_fn=dispatch_fn,
            per_state_map={},
            max_retry_backoff_ms=300_000,
            on_retry_fire=_no_op,
        )

    assert dispatch_calls == []
    assert state.claimed == pre_claimed
    assert "ghost" not in retry_queue


# ---------------------------------------------------------------------------
# Completed issue retry skip — no dispatch loop after normal exit
# ---------------------------------------------------------------------------


async def test_completed_issue_retry_skips_dispatch(
    state: OrchestratorState, retry_queue: RetryQueue
) -> None:
    """Continuation retry on a normally-exited issue must NOT redispatch.

    Previously this produced a 1s-tick redispatch loop until the tracker
    propagated the terminal state. Now ``on_retry_timer`` short-circuits
    when ``issue_id`` is already in ``state.completed`` — claim is
    released, retry entry is consumed, no dispatch.
    """
    state.record_completed("iss-done")
    state.mark_claimed("iss-done")
    retry_queue.schedule(
        issue_id="iss-done",
        attempt=2,
        kind="continuation",
        max_cap_ms=300_000,
        on_fire=_no_op,
    )

    dispatch_calls: list[tuple[Issue, int]] = []

    def dispatch_fn(issue: Issue, attempt: int) -> None:
        dispatch_calls.append((issue, attempt))

    fetch_called = {"n": 0}

    async def fetch() -> list[Issue]:
        fetch_called["n"] += 1
        return [_issue(id="iss-done")]

    await on_retry_timer(
        state,
        issue_id="iss-done",
        retry_queue=retry_queue,
        fetch_candidates_fn=fetch,
        dispatch_fn=dispatch_fn,
        per_state_map={},
        max_retry_backoff_ms=300_000,
        on_retry_fire=_no_op,
    )

    assert dispatch_calls == []
    assert not state.is_claimed("iss-done")
    assert "iss-done" not in retry_queue
    # Short-circuit happens BEFORE fetch — fetch must not be called.
    assert fetch_called["n"] == 0


# ---------------------------------------------------------------------------
# Fetch failure → reschedule with same attempt
# ---------------------------------------------------------------------------


async def test_fetch_failure_reschedules_same_attempt(
    state: OrchestratorState, retry_queue: RetryQueue
) -> None:
    state.mark_claimed("iss-1")
    retry_queue.schedule(
        issue_id="iss-1",
        attempt=3,
        kind="failure",
        max_cap_ms=300_000,
        on_fire=_no_op,
    )

    dispatch_calls: list[tuple[Issue, int]] = []

    def dispatch_fn(issue: Issue, attempt: int) -> None:
        dispatch_calls.append((issue, attempt))

    fetch = _candidates_factory(raise_on_call=LinearError("network down"))

    await on_retry_timer(
        state,
        issue_id="iss-1",
        retry_queue=retry_queue,
        fetch_candidates_fn=fetch,
        dispatch_fn=dispatch_fn,
        per_state_map={},
        max_retry_backoff_ms=300_000,
        on_retry_fire=_no_op,
    )

    assert dispatch_calls == []
    new_entry = retry_queue.get("iss-1")
    assert new_entry is not None
    # Same attempt + same kind, with last_error populated.
    assert new_entry.attempt == 3
    assert new_entry.kind == "failure"
    assert new_entry.last_error is not None
    assert "network down" in new_entry.last_error
    assert "fetch" in new_entry.last_error.lower()
    # Claim must remain — fetch failure is transient; we'll retry shortly.
    assert state.is_claimed("iss-1")
    retry_queue.cancel("iss-1")


# ---------------------------------------------------------------------------
# Issue absent from candidates → release claim, no dispatch
# ---------------------------------------------------------------------------


async def test_issue_not_in_candidates_releases_claim(
    state: OrchestratorState, retry_queue: RetryQueue
) -> None:
    state.mark_claimed("iss-1")
    retry_queue.schedule(
        issue_id="iss-1",
        attempt=2,
        kind="failure",
        max_cap_ms=300_000,
        on_fire=_no_op,
    )

    dispatch_calls: list[tuple[Issue, int]] = []

    def dispatch_fn(issue: Issue, attempt: int) -> None:
        dispatch_calls.append((issue, attempt))

    fetch = _candidates_factory([_issue(id="other")])

    await on_retry_timer(
        state,
        issue_id="iss-1",
        retry_queue=retry_queue,
        fetch_candidates_fn=fetch,
        dispatch_fn=dispatch_fn,
        per_state_map={},
        max_retry_backoff_ms=300_000,
        on_retry_fire=_no_op,
    )

    assert dispatch_calls == []
    assert not state.is_claimed("iss-1")
    # Retry entry was popped (timer fired).
    assert "iss-1" not in retry_queue


# ---------------------------------------------------------------------------
# No slots → reschedule with same attempt + 'no available orchestrator slots'
# ---------------------------------------------------------------------------


async def test_no_slots_reschedules_with_no_slots_message(
    state: OrchestratorState, retry_queue: RetryQueue
) -> None:
    # Saturate global capacity.
    state.add_running(_running_entry(_issue(id="r1", state="Todo")))
    state.add_running(_running_entry(_issue(id="r2", state="Todo")))
    state.add_running(_running_entry(_issue(id="r3", state="Todo")))
    state.mark_claimed("iss-1")
    retry_queue.schedule(
        issue_id="iss-1",
        attempt=4,
        kind="failure",
        max_cap_ms=300_000,
        on_fire=_no_op,
    )

    dispatch_calls: list[tuple[Issue, int]] = []

    def dispatch_fn(issue: Issue, attempt: int) -> None:
        dispatch_calls.append((issue, attempt))

    fetch = _candidates_factory([_issue(id="iss-1", state="Todo")])

    await on_retry_timer(
        state,
        issue_id="iss-1",
        retry_queue=retry_queue,
        fetch_candidates_fn=fetch,
        dispatch_fn=dispatch_fn,
        per_state_map={},
        max_retry_backoff_ms=300_000,
        on_retry_fire=_no_op,
    )

    assert dispatch_calls == []
    new_entry = retry_queue.get("iss-1")
    assert new_entry is not None
    assert new_entry.attempt == 4  # same attempt
    assert new_entry.kind == "failure"  # same kind
    assert new_entry.last_error == "no available orchestrator slots"
    # Claim preserved — we still intend to dispatch this issue.
    assert state.is_claimed("iss-1")
    retry_queue.cancel("iss-1")


async def test_per_state_limit_blocks_dispatch_with_global_room(
    state: OrchestratorState, retry_queue: RetryQueue
) -> None:
    # max=3, only 1 In Progress running, but per-state cap In Progress = 1.
    state.add_running(
        _running_entry(_issue(id="r1", state="In Progress"))
    )
    state.mark_claimed("iss-1")
    retry_queue.schedule(
        issue_id="iss-1",
        attempt=1,
        kind="failure",
        max_cap_ms=300_000,
        on_fire=_no_op,
    )
    dispatch_calls: list[tuple[Issue, int]] = []

    def dispatch_fn(issue: Issue, attempt: int) -> None:
        dispatch_calls.append((issue, attempt))

    fetch = _candidates_factory([_issue(id="iss-1", state="In Progress")])

    await on_retry_timer(
        state,
        issue_id="iss-1",
        retry_queue=retry_queue,
        fetch_candidates_fn=fetch,
        dispatch_fn=dispatch_fn,
        per_state_map={"in progress": 1},
        max_retry_backoff_ms=300_000,
        on_retry_fire=_no_op,
    )

    assert dispatch_calls == []
    new_entry = retry_queue.get("iss-1")
    assert new_entry is not None
    assert new_entry.last_error == "no available orchestrator slots"
    retry_queue.cancel("iss-1")


# ---------------------------------------------------------------------------
# Happy path: eligible + slots → dispatch
# ---------------------------------------------------------------------------


async def test_eligible_dispatches_with_attempt_from_entry(
    state: OrchestratorState, retry_queue: RetryQueue
) -> None:
    state.mark_claimed("iss-1")
    retry_queue.schedule(
        issue_id="iss-1",
        attempt=3,
        kind="continuation",
        max_cap_ms=300_000,
        on_fire=_no_op,
    )
    dispatch_calls: list[tuple[Issue, int]] = []

    def dispatch_fn(issue: Issue, attempt: int) -> None:
        dispatch_calls.append((issue, attempt))

    refreshed = _issue(id="iss-1", state="Todo")
    fetch = _candidates_factory([refreshed])

    await on_retry_timer(
        state,
        issue_id="iss-1",
        retry_queue=retry_queue,
        fetch_candidates_fn=fetch,
        dispatch_fn=dispatch_fn,
        per_state_map={},
        max_retry_backoff_ms=300_000,
        on_retry_fire=_no_op,
    )

    assert len(dispatch_calls) == 1
    issue_arg, attempt_arg = dispatch_calls[0]
    assert issue_arg is refreshed
    assert attempt_arg == 3
    # Retry entry was popped (timer fired).
    assert "iss-1" not in retry_queue


async def test_dispatch_fn_async_variant_is_awaited(
    state: OrchestratorState, retry_queue: RetryQueue
) -> None:
    """If the orchestrator passes an async dispatch_fn, we must await it."""
    state.mark_claimed("iss-1")
    retry_queue.schedule(
        issue_id="iss-1",
        attempt=2,
        kind="failure",
        max_cap_ms=300_000,
        on_fire=_no_op,
    )
    dispatched: list[tuple[Issue, int]] = []

    async def dispatch_fn(issue: Issue, attempt: int) -> None:
        await asyncio.sleep(0)
        dispatched.append((issue, attempt))

    refreshed = _issue(id="iss-1", state="Todo")
    fetch = _candidates_factory([refreshed])

    await on_retry_timer(
        state,
        issue_id="iss-1",
        retry_queue=retry_queue,
        fetch_candidates_fn=fetch,
        dispatch_fn=dispatch_fn,
        per_state_map={},
        max_retry_backoff_ms=300_000,
        on_retry_fire=_no_op,
    )
    assert dispatched == [(refreshed, 2)]
    # Sanity: dispatch_fn was actually a coroutine function.
    assert inspect.iscoroutinefunction(dispatch_fn)
