"""Candidate filtering + dispatch ordering + concurrency slots + spawn
(SPED §8.2, §8.3, §16.4).

:func:`filter_candidates` enforces dispatch eligibility:

- state must be in ``active_states`` and not in ``terminal_states``
  (case-insensitive comparison per §4.2)
- not already in ``state.running``
- not already in ``state.claimed``
- blocker rule: any blocker whose state is NOT in ``terminal_states`` blocks
  dispatch. ``BlockerRef.state == None`` is treated conservatively as
  non-terminal (we'd rather wait than dispatch into an unresolved dep).

:func:`sort_for_dispatch` returns a new list ordered by:

1. ``priority`` ascending, with ``None`` sorting last
2. ``created_at`` oldest first (``None`` sorts after dated)
3. ``identifier`` lexicographic tie-break

:func:`available_slots_for_dispatch` and :func:`concurrency_check` enforce
§8.3: ``min(global_slots, per_state_slots)``. ``per_state_map`` is consulted
case-insensitively; states absent from the map fall back to the global cap
(so the map only restricts states explicitly listed).

:func:`dispatch_issue` is the §16.4 spawn helper: cancels any pending retry
for the issue, marks claimed, spawns the worker via the caller-supplied
factory, and inserts a fresh :class:`RunningEntry` into ``state.running``.
On spawn failure it unclaims and schedules a retry at ``attempt + 1``.
"""

from __future__ import annotations

import asyncio
import inspect
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime

from river_gang.orchestrator.retry import RetryQueue
from river_gang.orchestrator.state import OrchestratorState, RunningEntry
from river_gang.tracker.issue import Issue

# Sentinel timestamp pushing None-created issues to the end of their bucket.
# Tz-aware so it compares cleanly against parse_issue's UTC datetimes.
_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)


def filter_candidates(
    issues: list[Issue],
    state: OrchestratorState,
    *,
    active_states: Iterable[str],
    terminal_states: Iterable[str],
) -> list[Issue]:
    """Return issues that pass §8.2 eligibility (excluding slot checks)."""
    active_norm = {s.lower() for s in active_states}
    terminal_norm = {s.lower() for s in terminal_states}

    out: list[Issue] = []
    for issue in issues:
        state_norm = issue.state.lower()
        if state_norm in terminal_norm:
            continue
        if state_norm not in active_norm:
            continue
        if issue.id in state.running:
            continue
        if state.is_claimed(issue.id):
            continue
        if _has_non_terminal_blocker(issue, terminal_norm):
            continue
        out.append(issue)
    return out


def _has_non_terminal_blocker(issue: Issue, terminal_norm: set[str]) -> bool:
    for blocker in issue.blocked_by:
        if blocker.state is None:
            return True
        if blocker.state.lower() not in terminal_norm:
            return True
    return False


def sort_for_dispatch(issues: list[Issue]) -> list[Issue]:
    """Return a new list ordered per §8.2 (priority asc, created_at asc,
    identifier lex). Input list is not mutated.
    """
    return sorted(issues, key=_dispatch_sort_key)


def _dispatch_sort_key(issue: Issue) -> tuple[int, int, datetime, str]:
    # priority bucket: None → 1, dated → 0; numeric priority used as primary.
    priority_bucket = 0 if issue.priority is not None else 1
    priority_val = issue.priority if issue.priority is not None else 0
    created = issue.created_at if issue.created_at is not None else _FAR_FUTURE
    return (priority_bucket, priority_val, created, issue.identifier)


def available_slots_for_dispatch(
    state_name: str,
    *,
    max_global: int,
    current_running_total: int,
    per_state_map: dict[str, int],
    current_per_state: int,
) -> int:
    """Return min(global_slots, state_slots), each clamped to ≥ 0 (§8.3).

    States not in ``per_state_map`` fall back to ``max_global`` for the
    state-specific cap. Map lookup is case-insensitive — caller-supplied
    keys may carry any casing; we normalize both.
    """
    global_slots = max(max_global - current_running_total, 0)

    normalized_map = {k.lower(): v for k, v in per_state_map.items()}
    state_cap = normalized_map.get(state_name.lower(), max_global)
    state_slots = max(state_cap - current_per_state, 0)

    return min(global_slots, state_slots)


def concurrency_check(
    issue: Issue,
    state: OrchestratorState,
    *,
    per_state_map: dict[str, int],
) -> bool:
    """Return True iff the orchestrator has at least one slot for ``issue``."""
    return (
        available_slots_for_dispatch(
            issue.state,
            max_global=state.max_concurrent_agents,
            current_running_total=len(state.running),
            per_state_map=per_state_map,
            current_per_state=state.count_in_state(issue.state),
        )
        > 0
    )


# ---------------------------------------------------------------------------
# §16.4 dispatch_issue
# ---------------------------------------------------------------------------


WorkerFactory = Callable[[Issue, int], "Awaitable[None] | asyncio.Task[None]"]


def dispatch_issue(
    state: OrchestratorState,
    *,
    issue: Issue,
    attempt: int,
    worker_factory: WorkerFactory,
    retry_queue: RetryQueue,
    max_retry_backoff_ms: int = 300_000,
) -> RunningEntry | None:
    """Spawn a worker for ``issue`` and book-keep state (§16.4).

    Cancels any pre-existing retry for ``issue.id`` (re-dispatch supersedes
    a pending retry timer per §8.4). Marks ``issue.id`` claimed before the
    spawn so concurrent eligibility scans can't double-dispatch. On spawn
    failure, the claim is released and a fresh retry is scheduled at
    ``attempt + 1`` with kind ``"failure"``; the function returns ``None``
    so the caller can keep iterating other candidates without exiting the
    dispatch loop.
    """
    retry_queue.cancel(issue.id)
    state.mark_claimed(issue.id)

    try:
        spawned = worker_factory(issue, attempt)
        task = _coerce_task(spawned)
    except Exception as exc:  # noqa: BLE001 -- spawn failure must surface as retry
        state.unclaim(issue.id)
        retry_queue.schedule(
            issue_id=issue.id,
            attempt=attempt + 1,
            kind="failure",
            max_cap_ms=max_retry_backoff_ms,
            on_fire=lambda _id: None,
            last_error=str(exc),
        )
        return None

    entry = RunningEntry(
        worker_handle=task,
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
        retry_attempt=attempt,
    )
    state.add_running(entry)
    return entry


def _coerce_task(
    spawned: Awaitable[None] | asyncio.Task[None],
) -> asyncio.Task[None]:
    """Return a Task regardless of whether the factory yielded a coroutine
    or pre-wrapped Task.

    Generic awaitables that are neither Tasks nor coroutines are rejected
    here: ``state.RunningEntry.worker_handle`` is typed as ``Task[None]``
    and the orchestrator's cancel/await paths assume Task semantics, so
    accepting a bare Future would be a quiet contract break.
    """
    if isinstance(spawned, asyncio.Task):
        return spawned
    if inspect.iscoroutine(spawned):
        return asyncio.create_task(spawned)
    raise TypeError(
        f"worker_factory must return a coroutine or asyncio.Task, "
        f"got {type(spawned).__name__}"
    )


__all__ = [
    "WorkerFactory",
    "available_slots_for_dispatch",
    "concurrency_check",
    "dispatch_issue",
    "filter_candidates",
    "sort_for_dispatch",
]
