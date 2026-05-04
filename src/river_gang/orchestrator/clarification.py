"""Pre-dispatch clarification gate."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from river_gang.codex.client import Session, TurnResult
from river_gang.config import EffectiveConfig
from river_gang.tracker.comment import Comment
from river_gang.tracker.issue import Issue

CLARIFICATION_POLL_INTERVAL_MS = 300_000
CLARIFICATION_COMMENT_MARKER = "<!-- river-gang clarification request -->"


@dataclass(frozen=True)
class ClarificationDecision:
    """Result of analyzing whether an issue is ready for implementation."""

    sufficient: bool
    questions: tuple[str, ...] = ()
    rationale: str | None = None


@dataclass(frozen=True)
class ClarificationWaitEntry:
    issue_id: str
    identifier: str
    last_seen_comment_ids: frozenset[str]
    last_question_fingerprint: str | None
    next_poll_at: datetime
    last_input_fingerprint: str | None = None


class ClarificationGate(Protocol):
    async def analyze(
        self,
        *,
        issue: Issue,
        comments: tuple[Comment, ...],
        workspace_path: Path,
        config: EffectiveConfig,
    ) -> ClarificationDecision: ...


class NoopClarificationGate:
    """Gate implementation used when no model-backed gate is wired."""

    async def analyze(
        self,
        *,
        issue: Issue,
        comments: tuple[Comment, ...],
        workspace_path: Path,
        config: EffectiveConfig,
    ) -> ClarificationDecision:
        return ClarificationDecision(sufficient=True)


CodexClientFactory = Callable[[Path, EffectiveConfig], Awaitable[Any]]


class CodexClarificationGate:
    """Model-backed gate that asks Codex to inspect the issue and codebase.

    The gate runs a separate read-only analysis turn before the implementation
    worker is dispatched. The model must return JSON; if parsing fails, the gate
    fails closed by returning an insufficient decision asking for operator help.
    """

    def __init__(self, client_factory: CodexClientFactory) -> None:
        self._client_factory = client_factory

    async def analyze(
        self,
        *,
        issue: Issue,
        comments: tuple[Comment, ...],
        workspace_path: Path,
        config: EffectiveConfig,
    ) -> ClarificationDecision:
        client = await self._client_factory(workspace_path, config)
        session: Session | None = None
        try:
            prompt = build_clarification_prompt(issue=issue, comments=comments)
            session = await client.start_session(
                workspace=workspace_path,
                prompt=prompt,
                issue=issue,
                approval_policy="never",
                sandbox_policy="read-only",
                read_timeout_ms=config.codex.read_timeout_ms,
                tracker_kind=config.tracker.kind,
            )
            result: TurnResult = await client.stream_turn(
                session=session,
                prompt=prompt,
                on_event=lambda _evt: None,
                turn_timeout_ms=config.codex.turn_timeout_ms,
                is_first_turn=True,
            )
            return parse_clarification_decision_payload(result.payload)
        finally:
            if session is not None:
                await client.stop_session(session)


def build_clarification_prompt(
    *, issue: Issue, comments: tuple[Comment, ...]
) -> str:
    comments_text = "\n\n".join(
        f"Comment {idx + 1} ({comment.user_name or 'unknown'}):\n{comment.body}"
        for idx, comment in enumerate(comments)
        if not is_clarification_comment(comment)
    )
    if not comments_text:
        comments_text = "No user comments yet."

    return f"""You are the river-gang pre-implementation clarification gate.

Analyze the Linear issue text AND the repository codebase available in the current
workspace before any implementation starts.

Decide whether there is enough information to implement the task safely. There is
enough information only when the final expected result is concrete and the
relevant edge cases are either specified in the issue/comments or can be
unambiguously inferred from the codebase.

If the final result is unclear, or any important edge-case decision is unclear,
return questions for the operator. Do not modify files. Do not create commits.
Do not open PRs. Do not transition Linear state. Return JSON only.

JSON schema:
{{
  "sufficient": true | false,
  "questions": ["question for Linear comment", ...],
  "rationale": "short reason"
}}

Issue:
Identifier: {issue.identifier}
Title: {issue.title}
Description:
{issue.description or ''}

Existing user comments:
{comments_text}
"""


def parse_clarification_decision_payload(payload: dict[str, Any]) -> ClarificationDecision:
    text = _extract_first_json_text(payload)
    if text is None:
        return ClarificationDecision(
            sufficient=False,
            questions=(
                "The pre-implementation analysis did not return valid JSON. "
                "Please clarify the expected final result and edge cases.",
            ),
            rationale="missing model JSON",
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match is None:
            return _malformed_json_decision()
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return _malformed_json_decision()

    sufficient = data.get("sufficient") is True if isinstance(data, dict) else False
    raw_questions = data.get("questions") if isinstance(data, dict) else None
    questions: tuple[str, ...] = ()
    if isinstance(raw_questions, list):
        questions = tuple(q for q in raw_questions if isinstance(q, str) and q.strip())
    rationale = data.get("rationale") if isinstance(data, dict) else None
    return ClarificationDecision(
        sufficient=sufficient,
        questions=questions,
        rationale=rationale if isinstance(rationale, str) else None,
    )


def _malformed_json_decision() -> ClarificationDecision:
    return ClarificationDecision(
        sufficient=False,
        questions=(
            "The pre-implementation analysis returned malformed JSON. "
            "Please clarify the expected final result and edge cases.",
        ),
        rationale="malformed model JSON",
    )


def is_clarification_comment(comment: Comment) -> bool:
    return CLARIFICATION_COMMENT_MARKER in comment.body


def non_clarification_comment_ids(comments: tuple[Comment, ...]) -> frozenset[str]:
    return frozenset(c.id for c in comments if not is_clarification_comment(c))


def fingerprint_clarification_inputs(
    issue: Issue, comments: tuple[Comment, ...]
) -> str:
    """Fingerprint issue/comment content that can answer clarification questions."""

    input_parts: list[str] = [
        issue.id,
        issue.identifier,
        issue.title,
        issue.description or "",
        issue.updated_at.isoformat() if issue.updated_at is not None else "",
    ]
    for comment in comments:
        if is_clarification_comment(comment):
            continue
        input_parts.extend(
            [
                comment.id,
                comment.body,
                comment.created_at.isoformat()
                if comment.created_at is not None
                else "",
                comment.updated_at.isoformat()
                if comment.updated_at is not None
                else "",
            ]
        )
    return sha256("\0".join(input_parts).encode("utf-8")).hexdigest()


def fingerprint_questions(questions: tuple[str, ...]) -> str:
    normalized = "\n".join(q.strip() for q in questions if q.strip())
    return sha256(normalized.encode("utf-8")).hexdigest()


def next_clarification_poll_at(now: datetime) -> datetime:
    return now + timedelta(milliseconds=CLARIFICATION_POLL_INTERVAL_MS)


def format_clarification_comment(decision: ClarificationDecision) -> str:
    questions = [q.strip() for q in decision.questions if q.strip()]
    if not questions:
        questions = ["Please clarify the expected final result and important edge cases."]
    rendered = "\n".join(f"{idx + 1}. {q}" for idx, q in enumerate(questions))
    rationale = f"\n\nReason: {decision.rationale}" if decision.rationale else ""
    return (
        f"{CLARIFICATION_COMMENT_MARKER}\n\n"
        "Before implementation, river-gang needs clarification:\n\n"
        f"{rendered}"
        f"{rationale}"
    )


def _extract_first_json_text(payload: Any) -> str | None:
    for text in _walk_strings(payload):
        stripped = text.strip()
        if stripped.startswith("{") or "{" in stripped:
            return stripped
    return None


def _walk_strings(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for child in value.values():
            out.extend(_walk_strings(child))
    elif isinstance(value, list):
        for child in value:
            out.extend(_walk_strings(child))
    return out


__all__ = [
    "CLARIFICATION_COMMENT_MARKER",
    "CLARIFICATION_POLL_INTERVAL_MS",
    "ClarificationDecision",
    "ClarificationGate",
    "ClarificationWaitEntry",
    "CodexClarificationGate",
    "NoopClarificationGate",
    "fingerprint_clarification_inputs",
    "fingerprint_questions",
    "format_clarification_comment",
    "is_clarification_comment",
    "next_clarification_poll_at",
    "non_clarification_comment_ids",
    "parse_clarification_decision_payload",
]
