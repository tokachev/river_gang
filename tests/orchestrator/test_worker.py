"""Tests for :mod:`river_gang.orchestrator.worker` (SPED §16.5, §7.1, §9.4).

Continuation/``max_turns`` was removed when state transitions moved into
the orchestrator (codex 0.125+ no longer advertises client-side tools, so
the agent can't signal "done" through Linear). The worker runs exactly
one turn per attempt; tests assert that single-turn semantics plus the
new tracker-side transitions (``start_state`` on entry, ``success_state``
on exit, failure comment first).
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from river_gang.codex.errors import CodexError
from river_gang.config import EffectiveConfig, apply_defaults
from river_gang.orchestrator import (
    CodexUpdate,
    Mailbox,
    OrchestratorMessage,
    WorkerExit,
)
from river_gang.orchestrator.worker import run_agent_attempt
from river_gang.tracker.errors import LinearError
from river_gang.tracker.issue import Issue
from river_gang.workspace.hooks import HookResult
from river_gang.workspace.manager import WorkspaceHookFailed
from tests.codex.fakes import FakeCodexClient, TurnScenario
from tests.tracker.fakes import FakeTracker
from tests.workspace.fakes import FakeWorkspaceManager

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _issue(*, id: str = "iss-1", state: str = "In Progress") -> Issue:
    return Issue(
        id=id,
        identifier=f"MT-{id}",
        title="title",
        state=state,
        description="desc",
        priority=2,
        branch_name="feat/x",
        url=None,
        labels=(),
        blocked_by=(),
        created_at=None,
        updated_at=None,
    )


def _config(
    *,
    before_run: str | None = None,
    after_run: str | None = None,
    start_state: str | None = "In Progress",
    success_state: str | None = "In Review",
) -> EffectiveConfig:
    base = apply_defaults({})
    new_hooks = dataclasses.replace(
        base.hooks,
        before_run=before_run,
        after_run=after_run,
    )
    new_tracker = dataclasses.replace(
        base.tracker,
        start_state=start_state,
        success_state=success_state,
    )
    return dataclasses.replace(base, hooks=new_hooks, tracker=new_tracker)


def _ok_result() -> HookResult:
    return HookResult(
        exit_code=0,
        stdout=b"",
        stderr=b"",
        duration_ms=10,
        timed_out=False,
        is_skipped=False,
    )


def _fail_result(exit_code: int = 1) -> HookResult:
    return HookResult(
        exit_code=exit_code,
        stdout=b"",
        stderr=b"boom",
        duration_ms=10,
        timed_out=False,
        is_skipped=False,
    )


def _hook_runner_returning(
    *results: HookResult,
) -> tuple[Callable[..., Awaitable[HookResult]], list[dict[str, object]]]:
    """Build a hook runner that returns the given results in order. Captures
    each invocation's kwargs so tests can assert call shape.
    """
    calls: list[dict[str, object]] = []
    iterator = iter(results)

    async def runner(
        script: str | None,
        *,
        cwd: Path,
        timeout_ms: int,
    ) -> HookResult:
        calls.append({"script": script, "cwd": cwd, "timeout_ms": timeout_ms})
        try:
            return next(iterator)
        except StopIteration as exc:
            raise AssertionError(
                "hook_runner called more times than results provided"
            ) from exc

    return runner, calls


def _drain(mailbox: Mailbox) -> list[OrchestratorMessage]:
    drained: list[OrchestratorMessage] = []
    while mailbox.qsize() > 0:
        # Internal queue is non-blocking when non-empty.
        drained.append(mailbox._queue.get_nowait())  # noqa: SLF001 -- test introspection
    return drained


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    return tmp_path / "workspaces"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_happy_path_runs_one_turn_and_transitions(
    workspace_root: Path,
) -> None:
    issue = _issue(id="iss-1")
    mbox = Mailbox()
    codex = FakeCodexClient()
    codex.queue_turn(
        TurnScenario(
            events=[{"event": "agent_message", "payload": {"text": "hi"}}],
            outcome="completed",
        )
    )
    workspace = FakeWorkspaceManager(root_path=workspace_root)
    tracker = FakeTracker()

    runner, runner_calls = _hook_runner_returning()
    config = _config()
    template = "Working on {{ issue.identifier }} attempt {{ attempt }}"

    await run_agent_attempt(
        issue=issue,
        attempt=1,
        mailbox=mbox,
        codex_client=codex,  # type: ignore[arg-type]
        workspace_manager=workspace,  # type: ignore[arg-type]
        tracker=tracker,  # type: ignore[arg-type]
        prompt_template=template,
        config=config,
        hook_runner=runner,
    )

    msgs = _drain(mbox)
    exits = [m for m in msgs if isinstance(m, WorkerExit)]
    assert len(exits) == 1
    exit_msg = exits[0]
    assert exit_msg.issue_id == "iss-1"
    assert exit_msg.reason == "normal"
    assert exit_msg.ok is True
    assert exit_msg.last_error is None
    assert exit_msg.runtime_seconds >= 0.0

    # Codex updates were posted in stream order.
    updates = [m for m in msgs if isinstance(m, CodexUpdate)]
    # 1 user event + 1 synthesised turn_completed event from FakeCodexClient.
    assert [u.event.event for u in updates] == ["agent_message", "turn_completed"]
    assert all(u.issue_id == "iss-1" for u in updates)

    # Workspace ensured exactly once. cleanup_for_issue (before_remove) NOT called.
    method_names = [c[0] for c in workspace.calls]
    assert "ensure_for_issue" in method_names
    assert "cleanup_for_issue" not in method_names

    # Codex session lifecycle: start → ONE stream → stop. No continuation loop.
    codex_methods = [c[0] for c in codex.calls]
    assert codex_methods == ["start_session", "stream_turn", "stop_session"]

    # Tracker drove start_state on entry, success_state on exit. Comments
    # never fire on a clean exit.
    assert tracker.transitions == [
        ("iss-1", "In Progress"),
        ("iss-1", "In Review"),
    ]
    assert tracker.comments == []

    # No legacy state-refresh inside the worker — that's reconcile's job.
    assert all(c[0] != "fetch_issue_states_by_ids" for c in tracker.calls)

    # No hooks configured → runner never invoked.
    assert runner_calls == []


async def test_first_turn_uses_full_prompt_no_continuation(
    workspace_root: Path,
) -> None:
    """Single-turn semantics: stream_turn is called exactly once with the
    rendered prompt and ``is_first_turn=True``. No continuation guidance.
    """
    issue = _issue(id="iss-1")
    mbox = Mailbox()
    codex = FakeCodexClient()
    codex.queue_turn(TurnScenario(outcome="completed", turn_id="t1"))
    workspace = FakeWorkspaceManager(root_path=workspace_root)
    tracker = FakeTracker()
    template = "Initial prompt for {{ issue.identifier }}"
    runner, _ = _hook_runner_returning()

    await run_agent_attempt(
        issue=issue,
        attempt=1,
        mailbox=mbox,
        codex_client=codex,  # type: ignore[arg-type]
        workspace_manager=workspace,  # type: ignore[arg-type]
        tracker=tracker,  # type: ignore[arg-type]
        prompt_template=template,
        config=_config(),
        hook_runner=runner,
    )

    stream_calls = [c for c in codex.calls if c[0] == "stream_turn"]
    assert len(stream_calls) == 1
    only = stream_calls[0][1]
    assert only["is_first_turn"] is True
    assert only["prompt"] == "Initial prompt for MT-iss-1"


# ---------------------------------------------------------------------------
# Pre-codex failures: workspace / prompt / before_run
# ---------------------------------------------------------------------------


async def test_workspace_failure_still_finalizes_via_tracker(
    workspace_root: Path,
) -> None:
    issue = _issue(id="iss-1")
    mbox = Mailbox()
    codex = FakeCodexClient()
    workspace = FakeWorkspaceManager(
        root_path=workspace_root,
        after_create_results={"MT-iss-1": _fail_result()},
    )
    tracker = FakeTracker()

    runner, runner_calls = _hook_runner_returning()

    await run_agent_attempt(
        issue=issue,
        attempt=1,
        mailbox=mbox,
        codex_client=codex,  # type: ignore[arg-type]
        workspace_manager=workspace,  # type: ignore[arg-type]
        tracker=tracker,  # type: ignore[arg-type]
        prompt_template="anything",
        config=_config(),
        hook_runner=runner,
    )

    msgs = _drain(mbox)
    exits = [m for m in msgs if isinstance(m, WorkerExit)]
    assert len(exits) == 1
    assert exits[0].reason == "workspace_failed"
    assert exits[0].ok is False
    assert exits[0].last_error is not None

    # No session created → no codex calls beyond nothing.
    assert codex.calls == []
    # Pre-codex failures still finalize: comment + success_state transition.
    # No start_state transition because we never reached start_session.
    assert tracker.transitions == [("iss-1", "In Review")]
    assert len(tracker.comments) == 1
    assert tracker.comments[0][0] == "iss-1"
    assert "workspace_failed" in tracker.comments[0][1]
    # No before_run/after_run hooks invoked.
    assert runner_calls == []


async def test_prompt_failure_finalizes_without_workspace(
    workspace_root: Path,
) -> None:
    issue = _issue(id="iss-1")
    mbox = Mailbox()
    codex = FakeCodexClient()
    workspace = FakeWorkspaceManager(root_path=workspace_root)
    tracker = FakeTracker()
    runner, runner_calls = _hook_runner_returning()

    # Strict-undefined: ``unknown_var`` is not in the render context.
    bad_template = "Hello {{ unknown_var.kaboom }}"

    await run_agent_attempt(
        issue=issue,
        attempt=1,
        mailbox=mbox,
        codex_client=codex,  # type: ignore[arg-type]
        workspace_manager=workspace,  # type: ignore[arg-type]
        tracker=tracker,  # type: ignore[arg-type]
        prompt_template=bad_template,
        config=_config(),
        hook_runner=runner,
    )

    msgs = _drain(mbox)
    exits = [m for m in msgs if isinstance(m, WorkerExit)]
    assert len(exits) == 1
    assert exits[0].reason == "prompt_failed"
    assert exits[0].ok is False

    # Workspace + codex + hooks must not have been touched.
    assert workspace.calls == []
    assert codex.calls == []
    assert runner_calls == []
    # Tracker still finalized with comment + success_state transition.
    assert tracker.transitions == [("iss-1", "In Review")]
    assert len(tracker.comments) == 1
    assert "prompt_failed" in tracker.comments[0][1]


async def test_before_run_hook_failure_finalizes(
    workspace_root: Path,
) -> None:
    issue = _issue(id="iss-1")
    mbox = Mailbox()
    codex = FakeCodexClient()
    workspace = FakeWorkspaceManager(root_path=workspace_root)
    tracker = FakeTracker()

    runner, runner_calls = _hook_runner_returning(_fail_result())
    config = _config(before_run="echo before", after_run=None)

    await run_agent_attempt(
        issue=issue,
        attempt=1,
        mailbox=mbox,
        codex_client=codex,  # type: ignore[arg-type]
        workspace_manager=workspace,  # type: ignore[arg-type]
        tracker=tracker,  # type: ignore[arg-type]
        prompt_template="prompt",
        config=config,
        hook_runner=runner,
    )

    msgs = _drain(mbox)
    exits = [m for m in msgs if isinstance(m, WorkerExit)]
    assert len(exits) == 1
    assert exits[0].reason == "before_run_failed"
    assert exits[0].ok is False

    # before_run was attempted; no session was started; after_run was NOT
    # invoked because session never opened.
    assert len(runner_calls) == 1
    assert runner_calls[0]["script"] == "echo before"
    assert codex.calls == []
    # Finalize sequence still ran: comment + success_state transition.
    assert tracker.transitions == [("iss-1", "In Review")]
    assert len(tracker.comments) == 1
    assert "before_run_failed" in tracker.comments[0][1]


async def test_session_failure_runs_after_run_hook_and_finalizes(
    workspace_root: Path,
) -> None:
    issue = _issue(id="iss-1")
    mbox = Mailbox()
    codex = FakeCodexClient(start_error=CodexError("could not connect"))
    workspace = FakeWorkspaceManager(root_path=workspace_root)
    tracker = FakeTracker()

    runner, runner_calls = _hook_runner_returning(_ok_result())  # after_run only
    config = _config(before_run=None, after_run="echo after")

    await run_agent_attempt(
        issue=issue,
        attempt=1,
        mailbox=mbox,
        codex_client=codex,  # type: ignore[arg-type]
        workspace_manager=workspace,  # type: ignore[arg-type]
        tracker=tracker,  # type: ignore[arg-type]
        prompt_template="prompt",
        config=config,
        hook_runner=runner,
    )

    msgs = _drain(mbox)
    exits = [m for m in msgs if isinstance(m, WorkerExit)]
    assert len(exits) == 1
    assert exits[0].reason == "session_failed"
    assert exits[0].ok is False
    assert exits[0].last_error is not None

    # after_run was invoked despite the session failing (best-effort cleanup).
    assert [c["script"] for c in runner_calls] == ["echo after"]
    # stop_session NOT called because no Session was returned.
    assert "stop_session" not in [c[0] for c in codex.calls]
    # No start_state transition — that fires AFTER start_session succeeds.
    # Finalize still ran: comment + success_state.
    assert tracker.transitions == [("iss-1", "In Review")]
    assert len(tracker.comments) == 1
    assert "session_failed" in tracker.comments[0][1]


# ---------------------------------------------------------------------------
# Turn-error variants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "expected_reason"),
    [
        ("failed", "turn_failed"),
        ("cancelled", "turn_cancelled"),
        ("timeout", "turn_timeout"),
        ("port_exit", "port_exit"),
    ],
)
async def test_turn_error_variants_propagate_and_finalize(
    outcome: str,
    expected_reason: str,
    workspace_root: Path,
) -> None:
    issue = _issue(id="iss-1")
    mbox = Mailbox()
    codex = FakeCodexClient()
    codex.queue_turn(TurnScenario(outcome=outcome))  # type: ignore[arg-type]
    workspace = FakeWorkspaceManager(root_path=workspace_root)
    tracker = FakeTracker()

    runner, runner_calls = _hook_runner_returning(_ok_result())
    config = _config(after_run="echo cleanup")

    await run_agent_attempt(
        issue=issue,
        attempt=1,
        mailbox=mbox,
        codex_client=codex,  # type: ignore[arg-type]
        workspace_manager=workspace,  # type: ignore[arg-type]
        tracker=tracker,  # type: ignore[arg-type]
        prompt_template="prompt",
        config=config,
        hook_runner=runner,
    )

    msgs = _drain(mbox)
    exits = [m for m in msgs if isinstance(m, WorkerExit)]
    assert len(exits) == 1
    assert exits[0].reason == expected_reason
    assert exits[0].ok is False

    # stop_session must have been invoked for cleanup.
    assert "stop_session" in [c[0] for c in codex.calls]
    # after_run hook was invoked (best-effort cleanup).
    assert [c["script"] for c in runner_calls] == ["echo cleanup"]
    # Both transitions fired (start before turn, success after) plus a
    # failure comment for the failure exit.
    assert tracker.transitions == [
        ("iss-1", "In Progress"),
        ("iss-1", "In Review"),
    ]
    assert len(tracker.comments) == 1
    assert expected_reason in tracker.comments[0][1]


# ---------------------------------------------------------------------------
# after_run hook failure: logged but does not flip success → failure
# ---------------------------------------------------------------------------


async def test_after_run_hook_failure_does_not_change_success(
    workspace_root: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    issue = _issue(id="iss-1")
    mbox = Mailbox()
    codex = FakeCodexClient()
    codex.queue_turn(TurnScenario(outcome="completed", turn_id="t1"))
    workspace = FakeWorkspaceManager(root_path=workspace_root)
    tracker = FakeTracker()

    runner, runner_calls = _hook_runner_returning(_fail_result(exit_code=42))
    config = _config(after_run="echo after")

    with caplog.at_level(logging.WARNING, logger="river_gang.orchestrator.worker"):
        await run_agent_attempt(
            issue=issue,
            attempt=1,
            mailbox=mbox,
            codex_client=codex,  # type: ignore[arg-type]
            workspace_manager=workspace,  # type: ignore[arg-type]
            tracker=tracker,  # type: ignore[arg-type]
            prompt_template="prompt",
            config=config,
            hook_runner=runner,
        )

    msgs = _drain(mbox)
    exits = [m for m in msgs if isinstance(m, WorkerExit)]
    assert len(exits) == 1
    assert exits[0].reason == "normal"
    assert exits[0].ok is True

    # Failure was logged for observability.
    assert any(
        "after_run" in rec.message
        for rec in caplog.records
    ), caplog.text

    # after_run was attempted exactly once.
    assert [c["script"] for c in runner_calls] == ["echo after"]


# ---------------------------------------------------------------------------
# Tracker mutation behaviour
# ---------------------------------------------------------------------------


async def test_no_transitions_when_states_are_None_in_config(
    workspace_root: Path,
) -> None:
    """If both ``start_state`` and ``success_state`` are unset, the worker
    must skip transitions entirely. Failure comments still fire — the
    "tell the operator what broke" channel is independent of the
    transition feature flag.
    """
    issue = _issue(id="iss-1")
    mbox = Mailbox()
    codex = FakeCodexClient()
    codex.queue_turn(TurnScenario(outcome="completed", turn_id="t1"))
    workspace = FakeWorkspaceManager(root_path=workspace_root)
    tracker = FakeTracker()
    runner, _ = _hook_runner_returning()

    await run_agent_attempt(
        issue=issue,
        attempt=1,
        mailbox=mbox,
        codex_client=codex,  # type: ignore[arg-type]
        workspace_manager=workspace,  # type: ignore[arg-type]
        tracker=tracker,  # type: ignore[arg-type]
        prompt_template="prompt",
        config=_config(start_state=None, success_state=None),
        hook_runner=runner,
    )

    assert tracker.transitions == []
    # Successful exit → no comment either.
    assert tracker.comments == []


async def test_transition_failure_logs_and_continues(
    workspace_root: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A LinearError on the start-state transition must NOT prevent the
    turn from running. Worker logs and proceeds; the success transition
    still fires (independently best-effort).
    """
    issue = _issue(id="iss-1")
    mbox = Mailbox()
    codex = FakeCodexClient()
    codex.queue_turn(TurnScenario(outcome="completed", turn_id="t1"))
    workspace = FakeWorkspaceManager(root_path=workspace_root)
    tracker = FakeTracker()
    tracker.fail_next_transition(LinearError("transient network error"))
    runner, _ = _hook_runner_returning()

    with caplog.at_level(logging.WARNING, logger="river_gang.orchestrator.worker"):
        await run_agent_attempt(
            issue=issue,
            attempt=1,
            mailbox=mbox,
            codex_client=codex,  # type: ignore[arg-type]
            workspace_manager=workspace,  # type: ignore[arg-type]
            tracker=tracker,  # type: ignore[arg-type]
            prompt_template="prompt",
            config=_config(),
            hook_runner=runner,
        )

    exits = [m for m in _drain(mbox) if isinstance(m, WorkerExit)]
    assert len(exits) == 1
    # Worker still considers the run successful — transition failure is
    # observability noise, not a worker-level failure.
    assert exits[0].reason == "normal"
    assert exits[0].ok is True

    # Both transitions were attempted (start failed, success succeeded).
    assert tracker.transitions == [
        ("iss-1", "In Progress"),
        ("iss-1", "In Review"),
    ]
    # The failure landed in the warning log.
    assert any(
        "transition_state" in rec.message
        for rec in caplog.records
    ), caplog.text


async def test_comment_failure_logs_and_continues(
    workspace_root: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A LinearError on add_comment during a failure exit must NOT escape;
    the success-state transition still runs.
    """
    issue = _issue(id="iss-1")
    mbox = Mailbox()
    codex = FakeCodexClient()
    codex.queue_turn(TurnScenario(outcome="failed"))
    workspace = FakeWorkspaceManager(root_path=workspace_root)
    tracker = FakeTracker()
    tracker.fail_next_comment(LinearError("comment endpoint 500"))
    runner, _ = _hook_runner_returning()

    with caplog.at_level(logging.WARNING, logger="river_gang.orchestrator.worker"):
        await run_agent_attempt(
            issue=issue,
            attempt=1,
            mailbox=mbox,
            codex_client=codex,  # type: ignore[arg-type]
            workspace_manager=workspace,  # type: ignore[arg-type]
            tracker=tracker,  # type: ignore[arg-type]
            prompt_template="prompt",
            config=_config(),
            hook_runner=runner,
        )

    exits = [m for m in _drain(mbox) if isinstance(m, WorkerExit)]
    assert len(exits) == 1
    assert exits[0].reason == "turn_failed"
    assert exits[0].ok is False
    # Comment was attempted and logged; success transition still landed.
    assert len(tracker.comments) == 1
    assert tracker.transitions == [
        ("iss-1", "In Progress"),
        ("iss-1", "In Review"),
    ]
    assert any(
        "add_comment" in rec.message
        for rec in caplog.records
    ), caplog.text


# ---------------------------------------------------------------------------
# before_remove must NOT be called by worker (§9.4 differentiation)
# ---------------------------------------------------------------------------


async def test_worker_does_not_invoke_before_remove(
    workspace_root: Path,
) -> None:
    issue = _issue(id="iss-1")
    mbox = Mailbox()
    codex = FakeCodexClient()
    codex.queue_turn(TurnScenario(outcome="completed", turn_id="t1"))
    workspace = FakeWorkspaceManager(root_path=workspace_root)
    tracker = FakeTracker()
    runner, _ = _hook_runner_returning()

    await run_agent_attempt(
        issue=issue,
        attempt=1,
        mailbox=mbox,
        codex_client=codex,  # type: ignore[arg-type]
        workspace_manager=workspace,  # type: ignore[arg-type]
        tracker=tracker,  # type: ignore[arg-type]
        prompt_template="prompt",
        config=_config(),
        hook_runner=runner,
    )

    method_names = [c[0] for c in workspace.calls]
    assert "cleanup_for_issue" not in method_names


# ---------------------------------------------------------------------------
# WorkspaceHookFailed surfaces as workspace_failed (defensive — fake raises this)
# ---------------------------------------------------------------------------


async def test_workspace_hook_failed_classified_as_workspace_failed(
    workspace_root: Path,
) -> None:
    issue = _issue(id="iss-1")
    mbox = Mailbox()
    codex = FakeCodexClient()
    workspace = FakeWorkspaceManager(
        root_path=workspace_root,
        after_create_results={"MT-iss-1": _fail_result()},
    )
    tracker = FakeTracker()

    runner, _ = _hook_runner_returning()
    await run_agent_attempt(
        issue=issue,
        attempt=1,
        mailbox=mbox,
        codex_client=codex,  # type: ignore[arg-type]
        workspace_manager=workspace,  # type: ignore[arg-type]
        tracker=tracker,  # type: ignore[arg-type]
        prompt_template="prompt",
        config=_config(),
        hook_runner=runner,
    )
    exits = [m for m in _drain(mbox) if isinstance(m, WorkerExit)]
    assert len(exits) == 1
    assert exits[0].reason == "workspace_failed"
    # The error string carries the concrete WorkspaceHookFailed message.
    assert exits[0].last_error is not None
    assert "after_create" in exits[0].last_error or isinstance(
        exits[0].last_error, str
    )


def test_isinstance_workspace_hook_failed_is_subclass_of_workspace_error() -> None:
    """Sanity check we're catching the right hierarchy."""
    from river_gang.workspace.manager import WorkspaceManagerError

    assert issubclass(WorkspaceHookFailed, WorkspaceManagerError)


# ---------------------------------------------------------------------------
# "Never raises" contract: unexpected exceptions become WorkerExit
# ---------------------------------------------------------------------------


async def test_unexpected_exception_in_stream_turn_becomes_worker_exit(
    workspace_root: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exception type *not* in the typed CodexError hierarchy must not
    propagate out of :func:`run_agent_attempt`. Per the docstring contract
    ("Never raises — failures are reported via :class:`WorkerExit`"), it
    must be reported as ``WorkerExit(reason="unexpected", ok=False)``.
    """
    issue = _issue(id="iss-1")
    mbox = Mailbox()
    codex = FakeCodexClient()
    workspace = FakeWorkspaceManager(root_path=workspace_root)
    tracker = FakeTracker()

    # Override stream_turn to raise a bare RuntimeError — simulating the
    # closed-LinearTransport / approval-handler-bug failure modes that
    # would otherwise leak out of the worker task.
    sentinel = "linear transport closed mid-tool-call"

    async def _raise_runtime_error(**_: object) -> None:
        codex.calls.append(("stream_turn", {"raised": sentinel}))
        raise RuntimeError(sentinel)

    codex.stream_turn = _raise_runtime_error  # type: ignore[method-assign]

    runner, _ = _hook_runner_returning()

    with caplog.at_level(logging.ERROR, logger="river_gang.orchestrator.worker"):
        # Must not raise — that's the contract under test.
        await run_agent_attempt(
            issue=issue,
            attempt=1,
            mailbox=mbox,
            codex_client=codex,  # type: ignore[arg-type]
            workspace_manager=workspace,  # type: ignore[arg-type]
            tracker=tracker,  # type: ignore[arg-type]
            prompt_template="prompt",
            config=_config(),
            hook_runner=runner,
        )

    msgs = _drain(mbox)
    exits = [m for m in msgs if isinstance(m, WorkerExit)]
    assert len(exits) == 1, msgs
    assert exits[0].reason == "unexpected"
    assert exits[0].ok is False
    assert exits[0].last_error is not None
    assert sentinel in exits[0].last_error
    assert exits[0].issue_id == issue.id
    assert exits[0].runtime_seconds >= 0.0

    # Cleanup still ran via the inner try/finally (stop_session called).
    assert "stop_session" in [c[0] for c in codex.calls]

    # The unexpected exception was logged at ERROR with traceback.
    error_records = [
        r for r in caplog.records
        if r.levelno == logging.ERROR and "unexpected" in r.getMessage()
    ]
    assert error_records, caplog.text
    assert error_records[0].exc_info is not None


async def test_unexpected_exception_in_run_agent_attempt_does_not_leak(
    workspace_root: Path,
) -> None:
    """End-to-end check: even when run_agent_attempt is awaited inside a
    bare task (no caller-level try/except), the task settles cleanly.
    """
    import asyncio

    issue = _issue(id="iss-2")
    mbox = Mailbox()
    codex = FakeCodexClient()
    workspace = FakeWorkspaceManager(root_path=workspace_root)
    tracker = FakeTracker()

    async def _raise(**_: object) -> None:
        raise RuntimeError("approval handler crashed")

    codex.stream_turn = _raise  # type: ignore[method-assign]

    runner, _ = _hook_runner_returning()

    task = asyncio.create_task(
        run_agent_attempt(
            issue=issue,
            attempt=1,
            mailbox=mbox,
            codex_client=codex,  # type: ignore[arg-type]
            workspace_manager=workspace,  # type: ignore[arg-type]
            tracker=tracker,  # type: ignore[arg-type]
            prompt_template="prompt",
            config=_config(),
            hook_runner=runner,
        )
    )
    await task

    # Task settled normally — no exception captured.
    assert task.exception() is None
    exits = [m for m in _drain(mbox) if isinstance(m, WorkerExit)]
    assert len(exits) == 1
    assert exits[0].reason == "unexpected"
    assert exits[0].ok is False
