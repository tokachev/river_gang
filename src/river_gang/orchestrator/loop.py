"""Poll-and-dispatch tick loop + main orchestrator class (SPED §16.2).

:class:`Orchestrator` is the integration class that owns the mailbox,
runs the single-writer dispatcher task, and schedules the periodic
:class:`PollTick` via ``loop.call_later``.

Tick algorithm (§8.1):

1. **Reconcile first** (§8.5):
   - Stall detection — synthesise :class:`WorkerExit` for any running
     entry whose last codex event is past ``codex.stall_timeout_ms``.
   - Tracker-state refresh — if the refresh fetch raises, log WARNING
     and skip the refresh-reconcile (workers keep running). Otherwise
     classify each running entry into terminate-with-cleanup,
     terminate-without-cleanup, or update_snapshot. Cleanup is
     fire-and-forget so the dispatcher loop stays snappy.
2. **Defensive reload** (§6.2): re-read the workflow config holder; on
   exception or ``None`` (validation failing) → log error, skip dispatch
   this tick, reschedule, return.
3. **Fetch candidates**; on tracker exception → log WARNING, skip
   dispatch, reschedule, return.
4. **Filter + sort** (§8.2).
5. **Dispatch** until ``concurrency_check`` says no slots remain (sorted
   so we can break early).
6. **Reschedule next tick** via ``loop.call_later`` and remember the
   handle so shutdown can cancel.

The dispatcher's :meth:`run` loop drains the mailbox and dispatches each
:class:`OrchestratorMessage` to the appropriate handler. State mutations
happen ONLY here — workers never touch :class:`OrchestratorState`
directly (single-writer pattern, §7).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from river_gang.codex.client import RuntimeEvent, Session
from river_gang.config import EffectiveConfig
from river_gang.observability.logging import set_log_context
from river_gang.orchestrator.dispatch import (
    concurrency_check,
    dispatch_issue,
    filter_candidates,
    sort_for_dispatch,
)
from river_gang.orchestrator.lifecycle import (
    on_codex_update,
    on_retry_timer,
    on_worker_exit,
)
from river_gang.orchestrator.mailbox import (
    CodexUpdate,
    ConfigReloaded,
    Mailbox,
    PollTick,
    RetryTimerFired,
    Shutdown,
    WorkerExit,
)
from river_gang.orchestrator.reconcile import (
    detect_stalls,
    reconcile_running_with_tracker,
)
from river_gang.orchestrator.retry import RetryQueue
from river_gang.orchestrator.state import OrchestratorState
from river_gang.orchestrator.worker import run_agent_attempt
from river_gang.tracker.errors import LinearError
from river_gang.tracker.issue import Issue

logger = logging.getLogger(__name__)


WorkflowLoader = Callable[[], "EffectiveConfig | None"]
CodexClientPerWorkerFactory = Callable[
    [Path, "EffectiveConfig"], "Awaitable[Any]"
]
"""Factory that builds a fresh :class:`CodexClient` per worker attempt.

Production wiring (see :mod:`river_gang.orchestrator.startup`) uses this to
spawn a new ``codex app-server`` subprocess per issue so subprocess
lifecycle is bound to a single Codex session, per the §10.1 contract.
``Orchestrator`` calls the factory inside :meth:`_make_worker_factory`
right before invoking :func:`run_agent_attempt`; the factory receives
the resolved per-issue workspace path so it can wire ``cwd`` correctly.
"""


class _CodexClientLike(Protocol):
    """Minimal :class:`CodexClient` surface needed by the worker."""

    async def start_session(
        self,
        *,
        workspace: Any,
        prompt: str,
        issue: Issue,
        approval_policy: str,
        sandbox_policy: str,
        read_timeout_ms: int,
        tracker_kind: str | None = ...,
    ) -> Session: ...

    async def stream_turn(
        self,
        *,
        session: Session,
        prompt: str,
        on_event: Callable[[RuntimeEvent], None],
        turn_timeout_ms: int,
        is_first_turn: bool = ...,
    ) -> Any: ...

    async def stop_session(
        self, session: Session, *, graceful_timeout_s: float = ...
    ) -> None: ...


class _WorkspaceManagerLike(Protocol):
    async def ensure_for_issue(self, identifier: str) -> Any: ...

    async def cleanup_for_issue(self, identifier: str) -> None: ...


class _TrackerLike(Protocol):
    async def fetch_candidate_issues(
        self, active_states: list[str]
    ) -> list[Issue]: ...

    async def fetch_issue_states_by_ids(
        self, issue_ids: list[str]
    ) -> list[Issue]: ...


class Orchestrator:
    """Main orchestrator class — owns mailbox, runs dispatcher, schedules ticks."""

    def __init__(
        self,
        *,
        state: OrchestratorState,
        mailbox: Mailbox,
        retry_queue: RetryQueue,
        tracker: _TrackerLike,
        codex_client: _CodexClientLike | None,
        workspace_manager: _WorkspaceManagerLike,
        prompt_template: str,
        config: EffectiveConfig,
        workflow_loader: WorkflowLoader,
        codex_client_factory: CodexClientPerWorkerFactory | None = None,
    ) -> None:
        self.state = state
        self.mailbox = mailbox
        self.retry_queue = retry_queue
        self.tracker = tracker
        self.codex_client = codex_client
        self.workspace_manager = workspace_manager
        self.prompt_template = prompt_template
        self.config = config
        self.workflow_loader = workflow_loader
        self._codex_client_factory = codex_client_factory

        self._next_tick_handle: asyncio.TimerHandle | None = None
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        # Cancelled worker tasks awaiting their final settle. Held so the
        # task object can't be garbage-collected mid-cancel and so shutdown
        # can drain them on demand.
        self._cancelled_workers: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # Tick handler (§8.1, §16.2)
    # ------------------------------------------------------------------

    async def on_tick(self) -> None:
        """Reconcile → defensive reload → fetch → dispatch → reschedule."""
        # 1. Reconcile.
        await self._reconcile()

        # 2. Defensive reload.
        try:
            new_config = self.workflow_loader()
        except Exception:  # noqa: BLE001 -- loader bug must not crash dispatcher
            logger.error(
                "workflow_loader raised — skipping dispatch for this tick",
                exc_info=True,
            )
            self._schedule_next_tick()
            return
        if new_config is None:
            logger.error(
                "workflow_loader returned None (validation failing) — "
                "skipping dispatch for this tick"
            )
            self._schedule_next_tick()
            return
        if new_config is not self.config:
            self.config = new_config

        # 3. Fetch candidates.
        try:
            candidates = await self.tracker.fetch_candidate_issues(
                list(self.config.tracker.active_states)
            )
        except LinearError as exc:
            logger.warning(
                "tracker fetch_candidate_issues failed — skipping dispatch: %s",
                exc,
            )
            self._schedule_next_tick()
            return
        except Exception:  # noqa: BLE001 -- non-LinearError = bug, escalate to ERROR
            logger.error(
                "tracker fetch_candidate_issues raised non-LinearError "
                "(programming bug or contract violation) — skipping dispatch",
                exc_info=True,
            )
            self._schedule_next_tick()
            return

        # 4. Filter + sort.
        eligible = filter_candidates(
            candidates,
            self.state,
            active_states=self.config.tracker.active_states,
            terminal_states=self.config.tracker.terminal_states,
        )
        eligible = sort_for_dispatch(eligible)

        # 5. Dispatch until slots exhausted.
        per_state_map = self.config.agent.max_concurrent_agents_by_state
        worker_factory = self._make_worker_factory()
        for issue in eligible:
            if not concurrency_check(
                issue, self.state, per_state_map=per_state_map
            ):
                # Per-state caps mean state X being full doesn't preclude
                # state Y having room — keep scanning. The global cap
                # check inside ``concurrency_check`` will short-circuit
                # the rest naturally once total slots are exhausted.
                continue
            dispatch_issue(
                self.state,
                issue=issue,
                attempt=1,
                worker_factory=worker_factory,
                retry_queue=self.retry_queue,
                max_retry_backoff_ms=self.config.agent.max_retry_backoff_ms,
            )

        # 6. Reschedule next tick.
        self._schedule_next_tick()

    # ------------------------------------------------------------------
    # Reconcile (§8.5)
    # ------------------------------------------------------------------

    async def _reconcile(self) -> None:
        """Stall detection + tracker-state refresh."""
        # Stall part — synthesise WorkerExit per stalled entry.
        stalled_ids = detect_stalls(
            self.state,
            now=datetime.now(UTC),
            stall_timeout_ms=self.config.codex.stall_timeout_ms,
        )
        for issue_id in stalled_ids:
            entry = self.state.running.get(issue_id)
            if entry is None:
                continue
            self._terminate_worker_handle(entry.worker_handle)
            # Track the cancelled task so it cannot keep mutating the world
            # via outstanding awaits before we drop our last reference. The
            # cleanup-tasks set already discards completed tasks via the
            # done-callback below.
            self._track_cancelled_worker(entry.worker_handle)
            runtime_seconds = (
                datetime.now(UTC) - entry.started_at
            ).total_seconds()
            await self.mailbox.send(
                WorkerExit(
                    issue_id=issue_id,
                    reason="stall_terminated",
                    ok=False,
                    runtime_seconds=runtime_seconds,
                    last_error=(
                        f"stall timeout {self.config.codex.stall_timeout_ms}ms "
                        f"exceeded"
                    ),
                )
            )

        # Tracker-state refresh part.
        if not self.state.running:
            return

        running_ids = [entry.issue.id for entry in self.state.running.values()]
        try:
            refreshed = await self.tracker.fetch_issue_states_by_ids(running_ids)
        except LinearError as exc:
            logger.warning(
                "tracker fetch_issue_states_by_ids failed during reconcile "
                "— keeping workers running: %s",
                exc,
            )
            return
        except Exception as exc:  # noqa: BLE001 -- defensive
            logger.warning(
                "tracker fetch_issue_states_by_ids raised during reconcile "
                "— keeping workers running: %s",
                exc,
                exc_info=True,
            )
            return

        actions = reconcile_running_with_tracker(
            self.state,
            refreshed_issues=refreshed,
            terminal_states=self.config.tracker.terminal_states,
            active_states=self.config.tracker.active_states,
        )

        for issue_id in actions.terminate_with_cleanup:
            entry = self.state.running.get(issue_id)
            if entry is None:
                continue
            self._terminate_worker_handle(entry.worker_handle)
            self._track_cancelled_worker(entry.worker_handle)
            self._spawn_cleanup_task(entry.identifier)

        for issue_id in actions.terminate_without_cleanup:
            entry = self.state.running.get(issue_id)
            if entry is None:
                continue
            self._terminate_worker_handle(entry.worker_handle)
            self._track_cancelled_worker(entry.worker_handle)

        for issue_id, refreshed_issue in actions.update_snapshot.items():
            entry = self.state.running.get(issue_id)
            if entry is None:
                continue
            entry.issue = refreshed_issue

    def _terminate_worker_handle(self, task: asyncio.Task[None]) -> None:
        if task.done():
            return
        task.cancel()

    def _track_cancelled_worker(self, task: asyncio.Task[None]) -> None:
        """Hold a reference to a cancelled worker task until it settles.

        Without this, the orchestrator drops its only reference to the
        task immediately after ``cancel()``; if the task hasn't observed
        the cancellation yet it can keep running and mutating world
        state (mailbox, tracker, etc.) until the next event-loop tick.
        We also schedule a background drain so no warnings fire at
        interpreter shutdown for "Task was destroyed but it is pending".
        """
        if task.done():
            return
        self._cancelled_workers.add(task)
        task.add_done_callback(self._cancelled_workers.discard)

    def _spawn_cleanup_task(self, identifier: str) -> None:
        """Schedule ``cleanup_for_issue`` as fire-and-forget background task."""

        async def _cleanup() -> None:
            try:
                await self.workspace_manager.cleanup_for_issue(identifier)
            except Exception:  # noqa: BLE001 -- cleanup must not crash dispatcher
                logger.warning(
                    "workspace cleanup for %s raised — suppressing",
                    identifier,
                    exc_info=True,
                )

        task = asyncio.create_task(_cleanup())
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    # ------------------------------------------------------------------
    # Worker factory (§16.4)
    # ------------------------------------------------------------------

    def _make_worker_factory(
        self,
    ) -> Callable[[Issue, int], asyncio.Task[None]]:
        def _factory(issue: Issue, attempt: int) -> asyncio.Task[None]:
            return asyncio.create_task(
                run_agent_attempt(
                    issue=issue,
                    attempt=attempt,
                    mailbox=self.mailbox,
                    codex_client=self.codex_client,
                    workspace_manager=self.workspace_manager,
                    tracker=self.tracker,
                    prompt_template=self.prompt_template,
                    config=self.config,
                    codex_client_factory=self._codex_client_factory,
                )
            )

        return _factory

    # ------------------------------------------------------------------
    # Tick scheduling
    # ------------------------------------------------------------------

    def _schedule_next_tick(self) -> None:
        """Schedule the next :class:`PollTick` via ``loop.call_later``."""
        loop = asyncio.get_running_loop()
        delay_s = self.config.polling.interval_ms / 1000.0
        # Cancel any prior handle so we don't leak overlapping timers.
        if self._next_tick_handle is not None:
            self._next_tick_handle.cancel()
        self._next_tick_handle = loop.call_later(
            delay_s, self._post_poll_tick
        )

    def _post_poll_tick(self) -> None:
        """Timer callback — synchronously enqueue a :class:`PollTick`.

        ``mailbox.send`` is async only because the public surface is
        symmetric; the unbounded queue lets us use the public sync
        ``send_nowait`` without losing the message.
        """
        self.mailbox.send_nowait(PollTick())

    def schedule_initial_tick(self) -> None:
        """Post the very first :class:`PollTick` to kick off the loop."""
        self.mailbox.send_nowait(PollTick())

    def cancel_next_tick(self) -> None:
        """Cancel the pending tick (shutdown)."""
        if self._next_tick_handle is not None:
            self._next_tick_handle.cancel()
            self._next_tick_handle = None

    # ------------------------------------------------------------------
    # Main loop (§7, §16.2)
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Drain the mailbox until :class:`Shutdown`, dispatching messages.

        Per-issue handlers are wrapped in :func:`set_log_context` so any
        logs they emit (including the missing-entry warnings in
        :mod:`lifecycle`) carry the §13.1 ``issue_id`` /
        ``issue_identifier`` / ``session_id`` fields.
        """
        while True:
            msg = await self.mailbox.recv()
            if isinstance(msg, PollTick):
                await self.on_tick()
            elif isinstance(msg, WorkerExit):
                with self._issue_log_context(msg.issue_id):
                    on_worker_exit(
                        self.state,
                        message=msg,
                        retry_queue=self.retry_queue,
                        max_retry_backoff_ms=self.config.agent.max_retry_backoff_ms,
                        on_retry_fire=self._post_retry_timer,
                    )
            elif isinstance(msg, CodexUpdate):
                with self._issue_log_context(msg.issue_id):
                    on_codex_update(self.state, message=msg)
            elif isinstance(msg, RetryTimerFired):
                with self._issue_log_context(msg.issue_id):
                    await on_retry_timer(
                        self.state,
                        issue_id=msg.issue_id,
                        retry_queue=self.retry_queue,
                        fetch_candidates_fn=self._fetch_filtered_candidates,
                        dispatch_fn=self._retry_dispatch_fn,
                        per_state_map=self.config.agent.max_concurrent_agents_by_state,
                        max_retry_backoff_ms=self.config.agent.max_retry_backoff_ms,
                        on_retry_fire=self._post_retry_timer,
                    )
            elif isinstance(msg, ConfigReloaded):
                self.config = msg.config
                # Mirror the live caps onto OrchestratorState so the
                # next concurrency check + tick scheduling pick them up
                # (the state copies were seeded at startup).
                self.state.poll_interval_ms = msg.config.polling.interval_ms
                self.state.max_concurrent_agents = (
                    msg.config.agent.max_concurrent_agents
                )
            elif isinstance(msg, Shutdown):
                self.cancel_next_tick()
                return

    def _issue_log_context(self, issue_id: str) -> Any:
        """Return a :func:`set_log_context` matching the running entry, if any.

        Falls back to ``issue_id`` alone when the entry is absent (e.g.
        racing :class:`WorkerExit` after reconcile-driven removal) so the
        handler's own missing-entry log still carries the id.
        """
        entry = self.state.running.get(issue_id)
        if entry is None:
            return set_log_context(issue_id=issue_id)
        return set_log_context(
            issue_id=issue_id,
            issue_identifier=entry.identifier,
            session_id=entry.session_id,
        )

    def _post_retry_timer(self, issue_id: str) -> None:
        """Retry timer fired — enqueue :class:`RetryTimerFired`."""
        self.mailbox.send_nowait(RetryTimerFired(issue_id=issue_id))

    async def _fetch_filtered_candidates(self) -> list[Issue]:
        """Refetch candidates for the retry-timer handler.

        Returns raw (unfiltered) candidates: ``on_retry_timer`` only needs
        to find the target issue by id and check slot availability —
        applying ``filter_candidates`` here would drop the very issue we
        want to redispatch (it's still in ``state.claimed`` between the
        worker exit and the timer firing per §8.4).
        """
        return await self.tracker.fetch_candidate_issues(
            list(self.config.tracker.active_states)
        )

    def _retry_dispatch_fn(
        self, issue: Issue, attempt: int
    ) -> None:
        dispatch_issue(
            self.state,
            issue=issue,
            attempt=attempt,
            worker_factory=self._make_worker_factory(),
            retry_queue=self.retry_queue,
            max_retry_backoff_ms=self.config.agent.max_retry_backoff_ms,
        )

    # ------------------------------------------------------------------
    # Test-aware shutdown helper
    # ------------------------------------------------------------------

    async def shutdown_workers(self) -> None:
        """Cancel all running worker tasks and await them.

        Used by tests; production shutdown lives in Task 38's startup
        orchestration. Best-effort — exceptions during cancellation
        propagate as :class:`asyncio.CancelledError` and are swallowed.
        """
        for entry in list(self.state.running.values()):
            if not entry.worker_handle.done():
                entry.worker_handle.cancel()
        for entry in list(self.state.running.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await entry.worker_handle


__all__ = [
    "Orchestrator",
    "WorkflowLoader",
]
