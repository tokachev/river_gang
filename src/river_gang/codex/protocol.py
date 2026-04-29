"""Codex app-server JSON-RPC 2.0 message helpers (SPED §10.2, §17.5).

The Codex app-server protocol is intentionally pinned to a JSON-RPC 2.0
wire format here: id-correlated request/response objects on stdout/stdin,
notifications (no ``id``) for streamed turn events. Spec §10.2 deliberately
defers to "the targeted Codex app-server protocol"; we pick JSON-RPC 2.0
because it matches the Codex app-server reference implementation and gives
us cheap request/response correlation.

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

JSONRPC_VERSION = "2.0"

CLIENT_NAME = "river-gang"

METHOD_INITIALIZE = "initialize"
METHOD_THREAD_START = "thread.start"
METHOD_TURN_START = "turn.start"


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


def build_initialize_request(
    *,
    id: int,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build an ``initialize`` request.

    When ``tools`` is supplied, it's included as ``params.tools`` so the
    Codex app-server knows which client-side tools the orchestrator will
    handle (SPED §10.5 client-side tool advertisement).
    """
    params: dict[str, Any] = {
        "clientInfo": {
            "name": CLIENT_NAME,
            "version": _resolve_client_version(),
        },
    }
    if tools is not None:
        params["tools"] = tools
    return {
        "jsonrpc": JSONRPC_VERSION,
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
    title: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "cwd": cwd,
        "approvalPolicy": approval_policy,
        "sandboxPolicy": sandbox_policy,
    }
    if title is not None:
        params["title"] = title
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": id,
        "method": METHOD_THREAD_START,
        "params": params,
    }


def build_turn_start_request(
    *,
    id: int,
    thread_id: str,
    prompt: str,
    title: str | None = None,
) -> dict[str, Any]:
    """First-turn request — carries the rendered issue prompt body."""
    params: dict[str, Any] = {
        "threadId": thread_id,
        "prompt": prompt,
    }
    if title is not None:
        params["title"] = title
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": id,
        "method": METHOD_TURN_START,
        "params": params,
    }


def build_continuation_turn_request(
    *,
    id: int,
    thread_id: str,
    guidance: str,
    title: str | None = None,
) -> dict[str, Any]:
    """Continuation-turn request — carries only continuation guidance.

    SPED §10.2: continuation turns MUST NOT resend the original issue prompt
    that's already in thread history.
    """
    params: dict[str, Any] = {
        "threadId": thread_id,
        "guidance": guidance,
    }
    if title is not None:
        params["title"] = title
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": id,
        "method": METHOD_TURN_START,
        "params": params,
    }


# ---------------------------------------------------------------------------
# Response parsing + identity extraction
# ---------------------------------------------------------------------------


def parse_response(raw: dict[str, Any], *, expected_id: int) -> JsonRpcResponse:
    """Strict-shape JSON-RPC 2.0 response decoder.

    Raises:
        ResponseError: malformed envelope, version mismatch, id mismatch,
            both ``result`` and ``error`` set, or neither set.
    """
    version = raw.get("jsonrpc")
    if version != JSONRPC_VERSION:
        raise ResponseError(
            f"unexpected jsonrpc version: {version!r}, want {JSONRPC_VERSION!r}"
        )

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
    """Pull ``threadId`` from a ``thread.start`` result. Raises on missing."""
    value = result.get("threadId")
    if not isinstance(value, str) or value == "":
        raise ResponseError(
            "thread.start result missing string 'threadId'"
        )
    return value


def extract_turn_id(result: dict[str, Any]) -> str:
    """Pull ``turnId`` from a ``turn.start`` result. Raises on missing."""
    value = result.get("turnId")
    if not isinstance(value, str) or value == "":
        raise ResponseError("turn.start result missing string 'turnId'")
    return value


def compose_session_id(thread_id: str, turn_id: str) -> str:
    """SPED §10.2: ``session_id = "<thread_id>-<turn_id>"``."""
    return f"{thread_id}-{turn_id}"
