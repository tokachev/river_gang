"""``POST /api/v1/refresh`` — manual poll-and-reconcile trigger (SPED §13.7.2).

Posts a :class:`PollTick` onto the orchestrator's mailbox so the next
dispatcher iteration runs reconciliation + tracker fetch out of cycle.
The request body is intentionally ignored — the spec marks it as
"empty body or ``{}``" and we treat any payload as a no-op trigger
rather than rejecting it (more forgiving for naive ``curl`` callers).

Per the plan, ``coalesced`` is always ``false``: the orchestrator's
mailbox is unbounded and processes ticks in order, so back-to-back
``/refresh`` calls each produce a real :class:`PollTick`. A future
implementation could dedupe by tracking a ``last_refresh_at`` window —
when that lands, ``coalesced=true`` will surface for the suppressed
calls. Until then we never lie to the caller about coalescing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from river_gang.orchestrator.mailbox import PollTick


class RefreshResponse(BaseModel):
    """Body of ``POST /api/v1/refresh`` (§13.7.2 example)."""

    model_config = ConfigDict(extra="forbid")

    queued: bool
    coalesced: bool
    requested_at: datetime
    operations: list[str]


router = APIRouter(prefix="/api/v1", tags=["refresh"])


@router.post("/refresh", status_code=202, response_model=RefreshResponse)
async def post_refresh(request: Request) -> Any:
    """Enqueue a :class:`PollTick` and acknowledge with ``202 Accepted``."""
    mailbox = request.app.state.mailbox_provider()
    await mailbox.send(PollTick())

    body = RefreshResponse(
        queued=True,
        coalesced=False,
        requested_at=datetime.now(UTC),
        operations=["poll", "reconcile"],
    )
    return JSONResponse(status_code=202, content=body.model_dump(mode="json"))


@router.get("/refresh")
async def get_refresh_method_not_allowed() -> None:
    """Explicit ``405`` for ``GET /api/v1/refresh``.

    FastAPI's automatic ``405`` synthesis only kicks in for paths that
    don't have a registered same-prefix catch-all. The issue-detail
    route (``/api/v1/{identifier}``) is registered AFTER this one and
    would otherwise match ``GET /api/v1/refresh`` with
    ``identifier="refresh"`` (returning a 404 envelope). Raising
    ``HTTPException(405)`` lets the global error-envelope handler in
    :mod:`river_gang.http.app` shape the response uniformly with the
    other 405s in the API.
    """
    raise HTTPException(status_code=405, headers={"Allow": "POST"})


__all__ = [
    "RefreshResponse",
    "router",
]
