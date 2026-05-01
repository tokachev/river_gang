"""High-level Codex app-server client (SPED §10.2-§10.5, §16.5, §17.5).

Drives the SPED §10.2 startup handshake and the §10.3 streaming-turn loop.

Startup (3-step handshake):

1. ``initialize``   — caps + protocol negotiation
2. ``thread.start`` — create a coding-agent thread bound to the workspace
3. ``turn.start``   — first turn carrying the rendered prompt

Streaming (post-startup):

- :meth:`stream_turn` writes a turn.start request (first turn carries the
  rendered prompt; continuation turns carry guidance only per §10.2),
  awaits the sync ack, then consumes notifications until a completion
  signal lands. Completion classification (§10.3, §10.4):

    * ``turn/completed`` w/ ``status=completed``    → :class:`TurnResult.succeeded`
    * ``turn/completed`` w/ ``status=failed``       → :class:`TurnFailed`
                                                      (``params.turn.error.message``)
    * ``turn/completed`` w/ ``status=interrupted``  → :class:`TurnCancelled`
    * ``turn/completed`` w/ ``status=inProgress``   → keep waiting (heartbeat)
    * top-level ``error`` notification              → :class:`TurnFailed`
                                                      (fatal unless
                                                      ``willRetry=True``, in
                                                      which case the stream
                                                      continues)
    * subprocess exit                               → :class:`PortExit`
    * ``turn_timeout_ms`` exceeded                  → :class:`TurnTimeout`

- Unsupported dynamic tool calls (§10.5): the ``item/tool/call`` handler
  returns a :file:`DynamicToolCallResponse` with ``success=false`` and an
  error-text content item, keeping the session streaming.

Each notification is delivered to the caller's ``on_event`` callback
wrapped in :class:`RuntimeEvent`. Sync request/response round-trips are
wrapped in :func:`asyncio.wait_for` against ``read_timeout_ms``;
:class:`TimeoutError` becomes :class:`ResponseTimeout`.

Shutdown (§16.5): :meth:`stop_session` waits for the subprocess to exit
up to ``graceful_timeout_s``, then escalates to ``aclose()`` (terminate →
SIGKILL inside :class:`CodexProcess`). The :file:`ClientNotification`
schema only allows the ``initialized`` notification, so we do NOT write a
``shutdown`` notification — the OS-level termination IS the shutdown
signal. The worker calls this on every exit branch — success, failure,
prompt error, refresh error.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from river_gang.codex.errors import (
    ResponseError,
    ResponseTimeout,
    TurnCancelled,
    TurnFailed,
    TurnTimeout,
)
from river_gang.codex.policy import (
    APPROVAL_METHODS,
    APPROVAL_POLICY_NEVER,
    ApprovalHandler,
)
from river_gang.codex.protocol import (
    JsonRpcResponse,
    build_continuation_turn_request,
    build_initialize_request,
    build_thread_start_request,
    build_turn_start_request,
    compose_session_id,
    extract_thread_id,
    extract_turn_id,
    parse_response,
)
from river_gang.codex.usage import extract_cumulative_tokens
from river_gang.tracker.issue import Issue

logger = logging.getLogger(__name__)


# Notification methods that signal turn completion (§10.4).
#
# Codex 0.125.0+ collapses the legacy turn_completed / turn_failed /
# turn_ended_with_error / turn_cancelled / turn_input_required notifications
# into a single ``turn/completed`` notification carrying ``params.turn.status``
# (one of ``completed`` / ``failed`` / ``interrupted`` / ``inProgress``). The
# failure message lives at ``params.turn.error.message`` when status=failed.
# ``turn_input_required`` is now a server **request** (handled in task 7).
#
# Top-level ``error`` is a separate session-level notification carrying
# ``params.error.message`` (TurnError shape) outside the per-turn lifecycle.
# It is fatal UNLESS ``params.willRetry=True`` — in that case codex intends
# to retry the turn itself and the stream continues (see
# :meth:`_handle_error_notification`).
METHOD_TURN_COMPLETED = "turn/completed"
_EVENT_ERROR = "error"

# Per-turn status values inside ``params.turn.status``.
_TURN_STATUS_COMPLETED = "completed"
_TURN_STATUS_FAILED = "failed"
_TURN_STATUS_INTERRUPTED = "interrupted"
_TURN_STATUS_IN_PROGRESS = "inProgress"

# Codex 0.125.0+ ServerRequest method for client-side tool execution. The
# legacy ``tool_call`` notification + ``tool_call_response`` notification pair
# was promoted to a single request/response round-trip on this method (see
# DynamicToolCallParams / DynamicToolCallResponse schemas).
METHOD_ITEM_TOOL_CALL = "item/tool/call"

# Codex 0.125.0+ ServerRequest method for the EXPERIMENTAL user-input flow.
# The legacy ``turn_input_required`` notification was promoted to a server
# request: codex sends ``{itemId, threadId, turnId, questions: [...]}`` and
# blocks until the client responds with answers. river-gang's documented
# trust posture (policy.py) is "never block on operator input" — so we
# always reply with an empty :file:`ToolRequestUserInputResponse` (``answers:
# {}``) which the schema accepts as "no answers provided" and signals refusal
# to codex without stalling the turn.
_METHOD_ITEM_TOOL_REQUEST_USER_INPUT = "item/tool/requestUserInput"

# Codex 0.125.0+ ServerRequest method emitted when an MCP server attached to
# the codex app-server raises an ``elicitation/create`` request (e.g. a tool
# wants the user to fill out a form before continuing). Per
# :file:`McpServerElicitationRequestResponse.json` the reply must carry
# ``{action: "accept"|"decline"|"cancel"}`` (with optional ``content`` only
# for "accept"). river-gang's never-block-on-input policy maps to ``decline``
# so codex/MCP-server unblocks immediately and the turn either continues
# without operator data or fails on the next event.
_METHOD_MCP_SERVER_ELICITATION_REQUEST = "mcpServer/elicitation/request"

# ClientNotification schema in codex 0.125.0+ defines exactly one notification
# the client may send: ``initialized``. Codex expects it after the ``initialize``
# round-trip succeeds and BEFORE any subsequent calls (thread/start, etc.).
_INITIALIZED_NOTIFICATION_METHOD = "initialized"

# JSON-RPC 2.0 reserved error codes used in the server-request dispatcher
# fallback so codex doesn't hang waiting on a reply.
#   * -32602 ("Invalid params")    → request params is not a JSON object.
#   * -32601 ("method not found")  → unregistered server-request method.
#   * -32603 ("Internal error")    → registered handler raised unexpectedly.
_JSONRPC_INVALID_PARAMS = -32602
_JSONRPC_METHOD_NOT_FOUND = -32601
_JSONRPC_INTERNAL_ERROR = -32603

# Type alias for a server-request handler: takes the params dict and returns
# either a result dict (sent back as ``{id, result}``) or raises to signal an
# error (sent back as ``{id, error}``). Tasks 5-7 register concrete handlers
# (item/tool/call, approval methods, item/tool/requestUserInput).
ServerRequestHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

# Client-side tool advertisement (SPED §10.5).
LINEAR_GRAPHQL_TOOL_NAME = "linear_graphql"
LINEAR_GRAPHQL_TOOL_SPEC: dict[str, Any] = {
    "name": LINEAR_GRAPHQL_TOOL_NAME,
    "description": (
        "Execute a single Linear GraphQL query or mutation against the "
        "configured Linear project. Returns the GraphQL response body "
        "(including any top-level errors). Reuses the orchestrator's "
        "Linear API credentials — the agent does NOT supply auth."
    ),
    "inputSchema": {
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "A single GraphQL operation (query or mutation). "
                    "Documents containing zero or more than one operation "
                    "are rejected."
                ),
            },
            "variables": {
                "type": "object",
                "description": (
                    "GraphQL variables for the operation. Optional; "
                    "defaults to an empty object."
                ),
            },
        },
    },
}


class _ProcessLike(Protocol):
    """Subset of :class:`CodexProcess` the client actually uses."""

    async def read_frame(self) -> dict[str, Any]: ...

    async def write_frame(self, payload: dict[str, Any]) -> None: ...

    async def wait_for_exit(self, timeout: float) -> bool: ...

    async def aclose(self) -> None: ...


class _ToolResultLike(Protocol):
    """Subset of :class:`river_gang.tools.linear_graphql.ToolResult`.

    Avoids a circular import (tools depend on tracker types, the codex
    client is generic). The structural typing matches the dataclass
    exactly — see ``ToolResult`` in :mod:`river_gang.tools.linear_graphql`.
    """

    success: bool
    data: dict[str, Any] | None
    errors: list[dict[str, Any]] | None
    error_message: str | None


class _LinearGraphqlToolLike(Protocol):
    """Subset of :class:`LinearGraphqlTool` the client uses."""

    async def execute(self, raw_input: Any) -> _ToolResultLike: ...


def _serialise_tool_result_text(result: _ToolResultLike) -> str:
    """Render a :class:`ToolResult` into a single ``inputText`` content item
    payload (DynamicToolCallResponse only carries free-form text strings; the
    schema has no structured success/error fields).

    Successful results stringify the data dict; failures prefer the
    ``error_message`` and append any GraphQL ``errors`` list for context.
    """
    if result.success:
        return json.dumps({"data": result.data}, sort_keys=True, default=str)

    payload: dict[str, Any] = {}
    if result.error_message is not None:
        payload["error"] = result.error_message
    if result.errors is not None:
        payload["errors"] = result.errors
    if result.data is not None:
        payload["data"] = result.data
    return json.dumps(payload, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# Result + event dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Session:
    thread_id: str
    first_turn_id: str
    codex_app_server_pid: int
    started_at: datetime

    @property
    def session_id(self) -> str:
        return compose_session_id(self.thread_id, self.first_turn_id)


@dataclass(frozen=True)
class RuntimeEvent:
    """One notification delivered to the orchestrator callback (§10.4).

    ``usage`` is populated by Task 19's token-accounting layer; left ``None``
    here — events flow through unannotated for now.
    """

    event: str
    timestamp: datetime
    codex_app_server_pid: int
    payload: dict[str, Any]
    usage: dict[str, Any] | None = None


@dataclass(frozen=True)
class TurnResult:
    """Outcome of a single :meth:`CodexClient.stream_turn` call.

    Successful turns return one of these; failing turns raise instead so
    callers can use a single try/except to drive the worker state machine.
    Existence of the result implies success — there is no ``failed`` flavour.
    """

    turn_id: str
    completion_event: str
    payload: dict[str, Any]

    @classmethod
    def succeeded(
        cls, *, turn_id: str, payload: dict[str, Any]
    ) -> TurnResult:
        return cls(
            turn_id=turn_id,
            completion_event=METHOD_TURN_COMPLETED,
            payload=payload,
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class CodexClient:
    """Drives the Codex app-server startup handshake + streaming turns.

    The client owns the JSON-RPC request id sequence (1, 2, 3, ...).
    Subsequent tasks reuse this client by extending it; do not instantiate
    one client per request.
    """

    def __init__(
        self,
        *,
        process: _ProcessLike,
        codex_app_server_pid: int,
        approval_handler: ApprovalHandler | None = None,
        linear_graphql_tool: _LinearGraphqlToolLike | None = None,
    ) -> None:
        self._process = process
        self._codex_app_server_pid = codex_app_server_pid
        self._approval_handler = approval_handler or ApprovalHandler(
            approval_policy=APPROVAL_POLICY_NEVER
        )
        self._linear_graphql_tool = linear_graphql_tool
        self._next_id: int = 1
        self._stopped = False
        # Last-seen ``read_timeout_ms`` from start_session / round_trip;
        # reused as the per-call cap for tool dispatch (agent-initiated
        # tool work shouldn't exceed the same wall-clock budget operators
        # already chose for protocol reads).
        self._read_timeout_ms: int | None = None
        # Last-seen ``turn_timeout_ms`` from stream_turn; fallback cap for
        # tool dispatch when ``_read_timeout_ms`` is unset (e.g. stream_turn
        # invoked outside start_session). A hung tool would otherwise block
        # forever.
        self._turn_timeout_ms: int | None = None
        # Server-request dispatch table (codex 0.125.0+ ServerRequest schema):
        # tasks 5-7 register concrete handlers via :meth:`register_server_request`
        # for ``item/tool/call``, the approval methods, and
        # ``item/tool/requestUserInput``. Unknown methods fall back to a
        # JSON-RPC ``-32601`` error response so codex doesn't hang.
        self._server_request_handlers: dict[str, ServerRequestHandler] = {}

        # Task 5: register the ``item/tool/call`` handler unconditionally so
        # both wired (linear_graphql) and unwired clients can produce a
        # structured response — unwired clients still need to reply (with an
        # unknown-tool error) instead of letting codex hang.
        self.register_server_request(
            METHOD_ITEM_TOOL_CALL, self._handle_item_tool_call
        )

        # Task 6: register approval handlers for all five codex 0.125.0+
        # approval ServerRequest methods. They share a single dispatcher
        # that routes through the policy handler — the per-method response
        # shape (decision vs permissions) is encoded in
        # :meth:`ApprovalHandler.build_result`.
        for approval_method in APPROVAL_METHODS:
            self.register_server_request(
                approval_method,
                self._make_approval_handler(approval_method),
            )

        # Task 7: register the ``item/tool/requestUserInput`` handler. Trust
        # posture (policy.py) is never-block-on-input — reply with an empty
        # :file:`ToolRequestUserInputResponse` so codex unblocks without an
        # operator-in-the-loop.
        self.register_server_request(
            _METHOD_ITEM_TOOL_REQUEST_USER_INPUT,
            self._handle_item_tool_request_user_input,
        )

        # MCP-server elicitation: same never-block-on-input policy. Without
        # this handler an attached MCP server stalls the turn waiting for an
        # ``accept|decline|cancel`` reply, codex eventually fails the turn,
        # and the worker burns its retry budget on requests it cannot fulfil.
        self.register_server_request(
            _METHOD_MCP_SERVER_ELICITATION_REQUEST,
            self._handle_mcp_server_elicitation_request,
        )

    def _allocate_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id

    def register_server_request(
        self, method: str, handler: ServerRequestHandler
    ) -> None:
        """Register a server-request handler for ``method`` (codex 0.125.0+
        ServerRequest dispatch).

        Tasks 5-7 use this to plug in handlers for ``item/tool/call``, the
        approval methods (``applyPatchApproval``, ``execCommandApproval``,
        ``item/commandExecution/requestApproval``,
        ``item/fileChange/requestApproval``,
        ``item/permissions/requestApproval``), and
        ``item/tool/requestUserInput``. Unregistered methods are answered
        with JSON-RPC error ``-32601`` (method not found).
        """
        if method in self._server_request_handlers:
            logger.debug(
                "overwriting server-request handler for %r", method
            )
        self._server_request_handlers[method] = handler

    # ------------------------------------------------------------------
    # Startup (Task 17)
    # ------------------------------------------------------------------

    async def start_session(
        self,
        *,
        workspace: Path,
        prompt: str,
        issue: Issue,
        approval_policy: str,
        sandbox_policy: str,
        read_timeout_ms: int,
        tracker_kind: str | None = None,
    ) -> Session:
        """Drive the full ``initialize`` → ``thread.start`` → ``turn.start``
        handshake.

        ``tracker_kind`` is accepted for API compatibility but currently
        unused: the codex 0.125.0+ schema has no public field on
        :file:`InitializeParams.json` / :file:`ThreadStartParams.json` for
        client-side tool advertisement (see :func:`build_initialize_request`).

        Raises:
            ResponseTimeout: any sync read exceeded ``read_timeout_ms``.
            ResponseError: malformed response, id mismatch, JSON-RPC error,
                or the response result lacked the expected ``threadId`` /
                ``turnId`` field.
            PortExit: subprocess exited mid-handshake.
        """
        self._read_timeout_ms = read_timeout_ms
        del tracker_kind  # see docstring — no wire field for tool advertisement

        # SPED §10.5 / docs/trust-posture.md: codex 0.125.0 ships a
        # DynamicToolSpec definition in ThreadStartParams.json but does not
        # reference it from any property — neither InitializeParams nor
        # ThreadStartParams (nor TurnStartParams) accept a tool advertisement
        # field. The ``item/tool/call`` handler is wired locally (we reply
        # to whatever codex sends) but codex won't issue ``item/tool/call``
        # for ``linear_graphql`` unless the tool has been registered via an
        # out-of-band path (e.g. an MCP server). Surface this gap loudly at
        # session start when a linear_graphql tool was wired — silent
        # registration would let operators believe the tool is reachable
        # when it isn't.
        if self._linear_graphql_tool is not None:
            logger.warning(
                "linear_graphql tool wired locally but codex 0.125.0 has no "
                "wire path to advertise client-side tools (DynamicToolSpec "
                "is defined but unreferenced on InitializeParams / "
                "ThreadStartParams / TurnStartParams). The item/tool/call "
                "handler is dormant unless codex is configured externally "
                "(MCP-style registration) to know about this tool."
            )

        await self._round_trip(
            build_initialize_request(id=self._allocate_id()),
            read_timeout_ms=read_timeout_ms,
            step="initialize",
        )

        # ClientNotification (codex 0.125.0+ schema) requires an ``initialized``
        # notification after the ``initialize`` ack and BEFORE any subsequent
        # client → server traffic (thread/start, turn/start, etc.). No params,
        # no id (notification, not request).
        await self._process.write_frame(
            {"method": _INITIALIZED_NOTIFICATION_METHOD}
        )

        thread_response = await self._round_trip(
            build_thread_start_request(
                id=self._allocate_id(),
                cwd=str(workspace),
                approval_policy=approval_policy,
                sandbox_policy=sandbox_policy,
            ),
            read_timeout_ms=read_timeout_ms,
            step="thread.start",
        )
        if thread_response.result is None:
            raise ResponseError(
                "codex thread.start response carried no result body"
            )
        thread_id = extract_thread_id(thread_response.result)

        turn_response = await self._round_trip(
            build_turn_start_request(
                id=self._allocate_id(),
                thread_id=thread_id,
                prompt=prompt,
            ),
            read_timeout_ms=read_timeout_ms,
            step="turn.start",
        )
        if turn_response.result is None:
            raise ResponseError(
                "codex turn.start response carried no result body"
            )
        first_turn_id = extract_turn_id(turn_response.result)

        return Session(
            thread_id=thread_id,
            first_turn_id=first_turn_id,
            codex_app_server_pid=self._codex_app_server_pid,
            started_at=datetime.now(UTC),
        )

    # ------------------------------------------------------------------
    # Streaming (Task 18)
    # ------------------------------------------------------------------

    async def stream_turn(
        self,
        *,
        session: Session,
        prompt: str,
        on_event: Callable[[RuntimeEvent], None],
        turn_timeout_ms: int,
        is_first_turn: bool = False,
    ) -> TurnResult:
        """Drive a single ``turn.start`` round-trip + the event stream.

        Args:
            session: returned by :meth:`start_session`.
            prompt: rendered prompt body for first turns; continuation
                guidance for subsequent turns.
            on_event: callback invoked synchronously for every notification
                (including the completion event itself).
            turn_timeout_ms: wall-clock cap on the entire turn including
                the streaming phase.
            is_first_turn: True for the first turn after startup; False for
                continuation turns. The first-turn request carries the full
                prompt body; continuation turns carry only ``guidance`` per
                §10.2.

        Returns: :class:`TurnResult.succeeded` on completion.

        Raises:
            TurnTimeout: streaming did not complete within ``turn_timeout_ms``.
            TurnFailed: ``turn/completed`` arrived with ``status=failed`` or a
                top-level ``error`` notification fired (session-level fatal).
            TurnCancelled: ``turn/completed`` arrived with ``status=interrupted``.
            PortExit: subprocess exited mid-stream.
        """
        self._turn_timeout_ms = turn_timeout_ms
        try:
            return await asyncio.wait_for(
                self._run_turn(
                    session=session,
                    prompt=prompt,
                    on_event=on_event,
                    is_first_turn=is_first_turn,
                ),
                timeout=turn_timeout_ms / 1000.0,
            )
        except TimeoutError as exc:
            raise TurnTimeout(
                f"turn for session {session.session_id} did not complete within "
                f"{turn_timeout_ms}ms"
            ) from exc

    async def _run_turn(
        self,
        *,
        session: Session,
        prompt: str,
        on_event: Callable[[RuntimeEvent], None],
        is_first_turn: bool,
    ) -> TurnResult:
        request_id = self._allocate_id()
        if is_first_turn:
            request = build_turn_start_request(
                id=request_id,
                thread_id=session.thread_id,
                prompt=prompt,
            )
        else:
            request = build_continuation_turn_request(
                id=request_id,
                thread_id=session.thread_id,
                guidance=prompt,
            )
        await self._process.write_frame(request)

        # First synchronous read is the ack carrying the new turn id.
        # Codex MAY emit unrelated notifications or server requests before
        # the response arrives; tolerate them with the same shape-based
        # dispatch as the streaming loop so we don't choke on interleaved
        # frames (parse_response would raise on a notification).
        ack = await self._await_response(
            expected_id=request_id, on_event=on_event
        )
        if ack.error is not None:
            raise TurnFailed(
                f"turn.start ack returned JSON-RPC error "
                f"code={ack.error.code} message={ack.error.message!r}"
            )
        if ack.result is None:
            raise TurnFailed(
                f"turn.start ack carried neither result nor error for "
                f"request id={request_id}"
            )
        turn_id = extract_turn_id(ack.result)

        # The ack already carries the Turn object's status. Documented
        # values are inProgress (continue) / completed / failed / interrupted
        # — the latter three resolve immediately so we don't sit on the
        # stream waiting for a notification that won't arrive.
        ack_turn = ack.result.get("turn") if isinstance(ack.result, dict) else None
        ack_status = ack_turn.get("status") if isinstance(ack_turn, dict) else None
        if ack_status == _TURN_STATUS_COMPLETED:
            return TurnResult.succeeded(
                turn_id=turn_id, payload=dict(ack.result)
            )
        if ack_status == _TURN_STATUS_FAILED:
            error_obj = (
                ack_turn.get("error") if isinstance(ack_turn, dict) else None
            )
            message = (
                error_obj.get("message")
                if isinstance(error_obj, dict)
                else None
            ) or "turn failed at ack"
            raise TurnFailed(f"turn {turn_id} failed: {message}")
        if ack_status == _TURN_STATUS_INTERRUPTED:
            raise TurnCancelled(f"turn {turn_id} interrupted")

        # Stream frames until a turn-completion signal lands. Codex 0.125.0+
        # multiplexes three frame shapes on the same channel:
        #   * server request      → has both ``id`` and ``method``
        #   * server notification → has ``method`` but no ``id``
        #   * response to us      → has ``id`` and ``result``/``error`` (no ``method``)
        # We discriminate on shape, not method-name allowlists, because codex
        # keeps adding ServerRequest variants and an allowlist would silently
        # let new request methods leak into the notification path.
        while True:
            raw = await self._process.read_frame()
            method = raw.get("method")
            has_method = isinstance(method, str) and method != ""
            has_id = "id" in raw

            if has_method and has_id:
                # Server-initiated request: dispatch + reply with id.
                await self._dispatch_server_request(raw)
                continue

            if not has_method:
                # No method → either a stray response to a prior request or
                # a malformed frame. Log and ignore defensively rather than
                # crash the session.
                #
                # Promote to WARNING when the stray response carries a
                # non-empty error object — that's typically codex rejecting
                # one of OUR replies to a server request (the dispatcher
                # handler returned a body codex didn't accept). Silently
                # dropping such a frame at DEBUG level hides protocol bugs.
                stray_error = raw.get("error")
                if isinstance(stray_error, dict) and stray_error:
                    logger.warning(
                        "ignoring stray response carrying error: id=%s "
                        "error=%s",
                        raw.get("id"),
                        stray_error,
                    )
                else:
                    logger.debug(
                        "ignoring frame with non-string/empty method: keys=%s "
                        "method_type=%s",
                        sorted(raw.keys()),
                        type(raw.get("method")).__name__,
                    )
                continue

            event_name = method  # narrowed: str, non-empty, no id → notification

            params = raw.get("params") or {}
            if not isinstance(params, dict):
                params = {"_raw_params": params}

            payload = dict(params)

            # SPED §13.5: only treat known cumulative payloads as usage —
            # generic ``usage`` maps and ``last_token_usage`` deltas stay None.
            token_snapshot = extract_cumulative_tokens(event_name, payload)
            usage_dict: dict[str, Any] | None = (
                dataclasses.asdict(token_snapshot)
                if token_snapshot is not None
                else None
            )

            event = RuntimeEvent(
                event=event_name,
                timestamp=datetime.now(UTC),
                codex_app_server_pid=self._codex_app_server_pid,
                payload=payload,
                usage=usage_dict,
            )

            on_event(event)

            if event_name == METHOD_TURN_COMPLETED:
                # Codex 0.125.0+ TurnCompletedNotification: branch on
                # ``params.turn.status`` instead of distinct method names.
                turn_obj = payload.get("turn") if isinstance(payload, dict) else None
                if not isinstance(turn_obj, dict):
                    # Malformed turn/completed without the required ``turn``
                    # object — protocol drift; surface as TurnFailed so the
                    # worker can record the exit instead of silently
                    # succeeding on a malformed payload.
                    raise TurnFailed(
                        f"turn {turn_id} turn/completed missing 'turn' object"
                    )
                status = turn_obj.get("status")
                if status == _TURN_STATUS_COMPLETED:
                    return TurnResult.succeeded(turn_id=turn_id, payload=payload)
                if status == _TURN_STATUS_FAILED:
                    error_obj = turn_obj.get("error")
                    message = (
                        error_obj.get("message")
                        if isinstance(error_obj, dict)
                        else None
                    ) or "turn failed"
                    raise TurnFailed(f"turn {turn_id} failed: {message}")
                if status == _TURN_STATUS_INTERRUPTED:
                    raise TurnCancelled(f"turn {turn_id} interrupted")
                if status == _TURN_STATUS_IN_PROGRESS:
                    # Heartbeat / progress beat — keep streaming.
                    continue
                # Unknown status: protocol drift. Raise rather than silently
                # treating as success — completing on unknown statuses would
                # mask schema regressions in production.
                raise TurnFailed(
                    f"turn {turn_id} turn/completed has unknown status "
                    f"{status!r}"
                )

            if event_name == _EVENT_ERROR:
                # Top-level ErrorNotification (codex 0.125.0+). The TurnError
                # shape lives at ``params.error.message``. Schema requires a
                # ``willRetry: bool`` field — when codex intends to retry the
                # turn itself we MUST NOT abort, otherwise we race the retry
                # and surface a spurious failure. Only an absent or False
                # ``willRetry`` is fatal (fail-closed).
                error_obj = payload.get("error") if isinstance(payload, dict) else None
                message = (
                    error_obj.get("message")
                    if isinstance(error_obj, dict)
                    else None
                ) or "session error"
                will_retry = (
                    payload.get("willRetry")
                    if isinstance(payload, dict)
                    else None
                )
                if will_retry is True:
                    # Surface as a notification beat (already delivered to
                    # on_event above) and keep streaming — codex will retry
                    # and emit the next turn/completed itself.
                    logger.info(
                        "codex error notification (willRetry=true) — "
                        "continuing turn %s: %s",
                        turn_id,
                        message,
                    )
                    continue
                raise TurnFailed(f"turn {turn_id} session error: {message}")

    async def _dispatch_server_request(self, raw: dict[str, Any]) -> None:
        """Route a server-initiated request and write the JSON-RPC response.

        Codex 0.125.0+ promotes several flows that used to be notifications
        (tool calls, approvals, user-input) into request/response: the server
        sends ``{id, method, params}`` and blocks waiting for our
        ``{id, result}`` or ``{id, error}`` reply. If we never reply the
        agent stalls until ``turn_timeout_ms`` fires.

        Failure modes:

        - Malformed id (None, not int|str)  → log + drop (no reply we can
          route — writing back a malformed reply just compounds the drift).
        - Non-dict params       → JSON-RPC ``-32602`` (Invalid params). For
          security-sensitive methods (approvals) silently coercing to ``{}``
          would risk auto-approving malformed requests; reject explicitly.
        - Unknown method        → JSON-RPC ``-32601`` (method not found).
        - Handler raised        → JSON-RPC ``-32603`` (Internal error).

        All three reply branches unblock codex immediately so the turn can
        continue.
        """
        request_id = raw.get("id")
        method = raw.get("method")
        params_raw = raw.get("params")

        # JSON-RPC requires id to be a String, Number, or Null. We additionally
        # require non-None — a Null id arrives on notifications, never on
        # requests we can route a reply to. Echoing whatever id type arrived
        # back to codex without validation invites a malformed reply frame.
        if request_id is None or not isinstance(request_id, (int, str)):
            logger.warning(
                "dropping server-request with malformed id: type=%s value=%r "
                "method=%r",
                type(request_id).__name__,
                request_id,
                method,
            )
            return

        # Schema-typed params is always an object on every codex 0.125.0+
        # ServerRequest variant. A non-dict params is wire-level malformed —
        # for approval methods coercing to ``{}`` would mean auto-approving a
        # malformed request, which is the wrong default under any policy.
        if params_raw is not None and not isinstance(params_raw, dict):
            logger.warning(
                "server-request %r has non-dict params (type=%s); "
                "replying -32602",
                method,
                type(params_raw).__name__,
            )
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await self._process.write_frame(
                    {
                        "id": request_id,
                        "error": {
                            "code": _JSONRPC_INVALID_PARAMS,
                            "message": (
                                f"invalid params: expected object, got "
                                f"{type(params_raw).__name__}"
                            ),
                        },
                    }
                )
            return

        params = params_raw if isinstance(params_raw, dict) else {}

        handler = self._server_request_handlers.get(method)  # type: ignore[arg-type]
        if handler is None:
            logger.debug(
                "server-request method %r has no handler; replying -32601",
                method,
            )
            response: dict[str, Any] = {
                "id": request_id,
                "error": {
                    "code": _JSONRPC_METHOD_NOT_FOUND,
                    "message": f"method not found: {method}",
                },
            }
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await self._process.write_frame(response)
            return

        try:
            result = await handler(params)
        except Exception as exc:  # noqa: BLE001 -- protocol boundary
            logger.exception(
                "server-request handler for %r raised; replying with error",
                method,
            )
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await self._process.write_frame(
                    {
                        "id": request_id,
                        "error": {
                            "code": _JSONRPC_INTERNAL_ERROR,
                            "message": f"handler error: {exc!s}",
                        },
                    }
                )
            return

        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await self._process.write_frame(
                {"id": request_id, "result": result}
            )

    async def _handle_item_tool_call(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Server-request handler for ``item/tool/call`` (codex 0.125.0+
        DynamicToolCall round-trip; SPED §10.5).

        ``params`` follows :file:`DynamicToolCallParams.json`:
        ``{tool: str, arguments: any, callId: str, threadId: str, turnId: str,
        namespace: str | null}``.

        Schema-wise ``arguments`` is typed ``true`` (any) — codex
        implementations may serialise it as a JSON-encoded string or as the
        raw object. We parse the string form via :func:`json.loads` and fall
        back to passing the raw string through if parsing fails (the tool
        layer can decide what to do with un-parseable text).

        Always returns a :file:`DynamicToolCallResponse.json` shape:
        ``{success: bool, contentItems: [{type: "inputText", text: str}, ...]}``.

        Failures are encoded inside the response as ``success=false`` rather
        than as JSON-RPC errors — the schema dedicates the response shape to
        carrying tool outcomes, success or not. This covers:

        - Unknown tool names / no wired tool      → ``unsupported_tool: ...``
        - :class:`asyncio.TimeoutError` from the wait_for guard
                                                  → ``tool_timeout``
        - Any other exception from ``execute``    → exception text
          (including a tool-internal :class:`TimeoutError` from e.g. the HTTP
          transport — that's not a wait_for trip and shouldn't be relabelled
          as ``tool_timeout``).

        The tool call is bounded by ``read_timeout_ms`` first; if unset we
        fall back to ``turn_timeout_ms`` so a hung tool cannot block the
        whole turn (or — when stream_turn was invoked outside start_session
        and turn_timeout_ms is also unset — block forever).
        """
        tool_name = params.get("tool")
        arguments = params.get("arguments")
        # DynamicToolCallParams.arguments is schema-typed ``true`` (any).
        # Some codex builds serialise the value as a JSON-encoded string;
        # decode here so the tool layer always sees a structured object when
        # codex meant one. Fall back to the raw string if it isn't JSON.
        if isinstance(arguments, str):
            # leave as raw str on parse failure; tool layer handles non-JSON input
            with contextlib.suppress(json.JSONDecodeError):
                arguments = json.loads(arguments)

        if (
            tool_name != LINEAR_GRAPHQL_TOOL_NAME
            or self._linear_graphql_tool is None
        ):
            return {
                "success": False,
                "contentItems": [
                    {
                        "type": "inputText",
                        "text": f"unsupported_tool: {tool_name!r}",
                    }
                ],
            }

        # Pick the tightest available wall-clock cap. Without one, a
        # misbehaving tool would block the streaming loop indefinitely —
        # neither read_timeout_ms (protocol reads) nor turn_timeout_ms
        # (stream_turn-level wait_for) provides per-call coverage by itself.
        timeout_ms = self._read_timeout_ms or self._turn_timeout_ms

        # Note: in Python 3.11+ ``asyncio.TimeoutError`` aliases the builtin
        # :class:`TimeoutError`, so a tool-internal :class:`TimeoutError`
        # (e.g. an HTTP transport timeout) is type-indistinguishable from a
        # :func:`asyncio.wait_for` trip when caught by exception type alone.
        # We disambiguate using :func:`asyncio.timeout` (3.11+ context
        # manager): its ``cm.expired()`` flag is the authoritative signal
        # of whether the deadline (not the body) raised.
        cm: Any = None
        try:
            if timeout_ms is not None:
                cm = asyncio.timeout(timeout_ms / 1000.0)
                async with cm:
                    result = await self._linear_graphql_tool.execute(
                        arguments
                    )
            else:
                result = await self._linear_graphql_tool.execute(arguments)
        except TimeoutError as exc:
            # Either our deadline tripped or the tool raised TimeoutError
            # itself. ``cm.expired()`` distinguishes — True ⇒ our deadline,
            # False ⇒ tool-internal (e.g. HTTP transport timeout).
            if cm is not None and cm.expired():
                return {
                    "success": False,
                    "contentItems": [
                        {"type": "inputText", "text": "tool_timeout"}
                    ],
                }
            logger.exception(
                "linear_graphql_tool.execute raised TimeoutError "
                "(not from our deadline); encoding as success=false"
            )
            return {
                "success": False,
                "contentItems": [
                    {"type": "inputText", "text": f"tool_error: {exc!s}"}
                ],
            }
        except Exception as exc:  # noqa: BLE001 -- protocol boundary
            logger.exception(
                "linear_graphql_tool.execute raised; encoding as success=false"
            )
            return {
                "success": False,
                "contentItems": [
                    {"type": "inputText", "text": f"tool_error: {exc!s}"}
                ],
            }

        text = _serialise_tool_result_text(result)
        return {
            "success": result.success,
            "contentItems": [{"type": "inputText", "text": text}],
        }

    async def _handle_item_tool_request_user_input(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Server-request handler for ``item/tool/requestUserInput`` (codex
        0.125.0+ EXPERIMENTAL ToolRequestUserInput round-trip; SPED §10.5).

        ``params`` follows :file:`ToolRequestUserInputParams.json`:
        ``{itemId, threadId, turnId, questions: [{id, header, question, ...}]}``.

        Returns a :file:`ToolRequestUserInputResponse.json` shape:
        ``{answers: {<questionId>: {answers: [<string>, ...]}}}``.

        Per :mod:`river_gang.codex.policy`, river-gang never blocks on
        operator input — we always reply with an empty ``answers`` map
        (the schema accepts it as "no answers were provided"). This unblocks
        codex without surfacing the request to a human, leaving the agent
        to proceed without the requested input or surface its own failure
        on the next turn event.
        """
        # ``params`` itself is unused — the response shape is fixed under
        # the never-block-on-input policy. Logged at debug for diagnostics
        # without leaking question text into INFO/WARN logs (questions can
        # contain prompts the agent sent verbatim).
        logger.debug(
            "request_user_input refused per policy: itemId=%r questions=%d",
            params.get("itemId"),
            len(params.get("questions") or []),
        )
        return {"answers": {}}

    async def _handle_mcp_server_elicitation_request(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Server-request handler for ``mcpServer/elicitation/request`` (codex
        0.125.0+ MCP-server elicitation round-trip).

        ``params`` follows :file:`McpServerElicitationRequestParams.json`:
        either form-mode ``{serverName, threadId, turnId?, message, mode:
        "form", requestedSchema}`` or url-mode ``{serverName, threadId,
        turnId?, elicitationId, message, mode: "url", url}``.

        Returns a :file:`McpServerElicitationRequestResponse.json` shape
        ``{action: "decline"}`` per the never-block-on-input policy. Form
        data could only be supplied by a human, and ``decline`` lets the
        MCP server fall back to its no-data path (or fail the operation,
        which surfaces on the next turn event) without stalling codex.
        """
        logger.debug(
            "mcpServer/elicitation/request declined per policy: "
            "serverName=%r mode=%r",
            params.get("serverName"),
            params.get("mode"),
        )
        return {"action": "decline"}

    def _make_approval_handler(
        self, method: str
    ) -> ServerRequestHandler:
        """Build a :data:`ServerRequestHandler` closure for an approval
        ``method`` (codex 0.125.0+ approval ServerRequest dispatch; SPED
        §10.5). The closure routes ``params`` through
        :meth:`ApprovalHandler.build_result` and returns the JSON-RPC
        ``result`` body — the dispatcher wraps it as ``{id, result}``.
        """

        async def handler(params: dict[str, Any]) -> dict[str, Any]:
            return self._approval_handler.build_result(method, params)

        return handler

    # ------------------------------------------------------------------
    # Shutdown (Task 18)
    # ------------------------------------------------------------------

    async def stop_session(
        self,
        session: Session,
        *,
        graceful_timeout_s: float = 5.0,
    ) -> None:
        """Wait for graceful subprocess exit, then escalate to terminate/kill.

        The codex 0.125.0+ :file:`ClientNotification` schema only allows the
        ``initialized`` notification — there is no ``shutdown`` notification
        the client may send. The OS-level termination IS the shutdown
        signal: we wait up to ``graceful_timeout_s`` for the process to
        exit on its own (e.g. after replying to its last server request),
        then escalate via :meth:`aclose` (SIGTERM → SIGKILL).

        Idempotent: subsequent calls are no-ops. All best-effort —
        :meth:`stop_session` MUST NEVER raise so the worker's exit branches
        can call it unconditionally.
        """
        del session  # process termination is identity-agnostic
        if self._stopped:
            return
        self._stopped = True

        # Wait for the subprocess to exit on its own; if it doesn't, the
        # underlying ``aclose`` will terminate then SIGKILL.
        with contextlib.suppress(Exception):
            await self._process.wait_for_exit(graceful_timeout_s)

        with contextlib.suppress(Exception):
            await self._process.aclose()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _round_trip(
        self,
        request: dict[str, Any],
        *,
        read_timeout_ms: int,
        step: str,
    ) -> JsonRpcResponse:
        """Send a request and await the matching response under timeout.

        Codex MAY interleave notifications or server requests on the same
        channel before our response lands. The naive read-one-frame approach
        chokes on those (``parse_response`` raises on a notification). We
        apply the same shape-based dispatch as the streaming loop:
        notifications are dropped (handshake-time on_event isn't wired),
        server requests are answered via :meth:`_dispatch_server_request`,
        and only an ``id``-matching response is returned.

        A JSON-RPC error response is mapped to :class:`ResponseError` here
        (not in :meth:`_await_response`) so the streaming-turn caller in
        :meth:`_run_turn` can map ack errors to :class:`TurnFailed` —
        worker.py only catches :class:`TurnFailed`/:class:`TurnCancelled`/
        :class:`TurnTimeout`/:class:`PortExit` on the streaming path, so a
        :class:`ResponseError` leaking out of stream_turn breaks the
        typed-exit contract.
        """
        await self._process.write_frame(request)
        try:
            response = await asyncio.wait_for(
                self._await_response(expected_id=request["id"], on_event=None),
                timeout=read_timeout_ms / 1000.0,
            )
        except TimeoutError as exc:
            raise ResponseTimeout(
                f"codex {step} did not respond within {read_timeout_ms}ms"
            ) from exc
        except ResponseError as exc:
            raise ResponseError(f"codex {step}: {exc!s}") from exc
        if response.error is not None:
            raise ResponseError(
                f"codex {step}: JSON-RPC error code={response.error.code} "
                f"message={response.error.message!r}"
            )
        return response

    async def _await_response(
        self,
        *,
        expected_id: int,
        on_event: Callable[[RuntimeEvent], None] | None,
    ) -> JsonRpcResponse:
        """Read frames until the response matching ``expected_id`` arrives.

        Tolerates interleaved server notifications and server requests:
        notifications are forwarded to ``on_event`` if supplied (else
        dropped; handshake reads pass ``None``), server requests are
        dispatched via :meth:`_dispatch_server_request`. A response with
        a different id is treated as malformed and raises :class:`ResponseError`
        via :func:`parse_response` — late/stray responses to retired requests
        are not silently skipped because doing so would mask correlation bugs
        (e.g. an out-of-order codex emission could leave us waiting on a
        response that already arrived).

        A response carrying a JSON-RPC ``error`` body is returned as-is
        (``response.error`` populated, ``response.result`` ``None``). Callers
        decide how to map it: :meth:`_round_trip` raises :class:`ResponseError`
        (handshake), :meth:`_run_turn` raises :class:`TurnFailed` (streaming
        ack — required for worker.py's typed-exit contract).
        """
        while True:
            raw = await self._process.read_frame()
            method = raw.get("method")
            has_method = isinstance(method, str) and method != ""
            has_id = "id" in raw

            if has_method and has_id:
                # Server-initiated request: dispatch + reply, keep waiting.
                await self._dispatch_server_request(raw)
                continue

            if has_method:
                # Notification: forward to on_event if wired, else drop.
                if on_event is not None:
                    params = raw.get("params") or {}
                    if not isinstance(params, dict):
                        params = {"_raw_params": params}
                    payload = dict(params)
                    on_event(
                        RuntimeEvent(
                            event=method,  # type: ignore[arg-type]
                            timestamp=datetime.now(UTC),
                            codex_app_server_pid=self._codex_app_server_pid,
                            payload=payload,
                        )
                    )
                else:
                    logger.debug(
                        "ignoring notification %r while awaiting response id=%s",
                        method,
                        expected_id,
                    )
                continue

            # No method: must be a response. parse_response raises on
            # id mismatch, missing id, or both/neither result+error.
            # A populated ``error`` body is returned as-is — callers map it
            # to the right exception type (ResponseError for handshake,
            # TurnFailed for the streaming turn.start ack).
            return parse_response(raw, expected_id=expected_id)


__all__ = [
    "CodexClient",
    "RuntimeEvent",
    "Session",
    "TurnResult",
]
