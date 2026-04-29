"""In-memory fakes for the codex layer (Tasks 16, 24a).

Two doubles:

- :class:`FakeCodexProcess` — frame-level fake of :class:`CodexProcess` used
  to drive the JSON-RPC streaming loop in M5 unit tests. Programmable
  script of inbound frames + recorded outbound writes.

- :class:`FakeCodexClient` — high-level fake of :class:`CodexClient` driven
  by a queue of :class:`TurnScenario` objects. Used by orchestrator
  integration tests in M7 — they don't care about the wire protocol and
  just need to choreograph turn outcomes (completed / failed / cancelled /
  input_required / timeout / port_exit).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from river_gang.codex.client import RuntimeEvent, Session, TurnResult
from river_gang.codex.errors import (
    CodexError,
    PortExit,
    TurnCancelled,
    TurnFailed,
    TurnInputRequired,
    TurnTimeout,
)
from river_gang.tracker.issue import Issue


class FakeCodexProcess:
    """A scripted stand-in for :class:`river_gang.codex.process.CodexProcess`.

    Usage:
        fake = FakeCodexProcess([{"a": 1}, {"a": 2}])
        async with fake:
            assert await fake.read_frame() == {"a": 1}
    """

    def __init__(
        self,
        frames: Iterable[dict[str, Any]] = (),
        *,
        terminal_error: CodexError | None = None,
        exit_code: int | None = 0,
        pid: int = 12345,
    ) -> None:
        self._frames: deque[dict[str, Any]] = deque(frames)
        self._terminal_error = terminal_error
        self._exit_code = exit_code
        self._pid = pid
        self.closed: bool = False
        self.frames_consumed: int = 0
        # public read of any frames the test code writes back, if it wants to
        self.written_frames: list[dict[str, Any]] = []
        # set when stop_session escalates past the graceful window
        self.kill_called: bool = False

    # ------------------------------------------------------------------
    # Programming the script after construction
    # ------------------------------------------------------------------

    def queue(self, *frames: dict[str, Any]) -> None:
        self._frames.extend(frames)

    def queue_error(self, error: CodexError) -> None:
        self._terminal_error = error

    # ------------------------------------------------------------------
    # CodexProcess surface
    # ------------------------------------------------------------------

    async def __aenter__(self) -> FakeCodexProcess:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def read_frame(self) -> dict[str, Any]:
        if self._frames:
            self.frames_consumed += 1
            return self._frames.popleft()
        if self._terminal_error is not None:
            raise self._terminal_error
        # No more frames and no terminal error → process exited.
        raise PortExit(
            f"FakeCodexProcess exhausted (exit_code={self._exit_code})"
        )

    async def write_frame(self, payload: dict[str, Any]) -> None:
        self.written_frames.append(payload)

    async def wait_for_exit(self, timeout: float) -> bool:
        """Default behaviour: pretend the process exited cleanly within the
        graceful window. Subclasses can override to simulate a stubborn
        process that ignores graceful shutdown."""
        return True

    async def aclose(self) -> None:
        self.closed = True

    @property
    def returncode(self) -> int | None:
        return self._exit_code if self.closed else None

    @property
    def pid(self) -> int:
        return self._pid


# ---------------------------------------------------------------------------
# FakeCodexClient — high-level scripted double for orchestrator tests
# ---------------------------------------------------------------------------


_TURN_OUTCOMES = (
    "completed",
    "failed",
    "cancelled",
    "input_required",
    "timeout",
    "port_exit",
)


@dataclass
class TurnScenario:
    """One queued turn outcome consumed by :meth:`FakeCodexClient.stream_turn`.

    Each entry in ``events`` becomes one :class:`RuntimeEvent` delivered to
    ``on_event`` BEFORE the outcome fires. Each event dict MAY contain:

    - ``event``: notification method name (required).
    - ``payload``: dict to attach to ``RuntimeEvent.payload`` (default ``{}``).
    - ``usage``: dict to attach to ``RuntimeEvent.usage`` (default ``None``).

    The outcome decides how the turn ends:

    - ``"completed"``       → returns :class:`TurnResult.succeeded` with
      ``payload=completion_payload`` and emits a synthesised
      ``turn_completed`` event (so tests don't have to put it in ``events``
      explicitly).
    - ``"failed"``          → raises :class:`TurnFailed` with optional
      ``reason``.
    - ``"cancelled"``       → raises :class:`TurnCancelled`.
    - ``"input_required"``  → raises :class:`TurnInputRequired`.
    - ``"timeout"``         → raises :class:`TurnTimeout`.
    - ``"port_exit"``       → raises :class:`PortExit`.
    """

    events: list[dict[str, Any]] = field(default_factory=list)
    outcome: Literal[
        "completed", "failed", "cancelled", "input_required", "timeout",
        "port_exit",
    ] = "completed"
    completion_payload: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    turn_id: str = "tn-1"

    def __post_init__(self) -> None:
        if self.outcome not in _TURN_OUTCOMES:
            raise ValueError(
                f"unknown TurnScenario outcome {self.outcome!r}; "
                f"expected one of {_TURN_OUTCOMES}"
            )


class FakeCodexClient:
    """Scripted stand-in for :class:`river_gang.codex.client.CodexClient`."""

    def __init__(
        self,
        *,
        thread_id: str = "th-1",
        first_turn_id: str = "tn-1",
        codex_app_server_pid: int = 12345,
        start_error: BaseException | None = None,
        stop_error: BaseException | None = None,
    ) -> None:
        self._thread_id = thread_id
        self._first_turn_id = first_turn_id
        self._codex_app_server_pid = codex_app_server_pid
        self._start_error = start_error
        self._stop_error = stop_error
        self._scenarios: deque[TurnScenario] = deque()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # ------------------------------------------------------------------
    # Configuration mutators
    # ------------------------------------------------------------------

    def queue_turn(self, scenario: TurnScenario) -> None:
        self._scenarios.append(scenario)

    # ------------------------------------------------------------------
    # CodexClient surface
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
        self.calls.append(
            (
                "start_session",
                {
                    "workspace": workspace,
                    "prompt": prompt,
                    "issue": issue,
                    "approval_policy": approval_policy,
                    "sandbox_policy": sandbox_policy,
                    "read_timeout_ms": read_timeout_ms,
                    "tracker_kind": tracker_kind,
                },
            )
        )
        if self._start_error is not None:
            raise self._start_error
        return Session(
            thread_id=self._thread_id,
            first_turn_id=self._first_turn_id,
            codex_app_server_pid=self._codex_app_server_pid,
            started_at=datetime.now(UTC),
        )

    async def stream_turn(
        self,
        *,
        session: Session,
        prompt: str,
        on_event: Callable[[RuntimeEvent], None],
        turn_timeout_ms: int,
        is_first_turn: bool = False,
    ) -> TurnResult:
        self.calls.append(
            (
                "stream_turn",
                {
                    "session": session,
                    "prompt": prompt,
                    "turn_timeout_ms": turn_timeout_ms,
                    "is_first_turn": is_first_turn,
                },
            )
        )
        assert self._scenarios, (
            "FakeCodexClient.stream_turn called with no queued scenario; "
            "tests must queue_turn(TurnScenario(...)) before each call"
        )
        scenario = self._scenarios.popleft()

        for raw_event in scenario.events:
            on_event(_runtime_event_from_dict(raw_event, self._codex_app_server_pid))

        if scenario.outcome == "completed":
            on_event(
                RuntimeEvent(
                    event="turn_completed",
                    timestamp=datetime.now(UTC),
                    codex_app_server_pid=self._codex_app_server_pid,
                    payload=dict(scenario.completion_payload),
                    usage=None,
                )
            )
            return TurnResult.succeeded(
                turn_id=scenario.turn_id,
                payload=dict(scenario.completion_payload),
            )

        reason = scenario.reason or scenario.outcome
        if scenario.outcome == "failed":
            raise TurnFailed(f"turn {scenario.turn_id} failed: {reason}")
        if scenario.outcome == "cancelled":
            raise TurnCancelled(f"turn {scenario.turn_id} cancelled")
        if scenario.outcome == "input_required":
            raise TurnInputRequired(
                f"turn {scenario.turn_id} requested user input"
            )
        if scenario.outcome == "timeout":
            raise TurnTimeout(
                f"turn {scenario.turn_id} exceeded {turn_timeout_ms}ms"
            )
        if scenario.outcome == "port_exit":
            raise PortExit(f"turn {scenario.turn_id}: subprocess exited")

        raise AssertionError(  # unreachable — guarded in __post_init__
            f"FakeCodexClient: unhandled outcome {scenario.outcome!r}"
        )

    async def stop_session(
        self,
        session: Session,
        *,
        graceful_timeout_s: float = 5.0,
    ) -> None:
        self.calls.append(
            (
                "stop_session",
                {"session": session, "graceful_timeout_s": graceful_timeout_s},
            )
        )
        # Mirror real CodexClient.stop_session: NEVER raises so callers can
        # invoke unconditionally. ``stop_error`` is recorded but suppressed.
        if self._stop_error is not None:
            return


def _runtime_event_from_dict(
    raw: dict[str, Any], codex_app_server_pid: int
) -> RuntimeEvent:
    event_name = raw.get("event")
    if not isinstance(event_name, str) or event_name == "":
        raise ValueError(
            "TurnScenario events must each carry a non-empty 'event' string"
        )
    payload = raw.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {"_raw": payload}
    usage = raw.get("usage")
    if usage is not None and not isinstance(usage, dict):
        usage = None
    return RuntimeEvent(
        event=event_name,
        timestamp=datetime.now(UTC),
        codex_app_server_pid=codex_app_server_pid,
        payload=dict(payload),
        usage=dict(usage) if usage is not None else None,
    )


__all__ = [
    "FakeCodexClient",
    "FakeCodexProcess",
    "TurnScenario",
]
