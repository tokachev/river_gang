"""Tests for the pre-dispatch clarification gate."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from river_gang.config.defaults import apply_defaults
from river_gang.orchestrator.clarification import (
    CLARIFICATION_COMMENT_MARKER,
    ClarificationDecision,
    ClarificationWaitEntry,
    fingerprint_clarification_inputs,
    parse_clarification_decision_payload,
)
from river_gang.orchestrator.loop import Orchestrator
from river_gang.orchestrator.mailbox import Mailbox
from river_gang.orchestrator.retry import RetryQueue
from river_gang.orchestrator.state import OrchestratorState
from river_gang.tracker.comment import Comment
from river_gang.tracker.issue import Issue
from tests.codex.fakes import FakeCodexClient, TurnScenario
from tests.tracker.fakes import FakeTracker
from tests.workspace.fakes import FakeWorkspaceManager


@dataclass
class FakeClarificationGate:
    decisions: list[ClarificationDecision]
    calls: list[dict[str, Any]]

    async def analyze(self, **kwargs: Any) -> ClarificationDecision:
        self.calls.append(kwargs)
        assert self.decisions, "test must queue a clarification decision"
        return self.decisions.pop(0)


def _issue(
    *,
    id: str = "issue-1",
    description: str | None = "Build the feature",
    updated_at: datetime | None = None,
) -> Issue:
    return Issue(
        id=id,
        identifier="TES-1",
        title="Add feature",
        state="Todo",
        description=description,
        priority=1,
        branch_name=None,
        url=None,
        labels=(),
        blocked_by=(),
        created_at=datetime(2026, 5, 4, tzinfo=UTC),
        updated_at=updated_at,
    )


def _comment(id: str, body: str, *, updated_at: datetime | None = None) -> Comment:
    return Comment(
        id=id,
        body=body,
        created_at=datetime(2026, 5, 4, tzinfo=UTC),
        updated_at=updated_at,
        user_id="user-1",
        user_name="Artem",
    )


def _config() -> Any:
    return apply_defaults(
        {
            "tracker": {"kind": "linear", "active_states": ["Todo"]},
            "polling": {"interval_ms": 30_000},
            "agent": {"max_concurrent_agents": 1},
        }
    )


def _make_orchestrator(
    *,
    tmp_path: Path,
    tracker: FakeTracker,
    gate: FakeClarificationGate,
    state: OrchestratorState | None = None,
) -> Orchestrator:
    config = _config()
    codex = FakeCodexClient()
    codex.queue_turn(TurnScenario())
    return Orchestrator(
        state=state
        or OrchestratorState(
            poll_interval_ms=config.polling.interval_ms,
            max_concurrent_agents=config.agent.max_concurrent_agents,
        ),
        mailbox=Mailbox(),
        retry_queue=RetryQueue(loop=asyncio.get_running_loop()),
        tracker=tracker,
        codex_client=codex,
        workspace_manager=FakeWorkspaceManager(root_path=tmp_path),
        prompt_template="implement {{ issue.identifier }}",
        config=config,
        workflow_loader=lambda: config,
        clarification_gate=gate,
        clock=lambda: datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
    )


async def test_sufficient_clarification_dispatches_normally(tmp_path: Path) -> None:
    issue = _issue()
    tracker = FakeTracker(candidates=[issue], comments_by_issue={issue.id: []})
    gate = FakeClarificationGate(
        decisions=[ClarificationDecision(sufficient=True)], calls=[]
    )
    orch = _make_orchestrator(tmp_path=tmp_path, tracker=tracker, gate=gate)

    await orch.on_tick()

    assert set(orch.state.running) == {issue.id}
    assert orch.state.clarification_waiting == {}
    assert len(gate.calls) == 1
    assert tracker.comments == []
    await orch.shutdown_workers()
    orch.cancel_next_tick()


async def test_insufficient_clarification_posts_questions_and_waits(
    tmp_path: Path,
) -> None:
    issue = _issue(description="Do stuff")
    tracker = FakeTracker(candidates=[issue], comments_by_issue={issue.id: []})
    gate = FakeClarificationGate(
        decisions=[
            ClarificationDecision(
                sufficient=False,
                questions=("What should the final command output be?",),
                rationale="missing acceptance criteria",
            )
        ],
        calls=[],
    )
    orch = _make_orchestrator(tmp_path=tmp_path, tracker=tracker, gate=gate)

    await orch.on_tick()

    assert orch.state.running == {}
    wait = orch.state.clarification_waiting[issue.id]
    assert wait.identifier == issue.identifier
    assert wait.last_seen_comment_ids == frozenset()
    assert wait.next_poll_at == datetime(2026, 5, 4, 12, 5, tzinfo=UTC)
    assert len(tracker.comments) == 1
    assert CLARIFICATION_COMMENT_MARKER in tracker.comments[0][1]
    assert "What should the final command output be?" in tracker.comments[0][1]
    orch.cancel_next_tick()


async def test_waiting_issue_before_five_minutes_skips_fetch_and_dispatch(
    tmp_path: Path,
) -> None:
    issue = _issue()
    state = OrchestratorState(poll_interval_ms=30_000, max_concurrent_agents=1)
    state.clarification_waiting[issue.id] = ClarificationWaitEntry(
        issue_id=issue.id,
        identifier=issue.identifier,
        last_seen_comment_ids=frozenset(),
        last_question_fingerprint="abc",
        next_poll_at=datetime(2026, 5, 4, 12, 0, tzinfo=UTC) + timedelta(minutes=1),
    )
    tracker = FakeTracker(candidates=[issue], comments_by_issue={issue.id: []})
    gate = FakeClarificationGate(decisions=[], calls=[])
    orch = _make_orchestrator(
        tmp_path=tmp_path, tracker=tracker, gate=gate, state=state
    )

    await orch.on_tick()

    assert orch.state.running == {}
    assert gate.calls == []
    assert not any(call[0] == "fetch_comments" for call in tracker.calls)
    orch.cancel_next_tick()


async def test_new_comment_after_wait_reanalyzes_and_dispatches(
    tmp_path: Path,
) -> None:
    issue = _issue()
    state = OrchestratorState(poll_interval_ms=30_000, max_concurrent_agents=1)
    state.clarification_waiting[issue.id] = ClarificationWaitEntry(
        issue_id=issue.id,
        identifier=issue.identifier,
        last_seen_comment_ids=frozenset({"bot-comment"}),
        last_question_fingerprint="abc",
        next_poll_at=datetime(2026, 5, 4, 11, 59, tzinfo=UTC),
    )
    tracker = FakeTracker(
        candidates=[issue],
        comments_by_issue={
            issue.id: [
                _comment("bot-comment", CLARIFICATION_COMMENT_MARKER),
                _comment("human-answer", "The final result is X."),
            ]
        },
    )
    gate = FakeClarificationGate(
        decisions=[ClarificationDecision(sufficient=True)], calls=[]
    )
    orch = _make_orchestrator(
        tmp_path=tmp_path, tracker=tracker, gate=gate, state=state
    )

    await orch.on_tick()

    assert set(orch.state.running) == {issue.id}
    assert issue.id not in orch.state.clarification_waiting
    assert len(gate.calls) == 1
    assert gate.calls[0]["comments"][-1].id == "human-answer"
    await orch.shutdown_workers()
    orch.cancel_next_tick()


def test_string_false_decision_payload_fails_closed() -> None:
    decision = parse_clarification_decision_payload(
        {"message": '{"sufficient": "false", "questions": []}'}
    )

    assert decision.sufficient is False


def test_malformed_fallback_json_payload_fails_closed() -> None:
    decision = parse_clarification_decision_payload(
        {"message": 'prefix {"sufficient": true, "questions": [} suffix'}
    )

    assert decision.sufficient is False
    assert decision.rationale == "malformed model JSON"
    assert decision.questions


async def test_edited_comment_after_wait_reanalyzes_and_dispatches(
    tmp_path: Path,
) -> None:
    issue = _issue()
    old_comments = (_comment("human-answer", "Still not enough detail"),)
    state = OrchestratorState(poll_interval_ms=30_000, max_concurrent_agents=1)
    state.clarification_waiting[issue.id] = ClarificationWaitEntry(
        issue_id=issue.id,
        identifier=issue.identifier,
        last_seen_comment_ids=frozenset({"human-answer"}),
        last_question_fingerprint="abc",
        next_poll_at=datetime(2026, 5, 4, 11, 59, tzinfo=UTC),
        last_input_fingerprint=fingerprint_clarification_inputs(issue, old_comments),
    )
    tracker = FakeTracker(
        candidates=[issue],
        comments_by_issue={
            issue.id: [
                _comment(
                    "human-answer",
                    "Final result and edge cases are now fully specified.",
                    updated_at=datetime(2026, 5, 4, 12, 1, tzinfo=UTC),
                )
            ]
        },
    )
    gate = FakeClarificationGate(
        decisions=[ClarificationDecision(sufficient=True)], calls=[]
    )
    orch = _make_orchestrator(
        tmp_path=tmp_path, tracker=tracker, gate=gate, state=state
    )

    await orch.on_tick()

    assert set(orch.state.running) == {issue.id}
    assert len(gate.calls) == 1
    assert gate.calls[0]["comments"][0].body.startswith("Final result")
    await orch.shutdown_workers()
    orch.cancel_next_tick()


async def test_updated_issue_text_after_wait_reanalyzes_and_dispatches(
    tmp_path: Path,
) -> None:
    old_issue = _issue(description="Do the thing")
    new_issue = _issue(
        description="Do the thing and handle empty, malformed, and duplicate inputs",
        updated_at=datetime(2026, 5, 4, 12, 1, tzinfo=UTC),
    )
    state = OrchestratorState(poll_interval_ms=30_000, max_concurrent_agents=1)
    state.clarification_waiting[new_issue.id] = ClarificationWaitEntry(
        issue_id=new_issue.id,
        identifier=new_issue.identifier,
        last_seen_comment_ids=frozenset(),
        last_question_fingerprint="abc",
        next_poll_at=datetime(2026, 5, 4, 11, 59, tzinfo=UTC),
        last_input_fingerprint=fingerprint_clarification_inputs(old_issue, ()),
    )
    tracker = FakeTracker(candidates=[new_issue], comments_by_issue={new_issue.id: []})
    gate = FakeClarificationGate(
        decisions=[ClarificationDecision(sufficient=True)], calls=[]
    )
    orch = _make_orchestrator(
        tmp_path=tmp_path, tracker=tracker, gate=gate, state=state
    )

    await orch.on_tick()

    assert set(orch.state.running) == {new_issue.id}
    assert len(gate.calls) == 1
    assert "malformed" in gate.calls[0]["issue"].description
    await orch.shutdown_workers()
    orch.cancel_next_tick()


async def test_still_insufficient_with_same_questions_does_not_duplicate_comment(
    tmp_path: Path,
) -> None:
    issue = _issue()
    question = "Which command should be added?"
    initial = ClarificationDecision(sufficient=False, questions=(question,))
    tracker = FakeTracker(candidates=[issue], comments_by_issue={issue.id: []})
    gate = FakeClarificationGate(decisions=[initial], calls=[])
    orch = _make_orchestrator(tmp_path=tmp_path, tracker=tracker, gate=gate)

    await orch.on_tick()
    assert len(tracker.comments) == 1

    wait = orch.state.clarification_waiting[issue.id]
    orch.state.clarification_waiting[issue.id] = ClarificationWaitEntry(
        issue_id=wait.issue_id,
        identifier=wait.identifier,
        last_seen_comment_ids=wait.last_seen_comment_ids,
        last_question_fingerprint=wait.last_question_fingerprint,
        next_poll_at=datetime(2026, 5, 4, 11, 59, tzinfo=UTC),
    )
    tracker.set_comments(issue.id, [_comment("human-answer", "Not enough")])
    gate.decisions.append(ClarificationDecision(sufficient=False, questions=(question,)))

    await orch.on_tick()

    assert len(tracker.comments) == 1
    assert orch.state.running == {}
    orch.cancel_next_tick()
