"""``GET /api/v1/{identifier}`` — per-issue detail endpoint (SPED §13.7.2).

Returns ``running`` / ``retry`` blocks depending on which side of the
state machine currently owns the issue. ``status`` discriminator is one
of ``"running" | "retrying" | "completed" | "unknown"`` (the ``unknown``
branch surfaces only via the 404 envelope; clients see status set to
exactly one of the first three on a 200 response).

Lookup uses :attr:`OrchestratorState.identifier_index` (a stable
``issue_id → identifier`` map maintained on every ``add_running`` and
never cleared) so retry-only and completed-only entries — neither of
which carry their identifier directly — resolve correctly. Identifier
matching is **case-sensitive** to match Linear's URL-friendly slugs and
keep request routing predictable.

Deferred fields (left empty in M9, populated in later milestones):

- ``logs.codex_session_logs`` — empty list. Real codex log paths land
  alongside per-session log capture in M10.
- ``tracked`` — empty dict. Reserved for tracker-side debug fields.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from river_gang.orchestrator.retry import RetryEntry, RetryQueue
from river_gang.orchestrator.state import OrchestratorState, RunningEntry

# ---------------------------------------------------------------------------
# Pydantic view models
# ---------------------------------------------------------------------------


class TokensView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int
    output_tokens: int
    total_tokens: int


class IssueAttempts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    restart_count: int
    current_retry_attempt: int | None


class IssueLogs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    codex_session_logs: list[str] = []


class IssueRunningBlock(BaseModel):
    """``running`` block when the issue currently has a worker (§13.7.2)."""

    model_config = ConfigDict(extra="forbid")

    session_id: str | None
    state: str
    started_at: datetime
    turn_count: int
    last_codex_event: str | None
    last_codex_timestamp: datetime | None
    last_codex_message: str | None
    tokens: TokensView


class IssueRetryBlock(BaseModel):
    """``retry`` block when the issue is currently in the retry queue."""

    model_config = ConfigDict(extra="forbid")

    attempt: int
    kind: str
    fire_at: datetime
    last_error: str | None


class RecentEventView(BaseModel):
    """Single event from the bounded ``recent_events`` deque (max 50)."""

    model_config = ConfigDict(extra="forbid")

    event: str
    timestamp: datetime
    codex_app_server_pid: int
    payload: dict[str, Any]
    usage: dict[str, Any] | None


class IssueDetailResponse(BaseModel):
    """Top-level JSON for ``GET /api/v1/{identifier}`` (§13.7.2)."""

    model_config = ConfigDict(extra="forbid")

    issue_id: str
    identifier: str
    title: str
    state: str
    status: str
    attempts: IssueAttempts
    recent_events: list[RecentEventView]
    last_error: str | None
    tracked: dict[str, Any]
    logs: IssueLogs
    running: IssueRunningBlock | None
    retry: IssueRetryBlock | None


# Same vocabulary as observability.snapshot — keep both in sync.
_TURN_START_EVENTS: frozenset[str] = frozenset({"turn_started", "turn.start"})


def lookup_issue_by_identifier(
    state: OrchestratorState,
    retry_queue: RetryQueue,
    identifier: str,
) -> tuple[RunningEntry | None, RetryEntry | None, str | None]:
    """Resolve ``identifier`` to (running, retry, issue_id).

    Lookup order:

    1. Scan ``state.running`` for an entry with matching ``identifier``
       — this is also a hit on the live issue snapshot.
    2. If not running, walk ``state.identifier_index`` (which retains the
       mapping past ``remove_running``) to recover the ``issue_id``,
       then look up the retry queue.

    ``issue_id`` is ``None`` only when the identifier is unknown to both
    the running map and the historical index — caller maps that to 404.
    """
    for entry in state.running.values():
        if entry.identifier == identifier:
            running_id = entry.issue.id
            retry = retry_queue._entries.get(running_id)  # noqa: SLF001 -- single-owner
            return entry, retry, running_id

    historical_id = _issue_id_for_identifier(state, identifier)
    if historical_id is None:
        return None, None, None
    retry = retry_queue._entries.get(historical_id)  # noqa: SLF001 -- single-owner
    return None, retry, historical_id


def _issue_id_for_identifier(
    state: OrchestratorState, identifier: str
) -> str | None:
    for issue_id, ident in state.identifier_index.items():
        if ident == identifier:
            return issue_id
    return None


router = APIRouter(prefix="/api/v1", tags=["issue"])


@router.get("/{identifier}")
async def get_issue_detail(identifier: str, request: Request) -> Any:
    """Return per-issue debug detail or 404 envelope."""
    state: OrchestratorState = request.app.state.state_provider()
    retry_queue: RetryQueue = request.app.state.retry_queue_provider()

    running, retry, issue_id = lookup_issue_by_identifier(
        state, retry_queue, identifier
    )

    if running is not None:
        return IssueDetailResponse(
            issue_id=running.issue.id,
            identifier=running.identifier,
            title=running.issue.title,
            state=running.issue.state,
            status="running",
            attempts=IssueAttempts(
                restart_count=running.restart_count,
                current_retry_attempt=None,
            ),
            recent_events=[
                _serialize_event(evt) for evt in running.recent_events
            ],
            last_error=running.last_error,
            tracked={},
            logs=IssueLogs(),
            running=IssueRunningBlock(
                session_id=running.session_id,
                state=running.issue.state,
                started_at=running.started_at,
                turn_count=_count_turn_starts(running),
                last_codex_event=running.last_codex_event,
                last_codex_timestamp=running.last_codex_timestamp,
                last_codex_message=running.last_codex_message,
                tokens=TokensView(
                    input_tokens=running.last_reported_input_tokens,
                    output_tokens=running.last_reported_output_tokens,
                    total_tokens=running.last_reported_total_tokens,
                ),
            ),
            retry=None,
        )

    if issue_id is None:
        raise _not_found(identifier)

    if retry is not None:
        return IssueDetailResponse(
            issue_id=issue_id,
            identifier=identifier,
            title="",
            state="",
            status="retrying",
            attempts=IssueAttempts(
                restart_count=0,
                current_retry_attempt=retry.attempt,
            ),
            recent_events=[],
            last_error=retry.last_error,
            tracked={},
            logs=IssueLogs(),
            running=None,
            retry=IssueRetryBlock(
                attempt=retry.attempt,
                kind=retry.kind,
                fire_at=retry.fire_at,
                last_error=retry.last_error,
            ),
        )

    if issue_id in state.completed:
        return IssueDetailResponse(
            issue_id=issue_id,
            identifier=identifier,
            title="",
            state="",
            status="completed",
            attempts=IssueAttempts(
                restart_count=0,
                current_retry_attempt=None,
            ),
            recent_events=[],
            last_error=None,
            tracked={},
            logs=IssueLogs(),
            running=None,
            retry=None,
        )

    raise _not_found(identifier)


def _serialize_event(evt: Any) -> RecentEventView:
    return RecentEventView(
        event=evt.event,
        timestamp=evt.timestamp,
        codex_app_server_pid=evt.codex_app_server_pid,
        payload=evt.payload,
        usage=evt.usage,
    )


def _count_turn_starts(entry: RunningEntry) -> int:
    return sum(
        1 for evt in entry.recent_events if evt.event in _TURN_START_EVENTS
    )


def _not_found(identifier: str) -> HTTPException:
    """Build the issue-not-found exception with the §13.7.2 envelope detail.

    The global ``HTTPException`` handler in :mod:`river_gang.http.app`
    detects detail dicts carrying ``code`` + ``message`` and emits them
    verbatim, so this route's specific ``"issue_not_found"`` slug is
    preserved instead of being flattened to the generic ``"not_found"``.
    """
    return HTTPException(
        status_code=404,
        detail={
            "code": "issue_not_found",
            "message": f"identifier={identifier} not tracked",
        },
    )


__all__ = [
    "IssueAttempts",
    "IssueDetailResponse",
    "IssueLogs",
    "IssueRetryBlock",
    "IssueRunningBlock",
    "RecentEventView",
    "TokensView",
    "lookup_issue_by_identifier",
    "router",
]
