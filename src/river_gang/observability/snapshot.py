"""Runtime snapshot builder (SPED §13.3, §13.7.2).

Pure projection of :class:`OrchestratorState` + :class:`RetryQueue` into
immutable dataclasses suitable for serialisation by an HTTP/dashboard
extension. Side-effect free — the orchestrator's mailbox dispatcher can
call :func:`build_snapshot` at any tick without taking locks.

The shapes here cover every REQUIRED key in §13.3 ("running",
"retrying", "codex_totals", "rate_limits") and match the §13.7.2
``GET /api/v1/state`` example response. Renderers (M9) translate
between these dataclasses and JSON; the snapshot itself stays Pythonic.

Implementation choices:

- ``turn_count`` — counts events in ``recent_events`` whose ``event``
  name signals a new turn (``"turn_started"`` or ``"turn.start"``).
  ``recent_events`` is the bounded deque maintained by
  :func:`river_gang.orchestrator.lifecycle.on_codex_update`.
- ``codex_totals.seconds_running`` — live aggregate per §13.5: stored
  cumulative for ended sessions PLUS active-session ``now -
  started_at`` for every entry in ``state.running``. Negative deltas
  (clock skew / fakes) clamp to zero so the displayed runtime cannot
  shrink as a side effect of a non-monotonic ``now``.
- ``RetryRow.identifier`` — :class:`RetryEntry` only carries
  ``issue_id``, so we look up ``state.running`` for a matching entry to
  fill ``identifier``. Unknown → ``None`` (the field is recovered as
  soon as the issue is re-dispatched and a fresh
  :class:`RunningEntry` lands).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from river_gang.codex import RateLimitSnapshot
from river_gang.orchestrator.retry import RetryQueue
from river_gang.orchestrator.state import OrchestratorState, RunningEntry

# Event names that signal a new turn began. Matches the runtime-event
# vocabulary used by the codex client + tests. Both snake_case and
# dotted spelling appear in the wild — count both.
_TURN_START_EVENTS: frozenset[str] = frozenset(
    {"turn_started", "turn.start"}
)


@dataclass(frozen=True)
class RunningRow:
    """One running session row (§13.7.2 ``running[]`` entry)."""

    issue_id: str
    identifier: str
    title: str
    state: str
    priority: int | None
    session_id: str | None
    started_at: datetime
    last_codex_event: str | None
    last_codex_timestamp: datetime | None
    turn_count: int
    last_error: str | None
    restart_count: int
    last_reported_input_tokens: int
    last_reported_output_tokens: int
    last_reported_total_tokens: int


@dataclass(frozen=True)
class RetryRow:
    """One scheduled retry row (§13.7.2 ``retrying[]`` entry).

    ``identifier`` is ``None`` when the orchestrator no longer holds a
    :class:`RunningEntry` for ``issue_id`` — the next dispatch will
    surface the identifier on the running side once a fresh entry is
    installed.
    """

    issue_id: str
    identifier: str | None
    attempt: int
    kind: str
    fire_at: datetime
    last_error: str | None


@dataclass(frozen=True)
class Snapshot:
    """Top-level snapshot payload (§13.7.2 ``GET /api/v1/state``)."""

    generated_at: datetime
    counts: dict[str, int]
    running: list[RunningRow]
    retrying: list[RetryRow]
    codex_totals: dict[str, Any]
    rate_limits: RateLimitSnapshot | None


def build_snapshot(
    state: OrchestratorState,
    *,
    retry_queue: RetryQueue,
    now: datetime,
) -> Snapshot:
    """Project ``state`` + ``retry_queue`` into an immutable :class:`Snapshot`.

    Pure: no mutation of either argument. ``now`` is injected (rather
    than read from ``datetime.now()`` here) so callers can stamp the
    snapshot deterministically and so tests can drive clock-dependent
    cases.
    """
    running_rows: list[RunningRow] = [
        _build_running_row(entry) for entry in state.running.values()
    ]
    retry_rows: list[RetryRow] = _build_retry_rows(state, retry_queue)

    seconds_running = _compute_seconds_running(state, now=now)

    codex_totals: dict[str, Any] = {
        "input_tokens": state.codex_totals.input_tokens,
        "output_tokens": state.codex_totals.output_tokens,
        "total_tokens": state.codex_totals.total_tokens,
        "seconds_running": seconds_running,
    }

    counts = {
        "running": len(running_rows),
        "retrying": len(retry_rows),
        "completed": len(state.completed),
    }

    return Snapshot(
        generated_at=now,
        counts=counts,
        running=running_rows,
        retrying=retry_rows,
        codex_totals=codex_totals,
        rate_limits=state.codex_rate_limits,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _build_running_row(entry: RunningEntry) -> RunningRow:
    return RunningRow(
        issue_id=entry.issue.id,
        identifier=entry.identifier,
        title=entry.issue.title,
        state=entry.issue.state,
        priority=entry.issue.priority,
        session_id=entry.session_id,
        started_at=entry.started_at,
        last_codex_event=entry.last_codex_event,
        last_codex_timestamp=entry.last_codex_timestamp,
        turn_count=_count_turn_starts(entry),
        last_error=entry.last_error,
        restart_count=entry.restart_count,
        last_reported_input_tokens=entry.last_reported_input_tokens,
        last_reported_output_tokens=entry.last_reported_output_tokens,
        last_reported_total_tokens=entry.last_reported_total_tokens,
    )


def _count_turn_starts(entry: RunningEntry) -> int:
    return sum(1 for evt in entry.recent_events if evt.event in _TURN_START_EVENTS)


def _build_retry_rows(
    state: OrchestratorState, retry_queue: RetryQueue
) -> list[RetryRow]:
    rows: list[RetryRow] = []
    # ``RetryQueue`` doesn't expose a public iterator yet — direct access to
    # ``_entries`` is the single-owner pattern documented at the class level.
    for retry_entry in retry_queue._entries.values():  # noqa: SLF001
        # ``state.running`` is empty for any retry-scheduled issue (the
        # retry path always runs after ``remove_running``); the original
        # identifier survives in ``identifier_index``.
        identifier = state.identifier_index.get(retry_entry.issue_id)
        rows.append(
            RetryRow(
                issue_id=retry_entry.issue_id,
                identifier=identifier,
                attempt=retry_entry.attempt,
                kind=retry_entry.kind,
                fire_at=retry_entry.fire_at,
                last_error=retry_entry.last_error,
            )
        )
    return rows


def _compute_seconds_running(
    state: OrchestratorState, *, now: datetime
) -> float:
    """Cumulative ended-session runtime + active elapsed (§13.5).

    Active elapsed clamped to ``>= 0`` to absorb clock skew or
    test-injected ``now`` values that pre-date ``started_at``.
    """
    active = 0.0
    for entry in state.running.values():
        delta = (now - entry.started_at).total_seconds()
        if delta > 0:
            active += delta
    return state.runtime_seconds_total + active


__all__ = [
    "RetryRow",
    "RunningRow",
    "Snapshot",
    "build_snapshot",
]
