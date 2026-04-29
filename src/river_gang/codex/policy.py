"""Trust posture (SPED §10.5, §15.1).

This module documents and enforces ``river_gang``'s mandatory
implementation-defined trust boundary. Per §15.1, every implementation
MUST state its approval and sandbox posture explicitly; this docstring
is that statement.

Policy:
    - ``approval_policy = "never"``  — the agent never blocks for human
      command-execution or file-change approvals. Every approval request
      is auto-approved by :class:`ApprovalHandler`.
    - ``sandbox_policy = "workspace-write"``  — the OS-level Codex sandbox
      is the operative authority that prevents writes outside the
      per-issue workspace. Path-containment checks (``§9.5 invariant 2``)
      are belt-and-braces, not the primary defence.
    - User-input-required events FAIL the run immediately (no
      operator-in-the-loop). Treated as hard failure per §10.5.

Operational consequence:
    The orchestrator NEVER pauses waiting for an external decision. A
    request that cannot be auto-resolved becomes a typed exit (``TurnFailed``,
    ``TurnInputRequired``) so the worker exits cleanly and the retry layer
    can decide what to do next.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from river_gang.codex.protocol import JSONRPC_VERSION

APPROVAL_POLICY_NEVER = "never"
SANDBOX_POLICY_WORKSPACE_WRITE = "workspace-write"

EVENT_APPROVAL_REQUEST = "approval_request"
EVENT_APPROVAL_AUTO_APPROVED = "approval_auto_approved"
EVENT_APPROVAL_DENIED = "approval_denied"

METHOD_APPROVAL_RESPONSE = "approval_response"


@dataclass(frozen=True)
class ApprovalDecision:
    response_frame: dict[str, Any]
    observability_event_method: str


class ApprovalHandler:
    def __init__(self, *, approval_policy: str) -> None:
        if not approval_policy:
            raise ValueError("approval_policy must be a non-empty string")
        self._approval_policy = approval_policy

    @property
    def approval_policy(self) -> str:
        return self._approval_policy

    def build_response(
        self, request_payload: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        approval_id = request_payload.get("approvalId")
        approved = self._approval_policy == APPROVAL_POLICY_NEVER

        response = {
            "jsonrpc": JSONRPC_VERSION,
            "method": METHOD_APPROVAL_RESPONSE,
            "params": {
                "approvalId": approval_id,
                "approved": approved,
            },
        }

        observability_event_method = (
            EVENT_APPROVAL_AUTO_APPROVED
            if approved
            else EVENT_APPROVAL_DENIED
        )
        observability = {
            "event": observability_event_method,
            "timestamp": datetime.now(UTC),
            "payload": dict(request_payload),
        }
        return response, observability


__all__ = [
    "APPROVAL_POLICY_NEVER",
    "EVENT_APPROVAL_AUTO_APPROVED",
    "EVENT_APPROVAL_DENIED",
    "EVENT_APPROVAL_REQUEST",
    "METHOD_APPROVAL_RESPONSE",
    "SANDBOX_POLICY_WORKSPACE_WRITE",
    "ApprovalDecision",
    "ApprovalHandler",
]
