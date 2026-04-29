"""``GET /api/v1/state`` — runtime snapshot endpoint (SPED §13.7.2).

Pydantic view models translate the immutable
:class:`river_gang.observability.snapshot.Snapshot` into the JSON shape
the spec example shows. Naming follows the spec verbatim — fields
diverge from the underlying dataclass only where Pydantic needs an
explicit type hint (e.g. ``rate_limits`` is the ``RateLimitsView``
projection rather than the dataclass).

The route handler reads providers from ``request.app.state`` so the
orchestrator can hot-swap state/retry references without rebuilding
the FastAPI app.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from river_gang.observability.snapshot import build_snapshot
from river_gang.orchestrator.retry import RetryQueue
from river_gang.orchestrator.state import OrchestratorState


class RunningView(BaseModel):
    """One running session row (§13.7.2)."""

    model_config = ConfigDict(extra="forbid")

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


class RetryView(BaseModel):
    """One scheduled retry row (§13.7.2)."""

    model_config = ConfigDict(extra="forbid")

    issue_id: str
    identifier: str | None
    attempt: int
    kind: str
    fire_at: datetime
    last_error: str | None


class CountsView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    running: int
    retrying: int
    completed: int


class CodexTotalsView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int
    output_tokens: int
    total_tokens: int
    seconds_running: float


class RateLimitsView(BaseModel):
    """Latest rate-limit snapshot (§13.5).

    ``reset_at`` is left as a free-form string so we can pass the agent's
    payload through unchanged — Linear surfaces an ISO-8601 string today,
    but other trackers might emit a different format.
    """

    model_config = ConfigDict(extra="forbid")

    limit: int | None
    remaining: int | None
    reset_at: str | None


class StateResponse(BaseModel):
    """Top-level JSON for ``GET /api/v1/state`` (§13.7.2)."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    counts: CountsView
    running: list[RunningView]
    retrying: list[RetryView]
    codex_totals: CodexTotalsView
    rate_limits: RateLimitsView | None


router = APIRouter(prefix="/api/v1", tags=["state"])


@router.get("/state", response_model=StateResponse)
async def get_state(request: Request) -> StateResponse:
    """Return the current runtime snapshot."""
    state: OrchestratorState = request.app.state.state_provider()
    retry_queue: RetryQueue = request.app.state.retry_queue_provider()
    snap = build_snapshot(state, retry_queue=retry_queue, now=datetime.now(UTC))

    rate_limits_view: RateLimitsView | None = None
    if snap.rate_limits is not None:
        rate_limits_view = RateLimitsView(
            limit=snap.rate_limits.limit,
            remaining=snap.rate_limits.remaining,
            reset_at=snap.rate_limits.reset_at,
        )

    return StateResponse(
        generated_at=snap.generated_at,
        counts=CountsView(**snap.counts),
        running=[RunningView(**vars(row)) for row in snap.running],
        retrying=[RetryView(**vars(row)) for row in snap.retrying],
        codex_totals=CodexTotalsView(**snap.codex_totals),
        rate_limits=rate_limits_view,
    )


__all__ = [
    "CodexTotalsView",
    "CountsView",
    "RateLimitsView",
    "RetryView",
    "RunningView",
    "StateResponse",
    "router",
]
