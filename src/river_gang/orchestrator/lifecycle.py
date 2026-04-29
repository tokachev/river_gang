"""Worker-exit handler + retry-timer handler (SPED §16.6, §8.4).

The mailbox dispatcher calls :func:`on_worker_exit` whenever a
:class:`~river_gang.orchestrator.WorkerExit` message lands. This is the
single point where worker-completion side effects mutate
:class:`OrchestratorState`:

- pop the running entry,
- accumulate runtime,
- on success: mark completed and schedule a continuation retry,
- on failure: schedule a failure retry with exponential backoff,
- always: release the issue's claim so the next tick (or the retry
  timer firing) can re-evaluate dispatch eligibility.

:func:`on_retry_timer` handles ``RetryTimerFired`` messages (§16.6
"Retry handling"): pop the retry entry, refetch candidates, and either
dispatch (eligible + slots available), reschedule with the same attempt
(transient failure, e.g. tracker fetch error or no slots), or release
the claim (issue no longer eligible). Filtering is delegated to the
caller-supplied ``fetch_candidates_fn`` — by contract it returns a
post-filter list (see §8.2 + Task 27 ``filter_candidates``).

The retry timer's ``on_fire`` callback is supplied by the orchestrator —
typically a closure that posts :class:`RetryTimerFired` to the mailbox.
We accept it as a parameter so this module stays free of mailbox/loop
plumbing and is unit-testable in isolation.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable

from river_gang.codex import TokenSnapshot, extract_rate_limits
from river_gang.codex.protocol import compose_session_id
from river_gang.orchestrator.dispatch import available_slots_for_dispatch
from river_gang.orchestrator.mailbox import CodexUpdate, WorkerExit
from river_gang.orchestrator.retry import RetryQueue
from river_gang.orchestrator.state import OrchestratorState, RunningEntry
from river_gang.tracker.issue import Issue

logger = logging.getLogger(__name__)


def next_attempt_from(running_entry: RunningEntry) -> int:
    """Return the next attempt number for retry scheduling (§8.4)."""
    return running_entry.retry_attempt + 1


def add_runtime_seconds_to_totals(
    state: OrchestratorState, seconds: float
) -> None:
    """Accumulate ``seconds`` into ``state.runtime_seconds_total``."""
    state.add_runtime_seconds(seconds)


def on_worker_exit(
    state: OrchestratorState,
    *,
    message: WorkerExit,
    retry_queue: RetryQueue,
    max_retry_backoff_ms: int,
    on_retry_fire: Callable[[str], None],
) -> None:
    """Handle a :class:`WorkerExit` mailbox message.

    Missing running entries are tolerated (logged at WARNING) without
    further mutation — the orchestrator may have already removed the
    entry via reconcile or shutdown, so a re-entrant exit is a benign
    race rather than a hard error.
    """
    entry = state.remove_running(message.issue_id)
    if entry is None:
        logger.warning(
            "worker exit for %s but no running entry found — ignoring",
            message.issue_id,
        )
        return

    add_runtime_seconds_to_totals(state, message.runtime_seconds)

    if message.ok:
        state.record_completed(message.issue_id)
        retry_queue.schedule(
            issue_id=message.issue_id,
            attempt=next_attempt_from(entry),
            kind="continuation",
            max_cap_ms=max_retry_backoff_ms,
            on_fire=on_retry_fire,
            last_error=None,
        )
    else:
        retry_queue.schedule(
            issue_id=message.issue_id,
            attempt=next_attempt_from(entry),
            kind="failure",
            max_cap_ms=max_retry_backoff_ms,
            on_fire=on_retry_fire,
            last_error=message.last_error,
        )

    state.unclaim(message.issue_id)


# ---------------------------------------------------------------------------
# Retry-timer handler (§16.6 "Retry handling", §8.4)
# ---------------------------------------------------------------------------


FetchCandidatesFn = Callable[[], Awaitable[list[Issue]]]
DispatchFn = Callable[
    [Issue, int],
    "None | Awaitable[None]",
]


async def on_retry_timer(
    state: OrchestratorState,
    *,
    issue_id: str,
    retry_queue: RetryQueue,
    fetch_candidates_fn: FetchCandidatesFn,
    dispatch_fn: DispatchFn,
    per_state_map: dict[str, int],
    max_retry_backoff_ms: int,
    on_retry_fire: Callable[[str], None],
) -> None:
    """Handle a :class:`~river_gang.orchestrator.RetryTimerFired` event.

    ``fetch_candidates_fn`` is expected to return an already-filtered
    list of dispatch-eligible issues (see §8.2 / Task 27
    ``filter_candidates``). This handler does NOT re-run the eligibility
    filter — it only checks that the requested issue is in the list and
    that a concurrency slot is available for its current state.

    Outcomes:

    - retry entry missing       → no-op (debug log)
    - fetch raises              → reschedule same ``(attempt, kind)``,
                                   ``last_error="fetch failed: ..."``,
                                   claim retained
    - issue absent from refresh → release claim, no dispatch
    - no slots                  → reschedule same ``(attempt, kind)``,
                                   ``last_error="no available
                                   orchestrator slots"``, claim retained
    - eligible + slots          → call ``dispatch_fn(match, attempt)``;
                                   if it returns a coroutine, await it
    """
    entry = retry_queue.pop(issue_id)
    if entry is None:
        logger.debug(
            "retry timer fired for %s but no retry entry — ignoring",
            issue_id,
        )
        return

    # Skip dispatch if the issue already completed: a continuation retry
    # against a normally-exited issue produces a 1s redispatch loop until
    # the tracker propagates the terminal state, wasting concurrency
    # slots. The §8.4 retry exists specifically to RE-attempt; once the
    # run flagged ``ok`` we have no work to retry.
    if issue_id in state.completed:
        state.unclaim(issue_id)
        logger.debug(
            "retry timer fired for completed issue %s — releasing claim "
            "and skipping dispatch",
            issue_id,
        )
        return

    try:
        candidates = await fetch_candidates_fn()
    except Exception as exc:  # noqa: BLE001 -- transient: retry, do not crash dispatcher
        retry_queue.schedule(
            issue_id=issue_id,
            attempt=entry.attempt,
            kind=entry.kind,
            max_cap_ms=max_retry_backoff_ms,
            on_fire=on_retry_fire,
            last_error=f"fetch failed: {exc}",
        )
        return

    match = next((c for c in candidates if c.id == issue_id), None)
    if match is None:
        state.unclaim(issue_id)
        return

    slots = available_slots_for_dispatch(
        match.state,
        max_global=state.max_concurrent_agents,
        current_running_total=len(state.running),
        per_state_map=per_state_map,
        current_per_state=state.count_in_state(match.state),
    )
    if slots <= 0:
        retry_queue.schedule(
            issue_id=issue_id,
            attempt=entry.attempt,
            kind=entry.kind,
            max_cap_ms=max_retry_backoff_ms,
            on_fire=on_retry_fire,
            last_error="no available orchestrator slots",
        )
        return

    result = dispatch_fn(match, entry.attempt)
    if inspect.isawaitable(result):
        await result


# ---------------------------------------------------------------------------
# Codex update handler (§7.3 "Codex Update Event", §13.5)
# ---------------------------------------------------------------------------


def _extract_session_ids(payload: dict[str, object]) -> tuple[str, str] | None:
    """Return ``(thread_id, turn_id)`` from a payload, accepting snake_case or
    camelCase. ``None`` when either is missing or non-string.
    """
    thread = payload.get("thread_id")
    if not isinstance(thread, str) or thread == "":
        thread = payload.get("threadId")
    turn = payload.get("turn_id")
    if not isinstance(turn, str) or turn == "":
        turn = payload.get("turnId")
    if not isinstance(thread, str) or not isinstance(turn, str):
        return None
    if thread == "" or turn == "":
        return None
    return thread, turn


def _extract_message_text(payload: dict[str, object]) -> str | None:
    """Return the human-readable message string from a payload, or None."""
    for key in ("message", "text"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return None


def on_codex_update(
    state: OrchestratorState,
    *,
    message: CodexUpdate,
) -> None:
    """Handle a :class:`CodexUpdate` mailbox message (§7.3, §13.5).

    Updates per-issue live session fields, appends the event to the
    bounded ``recent_events`` deque, accumulates token deltas into
    ``state.codex_totals`` (clamping regressions to zero so a server
    reset can't drive aggregates backwards), and refreshes
    ``state.codex_rate_limits`` only when the payload carries one — we
    never null out a prior snapshot just because this event is silent
    on the topic.

    Per-turn ``session_id`` semantics: whenever the payload exposes both
    ``thread_id`` (or ``threadId``) and ``turn_id`` (``turnId``), the
    running entry's ``session_id`` is rewritten to ``"<thread>-<turn>"``.
    Updating on EVERY new turn (not just the first) is required by §10.2
    + the dashboard contract — otherwise the snapshot would show a
    stale id from turn 1 even after the worker advanced to turn 2+.

    Missing running entries are silently ignored (DEBUG log). Codex
    notifications can race past worker-exit / reconcile teardown, and a
    re-entrant update is benign rather than an error.
    """
    entry = state.running.get(message.issue_id)
    if entry is None:
        logger.debug(
            "codex update for %s but no running entry — dropping",
            message.issue_id,
        )
        return

    event = message.event
    payload = event.payload

    entry.last_codex_event = event.event
    entry.last_codex_timestamp = event.timestamp

    text = _extract_message_text(payload)
    if text is not None:
        entry.last_codex_message = text

    ids = _extract_session_ids(payload)
    if ids is not None:
        entry.session_id = compose_session_id(ids[0], ids[1])

    entry.recent_events.append(event)

    usage = event.usage
    if usage is not None:
        new_input = usage.get("input_tokens")
        new_output = usage.get("output_tokens")
        new_total = usage.get("total_tokens")
        if (
            isinstance(new_input, int)
            and isinstance(new_output, int)
            and isinstance(new_total, int)
        ):
            delta_in = max(0, new_input - entry.last_reported_input_tokens)
            delta_out = max(0, new_output - entry.last_reported_output_tokens)
            delta_total = max(0, new_total - entry.last_reported_total_tokens)
            state.codex_totals = TokenSnapshot(
                input_tokens=state.codex_totals.input_tokens + delta_in,
                output_tokens=state.codex_totals.output_tokens + delta_out,
                total_tokens=state.codex_totals.total_tokens + delta_total,
            )
            entry.last_reported_input_tokens = new_input
            entry.last_reported_output_tokens = new_output
            entry.last_reported_total_tokens = new_total

    rate_limits = extract_rate_limits(payload)
    if rate_limits is not None:
        state.codex_rate_limits = rate_limits


__all__ = [
    "DispatchFn",
    "FetchCandidatesFn",
    "add_runtime_seconds_to_totals",
    "next_attempt_from",
    "on_codex_update",
    "on_retry_timer",
    "on_worker_exit",
]
