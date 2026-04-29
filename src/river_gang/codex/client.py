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

    * ``turn_completed``                         → :class:`TurnResult.succeeded`
    * ``turn_failed`` / ``turn_ended_with_error``→ :class:`TurnFailed`
    * ``turn_cancelled``                          → :class:`TurnCancelled`
    * ``turn_input_required``                     → :class:`TurnInputRequired`
                                                    (high-trust posture, §10.5)
    * subprocess exit                             → :class:`PortExit`
    * ``turn_timeout_ms`` exceeded                → :class:`TurnTimeout`

- Unsupported dynamic tool calls (§10.5): the client writes a
  ``tool_call_response`` failure frame and keeps streaming. Supported tool
  names live in ``supported_tools``; Task 24 wires ``linear_graphql`` here.

Each notification is delivered to the caller's ``on_event`` callback
wrapped in :class:`RuntimeEvent`. Sync request/response round-trips are
wrapped in :func:`asyncio.wait_for` against ``read_timeout_ms``;
:class:`TimeoutError` becomes :class:`ResponseTimeout`.

Shutdown (§16.5): :meth:`stop_session` writes a ``shutdown`` notification,
awaits subprocess exit up to ``graceful_timeout_s``, then escalates to
``aclose()`` (terminate → SIGKILL inside :class:`CodexProcess`). The worker
calls this on every exit branch — success, failure, prompt error, refresh
error.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from river_gang.codex.errors import (
    ResponseError,
    ResponseTimeout,
    TurnCancelled,
    TurnFailed,
    TurnInputRequired,
    TurnTimeout,
)
from river_gang.codex.policy import (
    APPROVAL_POLICY_NEVER,
    EVENT_APPROVAL_REQUEST,
    ApprovalHandler,
)
from river_gang.codex.protocol import (
    JSONRPC_VERSION,
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
_EVENT_TURN_COMPLETED = "turn_completed"
_EVENT_TURN_FAILED = "turn_failed"
_EVENT_TURN_ENDED_WITH_ERROR = "turn_ended_with_error"
_EVENT_TURN_CANCELLED = "turn_cancelled"
_EVENT_TURN_INPUT_REQUIRED = "turn_input_required"
_EVENT_TOOL_CALL = "tool_call"
_EVENT_UNSUPPORTED_TOOL_CALL = "unsupported_tool_call"
_EVENT_TOOL_CALL_COMPLETED = "tool_call_completed"

_SHUTDOWN_METHOD = "shutdown"
_TOOL_CALL_RESPONSE_METHOD = "tool_call_response"

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


# ---------------------------------------------------------------------------
# Result + event dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Session:
    thread_id: str
    first_turn_id: str
    codex_app_server_pid: int
    started_at: datetime
    session_id: str = field(init=False)

    def __post_init__(self) -> None:
        # ``frozen=True`` blocks normal assignment; ``object.__setattr__`` is
        # the documented escape hatch for derived fields.
        object.__setattr__(
            self, "session_id", compose_session_id(self.thread_id, self.first_turn_id)
        )


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
            completion_event=_EVENT_TURN_COMPLETED,
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
        supported_tools: set[str] | None = None,
        approval_handler: ApprovalHandler | None = None,
        linear_graphql_tool: _LinearGraphqlToolLike | None = None,
    ) -> None:
        self._process = process
        self._codex_app_server_pid = codex_app_server_pid
        self._supported_tools = set(supported_tools) if supported_tools else set()
        self._approval_handler = approval_handler or ApprovalHandler(
            approval_policy=APPROVAL_POLICY_NEVER
        )
        self._linear_graphql_tool = linear_graphql_tool
        if self._linear_graphql_tool is not None:
            self._supported_tools.add(LINEAR_GRAPHQL_TOOL_NAME)
        self._next_id: int = 1
        self._stopped = False
        # Last-seen ``read_timeout_ms`` from start_session / round_trip;
        # reused as the per-call cap for tool dispatch (agent-initiated
        # tool work shouldn't exceed the same wall-clock budget operators
        # already chose for protocol reads).
        self._read_timeout_ms: int | None = None

    def _allocate_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id

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

        ``tracker_kind`` controls client-side tool advertisement: when
        ``"linear"`` and a :class:`LinearGraphqlTool` was wired into the
        constructor, the ``initialize`` request advertises ``linear_graphql``
        in ``params.tools`` per SPED §10.5.

        Raises:
            ResponseTimeout: any sync read exceeded ``read_timeout_ms``.
            ResponseError: malformed response, id mismatch, JSON-RPC error,
                or the response result lacked the expected ``threadId`` /
                ``turnId`` field.
            PortExit: subprocess exited mid-handshake.
        """
        title = f"{issue.identifier}: {issue.title}"
        self._read_timeout_ms = read_timeout_ms

        tools_to_advertise: list[dict[str, Any]] | None = None
        if (
            tracker_kind == "linear"
            and self._linear_graphql_tool is not None
        ):
            tools_to_advertise = [LINEAR_GRAPHQL_TOOL_SPEC]

        await self._round_trip(
            build_initialize_request(
                id=self._allocate_id(), tools=tools_to_advertise
            ),
            read_timeout_ms=read_timeout_ms,
            step="initialize",
        )

        thread_response = await self._round_trip(
            build_thread_start_request(
                id=self._allocate_id(),
                cwd=str(workspace),
                approval_policy=approval_policy,
                sandbox_policy=sandbox_policy,
                title=title,
            ),
            read_timeout_ms=read_timeout_ms,
            step="thread.start",
        )
        assert thread_response.result is not None  # noqa: S101 -- enforced by parser
        thread_id = extract_thread_id(thread_response.result)

        turn_response = await self._round_trip(
            build_turn_start_request(
                id=self._allocate_id(),
                thread_id=thread_id,
                prompt=prompt,
                title=title,
            ),
            read_timeout_ms=read_timeout_ms,
            step="turn.start",
        )
        assert turn_response.result is not None  # noqa: S101 -- enforced by parser
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
            TurnFailed: protocol emitted ``turn_failed`` or
                ``turn_ended_with_error``.
            TurnCancelled: protocol emitted ``turn_cancelled``.
            TurnInputRequired: protocol emitted ``turn_input_required``.
                High-trust policy treats this as hard failure (§10.5).
            PortExit: subprocess exited mid-stream.
        """
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

        # First read is the synchronous ack carrying the new turnId.
        ack_raw = await self._process.read_frame()
        ack = parse_response(ack_raw, expected_id=request_id)
        if ack.error is not None:
            raise TurnFailed(
                f"turn.start ack returned JSON-RPC error "
                f"code={ack.error.code} message={ack.error.message!r}"
            )
        assert ack.result is not None  # noqa: S101 -- enforced by parser
        turn_id = extract_turn_id(ack.result)

        # Stream notifications until a completion signal lands.
        while True:
            raw = await self._process.read_frame()
            event_name = raw.get("method")
            if not isinstance(event_name, str) or event_name == "":
                # Not a notification (probably an unexpected response with
                # an id). Ignore unknown frames defensively rather than
                # crash the session, but DEBUG-log the shape so protocol
                # drift (server emitting non-string ``method``, missing
                # ``method`` key, etc.) is observable.
                logger.debug(
                    "ignoring frame with non-string/empty method: keys=%s "
                    "method_type=%s",
                    sorted(raw.keys()),
                    type(raw.get("method")).__name__,
                )
                continue

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

            # Tool-call routing happens BEFORE on_event so the response is
            # already in flight when the orchestrator sees the event.
            if event_name == _EVENT_TOOL_CALL:
                handled_event = await self._handle_tool_call(payload)
                if handled_event is not None:
                    on_event(handled_event)
                else:
                    on_event(event)
                continue

            if event_name == EVENT_APPROVAL_REQUEST:
                approval_event = await self._handle_approval_request(payload)
                on_event(approval_event)
                continue

            on_event(event)

            if event_name == _EVENT_TURN_COMPLETED:
                return TurnResult.succeeded(turn_id=turn_id, payload=payload)
            if event_name in (_EVENT_TURN_FAILED, _EVENT_TURN_ENDED_WITH_ERROR):
                reason = payload.get("reason") or payload.get("message") or event_name
                raise TurnFailed(f"turn {turn_id} failed: {reason}")
            if event_name == _EVENT_TURN_CANCELLED:
                raise TurnCancelled(f"turn {turn_id} cancelled")
            if event_name == _EVENT_TURN_INPUT_REQUIRED:
                raise TurnInputRequired(
                    f"turn {turn_id} requested user input "
                    "(high-trust posture treats this as hard failure)"
                )

    async def _handle_tool_call(
        self, payload: dict[str, Any]
    ) -> RuntimeEvent | None:
        """Route a tool-call notification (§10.5).

        - Supported registered tool (currently only ``linear_graphql``):
          dispatch synchronously, write a ``tool_call_response`` frame
          carrying the :class:`ToolResult`, and emit a
          ``tool_call_completed`` :class:`RuntimeEvent`.
        - Unknown / unsupported tool: write a failure ``tool_call_response``
          frame and emit ``unsupported_tool_call`` for observability so the
          session can keep running per §10.5.
        """
        tool_name = payload.get("toolName")
        call_id = payload.get("callId")

        if (
            tool_name == LINEAR_GRAPHQL_TOOL_NAME
            and self._linear_graphql_tool is not None
        ):
            return await self._dispatch_linear_graphql_tool(
                call_id=call_id, payload=payload
            )

        # Always send a tool_call_response — even when callId is missing or
        # of an unexpected type — so the agent's own pending tool-call entry
        # unblocks. Without this, an agent that emits a malformed tool_call
        # frame hangs waiting for a reply forever.
        response_call_id: Any = call_id if isinstance(call_id, (str, int)) else None
        with contextlib.suppress(Exception):
            await self._process.write_frame(
                {
                    "jsonrpc": JSONRPC_VERSION,
                    "method": _TOOL_CALL_RESPONSE_METHOD,
                    "params": {
                        "callId": response_call_id,
                        "ok": False,
                        "error": f"unsupported_tool: {tool_name!r}",
                    },
                }
            )

        return RuntimeEvent(
            event=_EVENT_UNSUPPORTED_TOOL_CALL,
            timestamp=datetime.now(UTC),
            codex_app_server_pid=self._codex_app_server_pid,
            payload={
                "toolName": tool_name,
                "callId": call_id,
                "originalArguments": payload.get("arguments"),
            },
            usage=None,
        )

    async def _dispatch_linear_graphql_tool(
        self,
        *,
        call_id: Any,
        payload: dict[str, Any],
    ) -> RuntimeEvent:
        """Run the wired :class:`LinearGraphqlTool`, reply, and synthesise a
        ``tool_call_completed`` event.

        The tool call is bounded by the same ``read_timeout_ms`` budget the
        protocol-level reads use — a hung tool can otherwise stall the
        whole turn until ``turn_timeout_ms`` fires (orders of magnitude
        coarser).
        """
        assert self._linear_graphql_tool is not None  # noqa: S101 -- type narrow
        arguments = payload.get("arguments")

        timed_out = False
        try:
            if self._read_timeout_ms is not None:
                result = await asyncio.wait_for(
                    self._linear_graphql_tool.execute(arguments),
                    timeout=self._read_timeout_ms / 1000.0,
                )
            else:
                result = await self._linear_graphql_tool.execute(arguments)
        except TimeoutError:
            timed_out = True
            result = None

        if timed_out:
            response_params: dict[str, Any] = {
                "callId": call_id,
                "ok": False,
                "error": "tool_timeout",
            }
            success = False
        else:
            assert result is not None  # noqa: S101 -- only None on timeout
            response_params = {
                "callId": call_id,
                "ok": result.success,
            }
            if result.data is not None:
                response_params["result"] = result.data
            if result.errors is not None:
                response_params["errors"] = result.errors
            if result.error_message is not None and not result.success:
                response_params["error"] = result.error_message
            success = result.success

        with contextlib.suppress(Exception):
            await self._process.write_frame(
                {
                    "jsonrpc": JSONRPC_VERSION,
                    "method": _TOOL_CALL_RESPONSE_METHOD,
                    "params": response_params,
                }
            )

        return RuntimeEvent(
            event=_EVENT_TOOL_CALL_COMPLETED,
            timestamp=datetime.now(UTC),
            codex_app_server_pid=self._codex_app_server_pid,
            payload={
                "toolName": LINEAR_GRAPHQL_TOOL_NAME,
                "callId": call_id,
                "ok": success,
                "timedOut": timed_out,
            },
            usage=None,
        )

    async def _handle_approval_request(
        self, payload: dict[str, Any]
    ) -> RuntimeEvent:
        """Route an ``approval_request`` notification through the policy
        handler. Writes the response back to the agent and synthesises the
        operator-visible ``approval_auto_approved`` / ``approval_denied``
        :class:`RuntimeEvent` (§10.4, §10.5).
        """
        response_frame, observability = self._approval_handler.build_response(
            payload
        )
        with contextlib.suppress(Exception):
            await self._process.write_frame(response_frame)
        assert observability is not None  # noqa: S101 -- handler always returns one
        return RuntimeEvent(
            event=observability["event"],
            timestamp=observability["timestamp"],
            codex_app_server_pid=self._codex_app_server_pid,
            payload=observability["payload"],
            usage=None,
        )

    # ------------------------------------------------------------------
    # Shutdown (Task 18)
    # ------------------------------------------------------------------

    async def stop_session(
        self,
        session: Session,
        *,
        graceful_timeout_s: float = 5.0,
    ) -> None:
        """Send a graceful shutdown, then escalate to terminate/kill.

        Idempotent: subsequent calls are no-ops. All best-effort —
        :meth:`stop_session` MUST NEVER raise so the worker's exit branches
        can call it unconditionally.
        """
        if self._stopped:
            return
        self._stopped = True

        # Best-effort graceful shutdown notification (no id, no reply expected).
        with contextlib.suppress(Exception):
            await self._process.write_frame(
                {
                    "jsonrpc": JSONRPC_VERSION,
                    "method": _SHUTDOWN_METHOD,
                    "params": {"sessionId": session.session_id},
                }
            )

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
        """Send a request, await the matching response under timeout."""
        await self._process.write_frame(request)
        try:
            raw = await asyncio.wait_for(
                self._process.read_frame(),
                timeout=read_timeout_ms / 1000.0,
            )
        except TimeoutError as exc:
            raise ResponseTimeout(
                f"codex {step} did not respond within {read_timeout_ms}ms"
            ) from exc

        response = parse_response(raw, expected_id=request["id"])
        if response.error is not None:
            raise ResponseError(
                f"codex {step} returned JSON-RPC error "
                f"code={response.error.code} message={response.error.message!r}"
            )
        return response


__all__ = [
    "CodexClient",
    "RuntimeEvent",
    "Session",
    "TurnResult",
]
