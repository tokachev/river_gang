"""Per-issue worker attempt (SPED §16.5, §7.1, §9.4, §10).

A *worker attempt* is one end-to-end pass through the §16.5 flow for a
single issue: render prompt → ensure workspace → ``before_run`` → start
Codex session → stream turns until exit condition → cleanup. The result
is reported to the orchestrator as a single :class:`WorkerExit` mailbox
message; the orchestrator's single-writer dispatcher decides what to do
with it (schedule retry, mark complete, etc.).

The worker is a free async function rather than a class — there's no
state worth carrying across calls, and tests want a flat callable to
drive with the M6.5 fakes.

Exit reasons (worker-internal vocabulary; freeform per :class:`WorkerExit`):

- ``"normal"``               — turn completed and tracker confirms the
                               issue is no longer active. ``ok=True``.
- ``"max_turns"``             — agent.max_turns reached without resolution.
- ``"workspace_failed"``      — ``ensure_for_issue`` raised.
- ``"before_run_failed"``     — ``before_run`` hook returned non-ok.
- ``"prompt_failed"``         — Liquid render failed.
- ``"session_failed"``        — Codex ``start_session`` raised.
- ``"turn_failed"``           — turn ended with ``turn_failed`` event.
- ``"turn_cancelled"``        — turn ended with ``turn_cancelled`` event.
- ``"turn_input_required"``   — turn paused for operator input.
- ``"turn_timeout"``          — turn exceeded ``codex.turn_timeout_ms``.
- ``"port_exit"``             — Codex subprocess exited mid-stream.
- ``"tracker_refresh_failed"``— per-turn state refresh raised.
- ``"unexpected"``            — defensive catch-all: any exception not
                               classified by the typed exits above
                               (e.g. transport closed mid-tool-call,
                               approval-handler bug). Prevents the worker
                               task from settling in exception state and
                               leaving the issue stuck in ``running`` until
                               the stall reaper kicks in.

§9.4 boundary: the worker NEVER calls ``before_remove`` /
``workspace_manager.cleanup_for_issue``. Workspace cleanup belongs to
reconcile's terminal-cleanup path; the worker only runs the agent.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from river_gang.codex.client import RuntimeEvent, Session
from river_gang.codex.errors import (
    CodexError,
    PortExit,
    TurnCancelled,
    TurnFailed,
    TurnInputRequired,
    TurnTimeout,
)
from river_gang.config import EffectiveConfig
from river_gang.observability.logging import session_id_var, set_log_context
from river_gang.orchestrator.mailbox import CodexUpdate, Mailbox, WorkerExit
from river_gang.prompt import render_prompt
from river_gang.prompt.errors import PromptError
from river_gang.tracker.errors import LinearError
from river_gang.tracker.issue import Issue
from river_gang.workspace.hooks import HookResult, run_hook
from river_gang.workspace.manager import WorkspaceManagerError

logger = logging.getLogger(__name__)

# Continuation prompt sent on turn ≥ 2 (§7.1: "guidance only" payload).
CONTINUATION_GUIDANCE = (
    "Continue working on the issue. "
    "Use the linear_graphql tool to refresh state and check progress."
)


# ---------------------------------------------------------------------------
# Structural protocols — duck-typed so tests can drop in fakes
# ---------------------------------------------------------------------------


class _CodexClientLike(Protocol):
    async def start_session(
        self,
        *,
        workspace: Path,
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


class _TrackerLike(Protocol):
    async def fetch_issue_states_by_ids(
        self, issue_ids: list[str]
    ) -> list[Issue]: ...


HookRunner = Callable[..., Awaitable[HookResult]]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


CodexClientPerWorkerFactory = Callable[
    [Path, EffectiveConfig], Awaitable[_CodexClientLike]
]
"""Factory that produces a fresh :class:`CodexClient` per attempt.

The factory receives the resolved per-issue workspace path (``cwd``)
and the live :class:`EffectiveConfig`. Production wiring builds a real
``codex app-server`` subprocess + :class:`CodexClient` here; tests
typically inject a fake that returns a pre-configured client.
"""


async def run_agent_attempt(
    *,
    issue: Issue,
    attempt: int,
    mailbox: Mailbox,
    codex_client: _CodexClientLike | None = None,
    workspace_manager: _WorkspaceManagerLike,
    tracker: _TrackerLike,
    prompt_template: str,
    config: EffectiveConfig,
    hook_runner: HookRunner = run_hook,
    codex_client_factory: CodexClientPerWorkerFactory | None = None,
) -> None:
    """Run one §16.5 worker attempt for ``issue``.

    Either ``codex_client`` (singleton, reused across attempts — typical
    in tests) OR ``codex_client_factory`` (per-attempt construction —
    production wiring) must be provided. When ``codex_client_factory``
    is provided it builds a fresh client per attempt and ``codex_client``
    is ignored; when only ``codex_client`` is provided it is used directly
    (legacy/test path).

    Posts events to ``mailbox`` (one :class:`CodexUpdate` per agent event,
    one :class:`WorkerExit` at the very end) and returns ``None``. Never
    raises — failures are reported via :class:`WorkerExit`.

    All log records emitted from inside this attempt automatically carry
    ``issue_id`` and ``issue_identifier`` (§13.1). Once :meth:`start_session`
    succeeds, ``session_id`` is also pinned to the first turn's id; per-turn
    session_id rotation is observable through :class:`OrchestratorState` and
    not re-set in worker logs (the worker's session anchor stays at thread+
    first turn, which matches Codex's protocol-level identity).
    """
    if codex_client is None and codex_client_factory is None:
        raise ValueError(
            "run_agent_attempt: one of codex_client or "
            "codex_client_factory must be provided"
        )
    with set_log_context(
        issue_id=issue.id, issue_identifier=issue.identifier
    ):
        await _run_agent_attempt_impl(
            issue=issue,
            attempt=attempt,
            mailbox=mailbox,
            codex_client=codex_client,
            workspace_manager=workspace_manager,
            tracker=tracker,
            prompt_template=prompt_template,
            config=config,
            hook_runner=hook_runner,
            codex_client_factory=codex_client_factory,
        )


async def _run_agent_attempt_impl(
    *,
    issue: Issue,
    attempt: int,
    mailbox: Mailbox,
    codex_client: _CodexClientLike | None,
    workspace_manager: _WorkspaceManagerLike,
    tracker: _TrackerLike,
    prompt_template: str,
    config: EffectiveConfig,
    hook_runner: HookRunner,
    codex_client_factory: CodexClientPerWorkerFactory | None,
) -> None:
    started_at = datetime.now(UTC)
    session: Session | None = None
    reason: str = "normal"
    ok: bool = True
    last_error: str | None = None

    # Outer guard: enforce the "Never raises" contract from
    # :func:`run_agent_attempt`. Any exception not classified by the
    # explicit handlers below (e.g. transport closed mid-tool-dispatch,
    # approval-handler bug, unexpected RuntimeError out of stream_turn)
    # is mapped to a ``WorkerExit(reason="unexpected", ok=False)`` so the
    # worker task always settles cleanly and the orchestrator dispatcher
    # observes a terminal exit. Without this, a stray exception would
    # leave the worker asyncio.Task in exception state, the issue stuck
    # in ``state.running``, and only reaped by the stall timer.
    try:
        # 1. Render prompt — short-circuit on render failure (no workspace yet).
        try:
            rendered_prompt = render_prompt(
                prompt_template, issue=issue, attempt=attempt
            )
        except PromptError as exc:
            await _post_exit(
                mailbox,
                issue=issue,
                reason="prompt_failed",
                ok=False,
                started_at=started_at,
                last_error=str(exc),
            )
            return

        # 2. Ensure workspace.
        try:
            ensure_result = await workspace_manager.ensure_for_issue(issue.identifier)
        except WorkspaceManagerError as exc:
            await _post_exit(
                mailbox,
                issue=issue,
                reason="workspace_failed",
                ok=False,
                started_at=started_at,
                last_error=str(exc),
            )
            return
        except Exception as exc:  # noqa: BLE001 -- defensive against fake/real divergence
            await _post_exit(
                mailbox,
                issue=issue,
                reason="workspace_failed",
                ok=False,
                started_at=started_at,
                last_error=str(exc),
            )
            return

        workspace_path: Path = ensure_result.path

        # 2.5. Build per-attempt CodexClient via the production factory, if any.
        # The §10.1 subprocess lifecycle is bound to a single Codex session,
        # so production wiring constructs a fresh client per attempt — the
        # factory owns subprocess spawn + supported_tools + transport wiring.
        if codex_client_factory is not None:
            try:
                codex_client = await codex_client_factory(workspace_path, config)
            except Exception as exc:  # noqa: BLE001 -- factory failure → session_failed exit
                await _post_exit(
                    mailbox,
                    issue=issue,
                    reason="session_failed",
                    ok=False,
                    started_at=started_at,
                    last_error=f"codex_client_factory raised: {exc}",
                )
                return
        if codex_client is None:
            await _post_exit(
                mailbox,
                issue=issue,
                reason="session_failed",
                ok=False,
                started_at=started_at,
                last_error="no codex client available (no client and no factory)",
            )
            return

        # 3. before_run hook (no session open yet — skip after_run on failure).
        if config.hooks.before_run:
            result = await hook_runner(
                config.hooks.before_run,
                cwd=workspace_path,
                timeout_ms=config.hooks.timeout_ms,
            )
            if not result.ok:
                await _post_exit(
                    mailbox,
                    issue=issue,
                    reason="before_run_failed",
                    ok=False,
                    started_at=started_at,
                    last_error=_summarize_hook_failure("before_run", result),
                )
                return

        # 4. Start codex session. On failure, after_run still runs.
        try:
            session = await codex_client.start_session(
                workspace=workspace_path,
                prompt=rendered_prompt,
                issue=issue,
                approval_policy=config.codex.approval_policy or "never",
                sandbox_policy=config.codex.turn_sandbox_policy or "workspace-write",
                read_timeout_ms=config.codex.read_timeout_ms,
                tracker_kind=config.tracker.kind,
            )
        except CodexError as exc:
            await _run_after_run_best_effort(
                hook_runner, config, workspace_path
            )
            await _post_exit(
                mailbox,
                issue=issue,
                reason="session_failed",
                ok=False,
                started_at=started_at,
                last_error=str(exc),
            )
            return

        # Pin session_id into log context now that the handshake is complete.
        # Worker keeps the first-turn anchor; per-turn rotation is reflected
        # in OrchestratorState via on_codex_update, not in worker log lines.
        session_token = session_id_var.set(session.session_id)

        # 5. Streaming loop. session is non-None past here.
        try:
            reason, ok, last_error = await _run_turn_loop(
                issue=issue,
                attempt=attempt,
                mailbox=mailbox,
                codex_client=codex_client,
                tracker=tracker,
                session=session,
                rendered_prompt=rendered_prompt,
                config=config,
            )
        finally:
            # 6. Always-run cleanup: stop_session + after_run (best-effort).
            try:
                await codex_client.stop_session(session)
            except Exception:  # noqa: BLE001 -- log-only; stop must not raise out
                logger.warning(
                    "stop_session raised for issue %s — suppressing",
                    issue.identifier,
                    exc_info=True,
                )
            await _run_after_run_best_effort(hook_runner, config, workspace_path)

        # §13.1 outcome log — emitted inside the session_id context window so
        # the rendered line carries all three context fields. Keys live in
        # ``msg`` so the existing key=value formatter renders them without
        # needing record extras.
        logger.info("worker exit outcome=%s ok=%s", reason, ok)
        session_id_var.reset(session_token)

        await _post_exit(
            mailbox,
            issue=issue,
            reason=reason,
            ok=ok,
            started_at=started_at,
            last_error=last_error,
        )
    except Exception as exc:  # noqa: BLE001 -- enforce "Never raises" contract
        # Defensive catch-all: anything that escaped the typed handlers
        # above (transport closed mid-tool-call, approval-handler bug,
        # bare RuntimeError out of stream_turn, etc.) gets mapped to a
        # WorkerExit so the worker task settles cleanly.
        logger.error(
            "worker attempt for %s raised unexpected %s — converting to WorkerExit",
            issue.identifier,
            type(exc).__name__,
            exc_info=True,
        )
        await _post_exit(
            mailbox,
            issue=issue,
            reason="unexpected",
            ok=False,
            started_at=started_at,
            last_error=str(exc),
        )


# ---------------------------------------------------------------------------
# Streaming loop
# ---------------------------------------------------------------------------


async def _run_turn_loop(
    *,
    issue: Issue,
    attempt: int,
    mailbox: Mailbox,
    codex_client: _CodexClientLike,
    tracker: _TrackerLike,
    session: Session,
    rendered_prompt: str,
    config: EffectiveConfig,
) -> tuple[str, bool, str | None]:
    """Drive turns until exit. Returns (reason, ok, last_error)."""
    max_turns = config.agent.max_turns
    turn_timeout_ms = config.codex.turn_timeout_ms
    active_states_norm = {s.lower() for s in config.tracker.active_states}

    turn_number = 1
    current_issue = issue
    while True:
        prompt_for_turn = (
            rendered_prompt if turn_number == 1 else CONTINUATION_GUIDANCE
        )

        def _on_event(evt: RuntimeEvent, _id: str = current_issue.id) -> None:
            mailbox.send_nowait(CodexUpdate(issue_id=_id, event=evt))

        try:
            await codex_client.stream_turn(
                session=session,
                prompt=prompt_for_turn,
                on_event=_on_event,
                turn_timeout_ms=turn_timeout_ms,
                is_first_turn=(turn_number == 1),
            )
        except TurnFailed as exc:
            return ("turn_failed", False, str(exc))
        except TurnCancelled as exc:
            return ("turn_cancelled", False, str(exc))
        except TurnInputRequired as exc:
            return ("turn_input_required", False, str(exc))
        except TurnTimeout as exc:
            return ("turn_timeout", False, str(exc))
        except PortExit as exc:
            return ("port_exit", False, str(exc))

        # Turn succeeded. Stop if we've hit the per-attempt cap.
        if turn_number >= max_turns:
            return ("max_turns", False, None)

        # Refresh tracker state.
        try:
            refreshed = await tracker.fetch_issue_states_by_ids(
                [current_issue.id]
            )
        except LinearError as exc:
            return ("tracker_refresh_failed", False, str(exc))
        except Exception as exc:  # noqa: BLE001 -- defensive
            return ("tracker_refresh_failed", False, str(exc))

        # Issue absent (deleted / not visible) → treat as normal exit.
        if not refreshed:
            return ("normal", True, None)
        refreshed_issue = refreshed[0]
        if refreshed_issue.state.lower() not in active_states_norm:
            return ("normal", True, None)

        # Continue with the refreshed snapshot so subsequent turns see
        # the latest title/blockers/labels if we ever consult them.
        current_issue = refreshed_issue
        turn_number += 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _post_exit(
    mailbox: Mailbox,
    *,
    issue: Issue,
    reason: str,
    ok: bool,
    started_at: datetime,
    last_error: str | None,
) -> None:
    runtime_seconds = (datetime.now(UTC) - started_at).total_seconds()
    await mailbox.send(
        WorkerExit(
            issue_id=issue.id,
            reason=reason,
            ok=ok,
            runtime_seconds=runtime_seconds,
            last_error=last_error,
        )
    )


async def _run_after_run_best_effort(
    hook_runner: HookRunner,
    config: EffectiveConfig,
    workspace_path: Path,
) -> None:
    if not config.hooks.after_run:
        return
    try:
        result = await hook_runner(
            config.hooks.after_run,
            cwd=workspace_path,
            timeout_ms=config.hooks.timeout_ms,
        )
    except Exception:  # noqa: BLE001 -- §9.4: failures logged-but-ignored
        logger.warning(
            "after_run hook raised — suppressing per §9.4",
            exc_info=True,
        )
        return
    if not result.ok:
        logger.warning(
            "after_run hook returned non-ok: %s",
            _summarize_hook_failure("after_run", result),
        )


def _summarize_hook_failure(name: str, result: HookResult) -> str:
    if result.timed_out:
        return f"hook {name!r} timed out after {result.duration_ms}ms"
    if result.is_skipped:
        return f"hook {name!r} skipped"
    return f"hook {name!r} exited with code {result.exit_code}"


__all__ = [
    "CONTINUATION_GUIDANCE",
    "run_agent_attempt",
]
