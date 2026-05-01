"""Codex app-server message helpers (SPED §10.2, §17.5).

Wire format follows the targeted Codex app-server schema: id-correlated
request/response objects on stdout/stdin, notifications (no ``id``) for
streamed turn events. SPED §10.2 explicitly defers to "the targeted Codex
app-server protocol"; current codex (0.125.0+) ships its schema via
``codex app-server generate-json-schema`` and does NOT carry a JSON-RPC
``"jsonrpc": "2.0"`` envelope field — only ``id`` / ``method`` / ``result``
/ ``error`` / ``params``. Earlier versions did carry it; we follow the
current schema.

Message construction is kept dict-based on purpose — the wire bytes go
straight through :func:`json.dumps`, and the framing layer (Task 16) speaks
the same dict shape. No intermediate dataclass-to-dict conversion.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any

from river_gang.codex.errors import ResponseError

CLIENT_NAME = "river-gang"

METHOD_INITIALIZE = "initialize"
METHOD_THREAD_START = "thread/start"
METHOD_TURN_START = "turn/start"


def _resolve_client_version() -> str:
    """Look up the installed package version; fall back to ``0+unknown``.

    Falling back to a fixed sentinel keeps a development checkout (where the
    package may not be installed) from breaking startup.
    """
    try:
        return _pkg_version("river_gang")
    except PackageNotFoundError:
        return "0+unknown"


@dataclass(frozen=True)
class JsonRpcError:
    code: int
    message: str
    data: Any | None = None


@dataclass(frozen=True)
class JsonRpcResponse:
    id: int
    result: dict[str, Any] | None
    error: JsonRpcError | None


# ---------------------------------------------------------------------------
# Request builders
# ---------------------------------------------------------------------------


def build_initialize_request(*, id: int) -> dict[str, Any]:
    """Build an ``initialize`` request.

    NOTE (codex 0.125.0+ schema gap): :file:`InitializeParams.json` and
    :file:`ThreadStartParams.json` define no public field for advertising
    client-side tools — :file:`DynamicToolSpec` is defined but unreferenced
    on either request. River-gang implements the agent-side of
    ``item/tool/call`` (SPED §10.5) but cannot advertise its
    ``linear_graphql`` tool through the wire. Codex must be configured with
    knowledge of the tool externally (e.g. via MCP server registration) for
    it to send ``item/tool/call`` requests. TODO: revisit when codex exposes
    a tool-advertisement field.
    """
    params: dict[str, Any] = {
        "clientInfo": {
            "name": CLIENT_NAME,
            "version": _resolve_client_version(),
        },
    }
    return {
        "id": id,
        "method": METHOD_INITIALIZE,
        "params": params,
    }


def build_thread_start_request(
    *,
    id: int,
    cwd: str,
    approval_policy: str,
    sandbox_policy: str,
) -> dict[str, Any]:
    """Build a ``thread/start`` request.

    The ``sandbox_policy`` keyword is rendered as JSON field ``sandbox`` —
    Codex 0.125.0+ ThreadStartParams uses ``sandbox`` for the SandboxMode
    enum (read-only / workspace-write / danger-full-access). The legacy
    ``sandboxPolicy`` field on this request was renamed.
    """
    params: dict[str, Any] = {
        "cwd": cwd,
        "approvalPolicy": approval_policy,
        "sandbox": sandbox_policy,
    }
    return {
        "id": id,
        "method": METHOD_THREAD_START,
        "params": params,
    }


def build_turn_start_request(
    *,
    id: int,
    thread_id: str,
    prompt: str,
) -> dict[str, Any]:
    """First-turn request — carries the rendered issue prompt body.

    Codex 0.125.0+ TurnStartParams takes ``input`` as an array of ``UserInput``
    objects (each ``{"type": "text", "text": ...}``) instead of the legacy
    ``prompt: string`` field.
    """
    params: dict[str, Any] = {
        "threadId": thread_id,
        "input": [{"type": "text", "text": prompt}],
    }
    return {
        "id": id,
        "method": METHOD_TURN_START,
        "params": params,
    }


def build_continuation_turn_request(
    *,
    id: int,
    thread_id: str,
    guidance: str,
) -> dict[str, Any]:
    """Continuation-turn request — carries only continuation guidance.

    SPED §10.2: continuation turns MUST NOT resend the original issue prompt
    that's already in thread history. Like the first turn, the continuation
    body travels in the ``input`` array as a text UserInput — wire-shape is
    identical to :func:`build_turn_start_request`; this wrapper exists for
    call-site documentation only.
    """
    return build_turn_start_request(id=id, thread_id=thread_id, prompt=guidance)


# ---------------------------------------------------------------------------
# Response parsing + identity extraction
# ---------------------------------------------------------------------------


def parse_response(raw: dict[str, Any], *, expected_id: int) -> JsonRpcResponse:
    """Strict-shape response decoder for the targeted Codex app-server.

    The current Codex schema (codex 0.125.0+) does not carry a JSON-RPC
    ``"jsonrpc": "2.0"`` envelope field — only ``id`` plus exactly one of
    ``result`` / ``error``. We tolerate the field if a future Codex sends
    it again, but never require it.

    Raises:
        ResponseError: malformed envelope, id mismatch, both ``result`` and
            ``error`` set, or neither set.
    """
    if "id" not in raw:
        raise ResponseError("response missing 'id' field (notification?)")
    response_id = raw["id"]
    if response_id != expected_id:
        raise ResponseError(
            f"response id mismatch: got {response_id!r}, expected {expected_id!r}"
        )

    has_result = "result" in raw
    has_error = "error" in raw and raw["error"] is not None
    if has_result and has_error:
        raise ResponseError("response carries both 'result' and 'error'")
    if not has_result and not has_error:
        raise ResponseError("response carries neither 'result' nor 'error'")

    if has_error:
        err = raw["error"]
        if not isinstance(err, dict):
            raise ResponseError(
                f"'error' must be an object, got {type(err).__name__}"
            )
        return JsonRpcResponse(
            id=response_id,
            result=None,
            error=JsonRpcError(
                code=int(err.get("code", 0)),
                message=str(err.get("message", "")),
                data=err.get("data"),
            ),
        )

    result = raw["result"]
    if not isinstance(result, dict):
        raise ResponseError(
            f"'result' must be an object, got {type(result).__name__}"
        )
    return JsonRpcResponse(id=response_id, result=result, error=None)


def extract_thread_id(result: dict[str, Any]) -> str:
    """Pull ``thread.id`` from a ``thread/start`` result. Raises on missing.

    Codex 0.125.0+ ThreadStartResponse wraps identity in a ``thread`` object
    (``{"thread": {"id": ..., ...}}``) instead of the legacy top-level
    ``threadId`` field.
    """
    thread = result.get("thread")
    if not isinstance(thread, dict):
        raise ResponseError(
            "thread/start result missing object 'thread'"
        )
    value = thread.get("id")
    if not isinstance(value, str) or value == "":
        raise ResponseError(
            "thread/start result missing string 'thread.id'"
        )
    return value


def extract_turn_id(result: dict[str, Any]) -> str:
    """Pull ``turn.id`` from a ``turn/start`` result. Raises on missing.

    Codex 0.125.0+ TurnStartResponse wraps identity in a ``turn`` object
    (``{"turn": {"id": ..., ...}}``) instead of the legacy top-level
    ``turnId`` field.
    """
    turn = result.get("turn")
    if not isinstance(turn, dict):
        raise ResponseError("turn/start result missing object 'turn'")
    value = turn.get("id")
    if not isinstance(value, str) or value == "":
        raise ResponseError("turn/start result missing string 'turn.id'")
    return value


def compose_session_id(thread_id: str, turn_id: str) -> str:
    """SPED §10.2: ``session_id = "<thread_id>-<turn_id>"``."""
    return f"{thread_id}-{turn_id}"
