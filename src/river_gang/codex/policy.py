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
    request that cannot be auto-resolved becomes a typed exit (``TurnFailed``)
    so the worker exits cleanly and the retry layer can decide what to do
    next.

Wire format (codex 0.125.0+ ServerRequest dispatch):
    Approvals are JSON-RPC server requests, not notifications. The five
    approval methods carry distinct response schemas — :class:`ApprovalHandler`
    returns the matching ``result`` dict per method, which the dispatcher
    wraps as ``{id, result}``. Two response shapes exist:

    - ``decision`` (string enum): ``applyPatchApproval``,
      ``execCommandApproval``, ``item/commandExecution/requestApproval``,
      ``item/fileChange/requestApproval``.
    - ``permissions`` (object): ``item/permissions/requestApproval`` —
      grants an empty :file:`GrantedPermissionProfile` (since the OS-level
      sandbox is the operative authority, no extra grant is needed).
"""

from __future__ import annotations

from typing import Any

APPROVAL_POLICY_NEVER = "never"
SANDBOX_POLICY_WORKSPACE_WRITE = "workspace-write"

# Approval method names (codex 0.125.0+ ServerRequest schema). Each method
# carries its own *ApprovalParams / *ApprovalResponse pair.
METHOD_APPLY_PATCH_APPROVAL = "applyPatchApproval"
METHOD_EXEC_COMMAND_APPROVAL = "execCommandApproval"
METHOD_ITEM_COMMAND_EXECUTION_REQUEST_APPROVAL = (
    "item/commandExecution/requestApproval"
)
METHOD_ITEM_FILE_CHANGE_REQUEST_APPROVAL = "item/fileChange/requestApproval"
METHOD_ITEM_PERMISSIONS_REQUEST_APPROVAL = "item/permissions/requestApproval"

APPROVAL_METHODS: tuple[str, ...] = (
    METHOD_APPLY_PATCH_APPROVAL,
    METHOD_EXEC_COMMAND_APPROVAL,
    METHOD_ITEM_COMMAND_EXECUTION_REQUEST_APPROVAL,
    METHOD_ITEM_FILE_CHANGE_REQUEST_APPROVAL,
    METHOD_ITEM_PERMISSIONS_REQUEST_APPROVAL,
)

# ReviewDecision (ApplyPatchApprovalResponse / ExecCommandApprovalResponse).
_REVIEW_DECISION_APPROVED = "approved"
_REVIEW_DECISION_DENIED = "denied"

# CommandExecutionApprovalDecision / FileChangeApprovalDecision.
_ITEM_DECISION_ACCEPT = "accept"
_ITEM_DECISION_DECLINE = "decline"


class ApprovalHandler:
    """Builds the per-method approval response payload (the ``result`` body
    of the JSON-RPC reply).

    Under ``approval_policy="never"`` every approval is granted. Any other
    policy denies — defensive default for future policies that may want
    stricter behaviour. Permissions requests under "never" return an empty
    granted profile (the OS-level sandbox is the operative authority).
    """

    def __init__(self, *, approval_policy: str) -> None:
        if not approval_policy:
            raise ValueError("approval_policy must be a non-empty string")
        self._approval_policy = approval_policy

    @property
    def approval_policy(self) -> str:
        return self._approval_policy

    @property
    def approves(self) -> bool:
        return self._approval_policy == APPROVAL_POLICY_NEVER

    def build_result(
        self, method: str, _params: dict[str, Any]
    ) -> dict[str, Any]:
        """Return the JSON-RPC ``result`` body for ``method``.

        ``_params`` is the request's ``params`` dict (unused under the
        current "never" / fall-back-deny policy, but threaded through so
        future policies can branch on it).
        """
        if method in (
            METHOD_APPLY_PATCH_APPROVAL,
            METHOD_EXEC_COMMAND_APPROVAL,
        ):
            decision = (
                _REVIEW_DECISION_APPROVED
                if self.approves
                else _REVIEW_DECISION_DENIED
            )
            return {"decision": decision}

        if method in (
            METHOD_ITEM_COMMAND_EXECUTION_REQUEST_APPROVAL,
            METHOD_ITEM_FILE_CHANGE_REQUEST_APPROVAL,
        ):
            decision = (
                _ITEM_DECISION_ACCEPT
                if self.approves
                else _ITEM_DECISION_DECLINE
            )
            return {"decision": decision}

        if method == METHOD_ITEM_PERMISSIONS_REQUEST_APPROVAL:
            # PermissionsRequestApprovalResponse: ``permissions`` is the only
            # required field. An empty GrantedPermissionProfile is valid and
            # signals "no additional permissions granted" — the OS sandbox
            # already covers the workspace-write surface we expose.
            # ``scope`` defaults to "turn" in the schema; omit explicit value.
            return {"permissions": {}}

        raise ValueError(f"unknown approval method: {method!r}")


__all__ = [
    "APPROVAL_METHODS",
    "APPROVAL_POLICY_NEVER",
    "METHOD_APPLY_PATCH_APPROVAL",
    "METHOD_EXEC_COMMAND_APPROVAL",
    "METHOD_ITEM_COMMAND_EXECUTION_REQUEST_APPROVAL",
    "METHOD_ITEM_FILE_CHANGE_REQUEST_APPROVAL",
    "METHOD_ITEM_PERMISSIONS_REQUEST_APPROVAL",
    "SANDBOX_POLICY_WORKSPACE_WRITE",
    "ApprovalHandler",
]
