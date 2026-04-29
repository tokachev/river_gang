"""Stall detection helper (SPED §16.5, §10.6 ``stall_timeout_ms``).

The orchestrator's reconciliation loop calls this predicate every tick to
decide whether an active worker should be terminated for inactivity. The
function is deliberately pure (no side effects, no IO) so it can be unit
tested in isolation and dropped into either the orchestrator or a snapshot
view without coupling.

Inputs:
    now:              current wall-clock instant.
    last_event_at:    timestamp of the latest agent event for this run, or
                      ``None`` when no events have arrived yet.
    started_at:       worker session start instant.
    stall_timeout_ms: threshold; ``<= 0`` disables stall detection.

All datetimes MUST be timezone-aware. Mixing naive and aware values is
rejected with :class:`TypeError` rather than silently producing the wrong
answer (Python's own subtraction would raise the same error one frame
deeper, but we surface it here with a clearer message).
"""

from __future__ import annotations

from datetime import datetime


def should_terminate_for_stall(
    *,
    now: datetime,
    last_event_at: datetime | None,
    started_at: datetime,
    stall_timeout_ms: int,
) -> bool:
    """Return ``True`` when the worker has been inactive for too long.

    Reference instant is ``last_event_at`` if set, otherwise ``started_at``
    (a freshly-started session that hasn't yet emitted its first event).
    Negative or zero ``stall_timeout_ms`` disables the check entirely.
    Clock skew that places the reference instant slightly in the future
    clamps to zero elapsed — never spuriously terminate on a bad clock.
    """
    if stall_timeout_ms <= 0:
        return False

    reference = last_event_at if last_event_at is not None else started_at

    _require_aware(now, "now")
    _require_aware(started_at, "started_at")
    if last_event_at is not None:
        _require_aware(last_event_at, "last_event_at")

    elapsed_ms = (now - reference).total_seconds() * 1000.0
    if elapsed_ms < 0:
        elapsed_ms = 0.0

    return elapsed_ms >= stall_timeout_ms


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise TypeError(
            f"{name} must be timezone-aware; got naive datetime {value!r}"
        )


__all__ = ["should_terminate_for_stall"]
