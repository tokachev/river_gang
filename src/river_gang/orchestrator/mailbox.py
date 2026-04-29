"""Single-writer mailbox (SPED §7).

The orchestrator owns a single :class:`Mailbox` consumed by exactly one
dispatcher task. Workers, timers, the polling loop, and config reloads are
*producers* — they only ever ``send``. The dispatcher is the *only*
mutator of :class:`OrchestratorState`, so we don't need locks: serialization
falls out of the mailbox's FIFO contract.

Messages are a discriminated union of frozen dataclasses; ``match``/``case``
gives mypy proper exhaustiveness checks at the dispatch site.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from river_gang.codex import RuntimeEvent
from river_gang.config import EffectiveConfig


@dataclass(frozen=True)
class PollTick:
    """Polling timer fired — orchestrator should fetch and dispatch."""


@dataclass(frozen=True, kw_only=True)
class WorkerExit:
    """Worker task exited.

    ``reason`` is freeform (e.g. ``"normal"``, ``"stall_detected"``,
    ``"workspace_failed"``). ``ok`` is True iff the run completed
    successfully; failure paths set it False and populate ``last_error``.
    """

    issue_id: str
    reason: str
    ok: bool
    runtime_seconds: float
    last_error: str | None = None


@dataclass(frozen=True, kw_only=True)
class CodexUpdate:
    """A coding-agent notification observed by a worker, forwarded to the
    orchestrator so token totals, recent_events, and last_codex_* can be
    updated by the single writer.
    """

    issue_id: str
    event: RuntimeEvent


@dataclass(frozen=True, kw_only=True)
class RetryTimerFired:
    """Scheduled retry due — orchestrator should re-evaluate the issue."""

    issue_id: str


@dataclass(frozen=True, kw_only=True)
class ConfigReloaded:
    """Workflow front-matter changed; orchestrator must adopt new effective
    config (poll interval, concurrency caps, etc.).
    """

    config: EffectiveConfig


@dataclass(frozen=True)
class Shutdown:
    """Shutdown requested — drain workers and exit the dispatcher loop."""


OrchestratorMessage = (
    PollTick | WorkerExit | CodexUpdate | RetryTimerFired | ConfigReloaded | Shutdown
)


class Mailbox:
    """Thin wrapper around :class:`asyncio.Queue` for typed orchestrator IPC.

    ``send`` uses ``put_nowait`` — the queue is unbounded, so we don't apply
    backpressure on producers. Worst-case the dispatcher falls behind and
    memory grows; the operator-visible response is the orchestrator's
    runtime metrics, not blocked workers.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[OrchestratorMessage] = asyncio.Queue()

    async def send(self, msg: OrchestratorMessage) -> None:
        self._queue.put_nowait(msg)

    def send_nowait(self, msg: OrchestratorMessage) -> None:
        """Synchronous enqueue for sync producer paths (timer callbacks,
        signal handlers, watcher reload callback). Identical semantics to
        :meth:`send` — queue is unbounded so ``put_nowait`` cannot fail.
        """
        self._queue.put_nowait(msg)

    async def recv(self) -> OrchestratorMessage:
        return await self._queue.get()

    def qsize(self) -> int:
        return self._queue.qsize()


__all__ = [
    "CodexUpdate",
    "ConfigReloaded",
    "Mailbox",
    "OrchestratorMessage",
    "PollTick",
    "RetryTimerFired",
    "Shutdown",
    "WorkerExit",
]
