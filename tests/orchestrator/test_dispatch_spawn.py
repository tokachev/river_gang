"""Tests for dispatch_issue in :mod:`river_gang.orchestrator.dispatch`
(SPED §16.4)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import cast

from river_gang.orchestrator import OrchestratorState, RetryQueue
from river_gang.orchestrator.dispatch import dispatch_issue
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


def _coro_factory(
    *, sleep_for: float = 0.0
) -> Callable[[Issue, int], Awaitable[None]]:
    """Returns a factory that yields a fresh coroutine on each call."""

    async def _worker(issue: Issue, attempt: int) -> None:
        await asyncio.sleep(sleep_for)

    def factory(issue: Issue, attempt: int) -> Awaitable[None]:
        return _worker(issue, attempt)

    return factory


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_happy_path_spawns_task_and_populates_state() -> None:
    loop = asyncio.get_running_loop()
    state = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=3)
    rq = RetryQueue(loop=loop)
    issue = _issue(id="iss-1")

    factory = _coro_factory()
    before = datetime.now(UTC)
    entry = dispatch_issue(
        state,
        issue=issue,
        attempt=2,
        worker_factory=factory,
        retry_queue=rq,
    )
    after = datetime.now(UTC)

    assert entry is not None
    assert entry.identifier == issue.identifier
    assert entry.issue is issue
    assert entry.session_id is None
    assert entry.last_reported_input_tokens == 0
    assert entry.last_reported_output_tokens == 0
    assert entry.last_reported_total_tokens == 0
    assert entry.last_codex_timestamp is None
    assert entry.last_codex_event is None
    assert entry.last_codex_message is None
    assert entry.last_error is None
    assert entry.restart_count == 0
    assert entry.retry_attempt == 2
    assert entry.monitor_handle is None
    assert entry.recent_events.maxlen == 50
    assert before <= entry.started_at <= after
    assert isinstance(entry.worker_handle, asyncio.Task)

    assert state.running == {"iss-1": entry}
    assert state.is_claimed("iss-1")
    assert len(rq) == 0

    # Drain the spawned task so pytest doesn't warn about unawaited coroutines.
    await entry.worker_handle


async def test_happy_path_factory_returning_task_directly() -> None:
    """Some callers may already wrap their work in a Task before passing it
    in (e.g. when they need a handle on the task themselves). The dispatch
    helper must accept either form.
    """
    loop = asyncio.get_running_loop()
    state = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=3)
    rq = RetryQueue(loop=loop)
    issue = _issue(id="iss-1")

    async def _worker(issue: Issue, attempt: int) -> None:
        await asyncio.sleep(0)

    def factory(issue: Issue, attempt: int) -> asyncio.Task[None]:
        return asyncio.create_task(_worker(issue, attempt))

    entry = dispatch_issue(
        state,
        issue=issue,
        attempt=1,
        worker_factory=factory,
        retry_queue=rq,
    )
    assert entry is not None
    assert isinstance(entry.worker_handle, asyncio.Task)
    await entry.worker_handle


# ---------------------------------------------------------------------------
# Retry-cancellation
# ---------------------------------------------------------------------------


async def test_dispatch_cancels_existing_retry_entry() -> None:
    loop = asyncio.get_running_loop()
    state = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=3)
    rq = RetryQueue(loop=loop)
    issue = _issue(id="iss-1")

    rq.schedule(
        issue_id="iss-1",
        attempt=1,
        kind="failure",
        max_cap_ms=300_000,
        on_fire=lambda _id: None,
    )
    assert "iss-1" in rq

    entry = dispatch_issue(
        state,
        issue=issue,
        attempt=2,
        worker_factory=_coro_factory(),
        retry_queue=rq,
    )
    assert entry is not None
    assert "iss-1" not in rq
    assert len(rq) == 0
    await entry.worker_handle


async def test_dispatch_does_not_touch_other_retry_entries() -> None:
    loop = asyncio.get_running_loop()
    state = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=3)
    rq = RetryQueue(loop=loop)

    rq.schedule(
        issue_id="other",
        attempt=1,
        kind="failure",
        max_cap_ms=300_000,
        on_fire=lambda _id: None,
    )
    issue = _issue(id="iss-1")
    entry = dispatch_issue(
        state,
        issue=issue,
        attempt=1,
        worker_factory=_coro_factory(),
        retry_queue=rq,
    )
    assert entry is not None
    assert "other" in rq
    assert "iss-1" not in rq
    rq.cancel("other")
    await entry.worker_handle


# ---------------------------------------------------------------------------
# Spawn failure path
# ---------------------------------------------------------------------------


async def test_spawn_failure_unclaims_and_schedules_retry() -> None:
    loop = asyncio.get_running_loop()
    state = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=3)
    rq = RetryQueue(loop=loop)
    issue = _issue(id="iss-1")

    def failing_factory(issue: Issue, attempt: int) -> Awaitable[None]:
        raise RuntimeError("could not spawn")

    out = dispatch_issue(
        state,
        issue=issue,
        attempt=2,
        worker_factory=failing_factory,
        retry_queue=rq,
    )
    assert out is None
    assert "iss-1" not in state.running
    assert not state.is_claimed("iss-1")

    # Retry scheduled at attempt+1 (failure kind).
    assert "iss-1" in rq
    retry_entry = rq.get("iss-1")
    assert retry_entry is not None
    assert retry_entry.attempt == 3  # 2 + 1 = next attempt
    assert retry_entry.kind == "failure"
    assert retry_entry.last_error is not None
    assert "could not spawn" in retry_entry.last_error
    rq.cancel("iss-1")


async def test_spawn_failure_with_existing_retry_replaces_it() -> None:
    """If we were retrying and dispatch fails again, the retry entry is
    rescheduled (cancel old, schedule new) rather than left dangling.
    """
    loop = asyncio.get_running_loop()
    state = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=3)
    rq = RetryQueue(loop=loop)
    issue = _issue(id="iss-1")

    rq.schedule(
        issue_id="iss-1",
        attempt=1,
        kind="failure",
        max_cap_ms=300_000,
        on_fire=lambda _id: None,
    )
    assert rq.get("iss-1").attempt == 1  # type: ignore[union-attr]

    def failing_factory(issue: Issue, attempt: int) -> Awaitable[None]:
        raise RuntimeError("nope")

    out = dispatch_issue(
        state,
        issue=issue,
        attempt=2,
        worker_factory=failing_factory,
        retry_queue=rq,
    )
    assert out is None
    # New retry recorded at attempt+1 (replacing the old).
    new = rq.get("iss-1")
    assert new is not None
    assert new.attempt == 3
    rq.cancel("iss-1")


# ---------------------------------------------------------------------------
# State invariants
# ---------------------------------------------------------------------------


async def test_running_keyed_by_issue_id() -> None:
    loop = asyncio.get_running_loop()
    state = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=3)
    rq = RetryQueue(loop=loop)

    a = _issue(id="a")
    b = _issue(id="b")
    e_a = dispatch_issue(
        state,
        issue=a,
        attempt=1,
        worker_factory=_coro_factory(),
        retry_queue=rq,
    )
    e_b = dispatch_issue(
        state,
        issue=b,
        attempt=1,
        worker_factory=_coro_factory(),
        retry_queue=rq,
    )
    assert e_a is not None and e_b is not None
    assert state.running["a"] is e_a
    assert state.running["b"] is e_b
    assert state.is_claimed("a") and state.is_claimed("b")
    await asyncio.gather(e_a.worker_handle, e_b.worker_handle)


async def test_dispatched_attempt_recorded_in_running_entry() -> None:
    loop = asyncio.get_running_loop()
    state = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=3)
    rq = RetryQueue(loop=loop)
    issue = _issue(id="iss-1")

    entry = dispatch_issue(
        state,
        issue=issue,
        attempt=5,
        worker_factory=_coro_factory(),
        retry_queue=rq,
    )
    assert entry is not None
    assert entry.retry_attempt == 5
    await cast(asyncio.Task[None], entry.worker_handle)
