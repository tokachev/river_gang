"""Retry queue + backoff (SPED §8.4).

Single-owner retry book-keeping for the orchestrator. The mailbox dispatcher
schedules retries via :meth:`RetryQueue.schedule`; the registered ``on_fire``
callback is invoked by the event loop and should send a
:class:`river_gang.orchestrator.RetryTimerFired` message to the mailbox so
state mutations stay on the single writer.

Backoff (§8.4):

- Continuation retries (clean exit) use a fixed 1000 ms delay.
- Failure-driven retries use ``min(10000 * 2^(attempt-1), max_cap_ms)``;
  ``attempt < 1`` is rejected so caller bugs surface immediately.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

RetryKind = Literal["continuation", "failure"]

# Continuation retries fire after a fixed 1s pause (§8.4).
CONTINUATION_DELAY_MS = 1000

# Base delay for failure-driven backoff (§8.4 formula).
_FAILURE_BASE_MS = 10_000


@dataclass(frozen=True)
class RetryEntry:
    """Scheduled retry record for one issue (§4.1.7)."""

    issue_id: str
    attempt: int
    kind: RetryKind
    scheduled_at: datetime
    fire_at: datetime
    timer_handle: asyncio.TimerHandle | None
    last_error: str | None = None


def compute_backoff_ms(attempt: int, *, max_cap_ms: int) -> int:
    """Failure-driven retry delay in milliseconds (§8.4).

    Raises:
        ValueError: ``attempt`` < 1.
    """
    if attempt < 1:
        raise ValueError(f"attempt must be >= 1, got {attempt}")
    raw: int = _FAILURE_BASE_MS * (2 ** (attempt - 1))
    return min(raw, max_cap_ms)


class RetryQueue:
    """Owns scheduled :class:`RetryEntry` instances keyed by issue id.

    The orchestrator owns exactly one :class:`RetryQueue`. Scheduling a new
    retry for an issue cancels any previously-scheduled timer for the same
    issue — re-scheduling supersedes (§8.4: "Cancel any existing retry timer
    for the same issue").
    """

    def __init__(self, *, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._entries: dict[str, RetryEntry] = {}

    def schedule(
        self,
        *,
        issue_id: str,
        attempt: int,
        kind: RetryKind,
        max_cap_ms: int,
        on_fire: Callable[[str], None],
        last_error: str | None = None,
    ) -> RetryEntry:
        delay_ms = (
            CONTINUATION_DELAY_MS
            if kind == "continuation"
            else compute_backoff_ms(attempt, max_cap_ms=max_cap_ms)
        )
        existing = self._entries.pop(issue_id, None)
        if existing is not None and existing.timer_handle is not None:
            existing.timer_handle.cancel()

        delay_s = delay_ms / 1000.0
        timer_handle = self._loop.call_later(delay_s, on_fire, issue_id)

        scheduled_at = datetime.now(UTC)
        fire_at = scheduled_at + timedelta(milliseconds=delay_ms)
        entry = RetryEntry(
            issue_id=issue_id,
            attempt=attempt,
            kind=kind,
            scheduled_at=scheduled_at,
            fire_at=fire_at,
            timer_handle=timer_handle,
            last_error=last_error,
        )
        self._entries[issue_id] = entry
        return entry

    def cancel(self, issue_id: str) -> RetryEntry | None:
        entry = self._entries.pop(issue_id, None)
        if entry is None:
            return None
        if entry.timer_handle is not None:
            entry.timer_handle.cancel()
        return entry

    def cancel_all(self) -> None:
        for entry in self._entries.values():
            if entry.timer_handle is not None:
                entry.timer_handle.cancel()
        self._entries.clear()

    def pop(self, issue_id: str) -> RetryEntry | None:
        """Remove the entry without canceling its timer.

        Use after :class:`~river_gang.orchestrator.RetryTimerFired` lands in
        the mailbox — the timer has already fired, so cancellation is moot.
        """
        return self._entries.pop(issue_id, None)

    def get(self, issue_id: str) -> RetryEntry | None:
        return self._entries.get(issue_id)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, issue_id: object) -> bool:
        return issue_id in self._entries


__all__ = [
    "CONTINUATION_DELAY_MS",
    "RetryEntry",
    "RetryKind",
    "RetryQueue",
    "compute_backoff_ms",
]
