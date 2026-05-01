"""Tests for :class:`CodexClient.start_session` (SPED §10.2, §17.5)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest

from river_gang.codex.client import CodexClient, Session
from river_gang.codex.errors import (
    PortExit,
    ResponseError,
    ResponseTimeout,
)
from river_gang.codex.protocol import (
    METHOD_INITIALIZE,
    METHOD_THREAD_START,
    METHOD_TURN_START,
)
from river_gang.tracker.issue import Issue
from tests.codex.fakes import FakeCodexProcess

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _issue(identifier: str = "RG-1", title: str = "Implement feature") -> Issue:
    return Issue(
        id=f"uuid-{identifier}",
        identifier=identifier,
        title=title,
        state="Todo",
        description=None,
        priority=None,
        branch_name=None,
        url=None,
        labels=(),
        blocked_by=(),
        created_at=None,
        updated_at=None,
    )


def _make_fake_for_handshake(
    *,
    thread_id: str = "th-1",
    turn_id: str = "tn-1",
    initialize_capabilities: dict[str, object] | None = None,
) -> FakeCodexProcess:
    """Returns a fake process scripted to satisfy a full 3-step handshake.

    Each request from the client is read out of ``written_frames`` and the
    fake replies with id-correlated responses queued in advance. Responses
    follow the codex 0.125.0+ schema: ``thread/start`` wraps identity inside
    ``result.thread`` and ``turn/start`` inside ``result.turn``.
    """
    fake = FakeCodexProcess()
    # Pre-queue responses by id (the client always uses 1, 2, 3 in order).
    fake.queue(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": initialize_capabilities or {"capabilities": {}},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"thread": {"id": thread_id}},
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {"turn": {"id": turn_id, "status": "inProgress"}},
        },
    )
    return fake


# ---------------------------------------------------------------------------
# start_session — happy path
# ---------------------------------------------------------------------------


async def test_start_session_returns_session_with_ids(tmp_path: Path) -> None:
    fake = _make_fake_for_handshake(thread_id="th-abc", turn_id="tn-xyz")

    client = CodexClient(process=fake, codex_app_server_pid=12345)
    session = await client.start_session(
        workspace=tmp_path,
        prompt="Do the thing",
        issue=_issue(),
        approval_policy="never",
        sandbox_policy="workspace-write",
        read_timeout_ms=5000,
    )

    assert isinstance(session, Session)
    assert session.thread_id == "th-abc"
    assert session.first_turn_id == "tn-xyz"
    assert session.session_id == "th-abc-tn-xyz"
    assert session.codex_app_server_pid == 12345
    assert isinstance(session.started_at, datetime)


async def test_start_session_emits_three_jsonrpc_requests_in_order(
    tmp_path: Path,
) -> None:
    fake = _make_fake_for_handshake()
    client = CodexClient(process=fake, codex_app_server_pid=1)
    await client.start_session(
        workspace=tmp_path,
        prompt="P",
        issue=_issue(),
        approval_policy="never",
        sandbox_policy="workspace-write",
        read_timeout_ms=5000,
    )

    # Codex 0.125.0+ requires an ``initialized`` notification between the
    # initialize ack and any subsequent request — filter to id-bearing
    # frames to assert request ordering.
    requests = [f for f in fake.written_frames if "id" in f]
    methods = [f["method"] for f in requests]
    ids = [f["id"] for f in requests]
    assert methods == [
        METHOD_INITIALIZE,
        METHOD_THREAD_START,
        METHOD_TURN_START,
    ]
    assert ids == [1, 2, 3]


async def test_start_session_thread_start_uses_workspace_cwd(tmp_path: Path) -> None:
    fake = _make_fake_for_handshake()
    client = CodexClient(process=fake, codex_app_server_pid=1)
    await client.start_session(
        workspace=tmp_path,
        prompt="P",
        issue=_issue(),
        approval_policy="never",
        sandbox_policy="workspace-write",
        read_timeout_ms=5000,
    )

    thread_start = next(
        f for f in fake.written_frames if f.get("method") == METHOD_THREAD_START
    )
    # Codex 0.125.0+ ThreadStartParams renamed ``sandboxPolicy`` to ``sandbox``.
    assert thread_start["params"]["cwd"] == str(tmp_path)
    assert thread_start["params"]["approvalPolicy"] == "never"
    assert thread_start["params"]["sandbox"] == "workspace-write"


async def test_start_session_first_turn_carries_prompt(tmp_path: Path) -> None:
    """SPED §10.2: first turn carries the rendered issue prompt body.

    Codex 0.125.0+ TurnStartParams takes ``input: UserInput[]`` (each
    ``{type: "text", text}``) instead of legacy ``prompt: string``.
    """
    fake = _make_fake_for_handshake(thread_id="th-1")
    client = CodexClient(process=fake, codex_app_server_pid=1)
    await client.start_session(
        workspace=tmp_path,
        prompt="The full rendered prompt body",
        issue=_issue(),
        approval_policy="never",
        sandbox_policy="workspace-write",
        read_timeout_ms=5000,
    )

    turn_start = next(
        f for f in fake.written_frames if f.get("method") == METHOD_TURN_START
    )
    assert turn_start["params"]["threadId"] == "th-1"
    assert turn_start["params"]["input"] == [
        {"type": "text", "text": "The full rendered prompt body"}
    ]
    # Legacy field MUST NOT leak.
    assert "prompt" not in turn_start["params"]
    # Must NOT carry continuation-guidance fields on the first turn.
    assert "guidance" not in turn_start["params"]
    assert "continuation" not in turn_start["params"]


async def test_start_session_initialize_advertises_client_info(tmp_path: Path) -> None:
    fake = _make_fake_for_handshake()
    client = CodexClient(process=fake, codex_app_server_pid=1)
    await client.start_session(
        workspace=tmp_path,
        prompt="P",
        issue=_issue(),
        approval_policy="never",
        sandbox_policy="workspace-write",
        read_timeout_ms=5000,
    )
    init_request = next(
        f for f in fake.written_frames if f.get("method") == METHOD_INITIALIZE
    )
    info = init_request["params"]["clientInfo"]
    assert info["name"] == "river-gang"
    assert isinstance(info["version"], str)
    assert info["version"]


async def test_start_session_request_payloads_are_json_serialisable(
    tmp_path: Path,
) -> None:
    """Every emitted request must round-trip through json.dumps/loads."""
    fake = _make_fake_for_handshake()
    client = CodexClient(process=fake, codex_app_server_pid=1)
    await client.start_session(
        workspace=tmp_path,
        prompt="P",
        issue=_issue(),
        approval_policy="never",
        sandbox_policy="workspace-write",
        read_timeout_ms=5000,
    )
    for frame in fake.written_frames:
        assert json.loads(json.dumps(frame)) == frame


# ---------------------------------------------------------------------------
# Interleaved frames during handshake
# ---------------------------------------------------------------------------


async def test_start_session_tolerates_interleaved_notification_before_response(
    tmp_path: Path,
) -> None:
    """Codex MAY emit a notification (e.g. thread/started) before the
    matching response. The handshake must skip past it and still locate
    the id-correlated response — naive read-one-frame logic would hand
    the notification to ``parse_response`` which would raise."""
    fake = FakeCodexProcess()
    fake.queue(
        # initialize ack preceded by an unrelated notification.
        {"jsonrpc": "2.0", "method": "thread/started", "params": {"x": 1}},
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"thread": {"id": "th-A"}}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {"turn": {"id": "tn-A", "status": "inProgress"}},
        },
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)
    session = await client.start_session(
        workspace=tmp_path,
        prompt="P",
        issue=_issue(),
        approval_policy="never",
        sandbox_policy="workspace-write",
        read_timeout_ms=5000,
    )
    assert session.thread_id == "th-A"
    assert session.first_turn_id == "tn-A"


async def test_start_session_dispatches_server_request_during_handshake(
    tmp_path: Path,
) -> None:
    """A server request preceding a handshake response must be dispatched
    (with a JSON-RPC reply) and the handshake still complete — codex
    waits on the dispatcher reply before sending the next response."""
    fake = FakeCodexProcess()
    fake.queue(
        # Server request arrives before the initialize ack.
        {
            "jsonrpc": "2.0",
            "id": 9999,
            "method": "some/server/request",
            "params": {"k": "v"},
        },
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"thread": {"id": "th-B"}}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {"turn": {"id": "tn-B", "status": "inProgress"}},
        },
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)
    session = await client.start_session(
        workspace=tmp_path,
        prompt="P",
        issue=_issue(),
        approval_policy="never",
        sandbox_policy="workspace-write",
        read_timeout_ms=5000,
    )
    assert session.thread_id == "th-B"

    # The dispatcher wrote a -32601 reply for the unregistered method.
    replies = [
        f
        for f in fake.written_frames
        if f.get("id") == 9999 and "method" not in f
    ]
    assert len(replies) == 1, fake.written_frames
    assert replies[0]["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# Read timeout
# ---------------------------------------------------------------------------


async def test_start_session_read_timeout_raises_response_timeout(
    tmp_path: Path,
) -> None:
    """A fake process that never produces an ``initialize`` response must
    surface :class:`ResponseTimeout`, not hang forever."""

    class StallingFake(FakeCodexProcess):
        async def read_frame(self) -> dict[str, object]:
            await asyncio.sleep(60)  # never returns within the test timeout
            raise AssertionError("unreachable")

    fake = StallingFake()
    client = CodexClient(process=fake, codex_app_server_pid=1)

    with pytest.raises(ResponseTimeout):
        await client.start_session(
            workspace=tmp_path,
            prompt="P",
            issue=_issue(),
            approval_policy="never",
            sandbox_policy="workspace-write",
            read_timeout_ms=50,
        )


async def test_start_session_timeout_at_thread_start_step(tmp_path: Path) -> None:
    """First request succeeds, second hangs → ResponseTimeout from step 2."""
    fake = FakeCodexProcess()
    fake.queue(
        {"jsonrpc": "2.0", "id": 1, "result": {}},
    )
    # Replace read_frame so subsequent reads stall after the first.
    original_read = fake.read_frame
    call_counter = {"n": 0}

    async def stall_after_first() -> dict[str, object]:
        call_counter["n"] += 1
        if call_counter["n"] == 1:
            return await original_read()
        await asyncio.sleep(60)
        raise AssertionError("unreachable")

    fake.read_frame = stall_after_first  # type: ignore[method-assign]

    client = CodexClient(process=fake, codex_app_server_pid=1)
    with pytest.raises(ResponseTimeout):
        await client.start_session(
            workspace=tmp_path,
            prompt="P",
            issue=_issue(),
            approval_policy="never",
            sandbox_policy="workspace-write",
            read_timeout_ms=50,
        )


# ---------------------------------------------------------------------------
# Startup failure
# ---------------------------------------------------------------------------


async def test_start_session_initialize_error_response_raises(tmp_path: Path) -> None:
    fake = FakeCodexProcess(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32601, "message": "method not found"},
            }
        ]
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)
    with pytest.raises(ResponseError) as exc:
        await client.start_session(
            workspace=tmp_path,
            prompt="P",
            issue=_issue(),
            approval_policy="never",
            sandbox_policy="workspace-write",
            read_timeout_ms=5000,
        )
    assert "method not found" in str(exc.value)


async def test_start_session_thread_start_error_response_raises(
    tmp_path: Path,
) -> None:
    fake = FakeCodexProcess(
        [
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "error": {"code": -32000, "message": "thread limit reached"},
            },
        ]
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)
    with pytest.raises(ResponseError):
        await client.start_session(
            workspace=tmp_path,
            prompt="P",
            issue=_issue(),
            approval_policy="never",
            sandbox_policy="workspace-write",
            read_timeout_ms=5000,
        )


async def test_start_session_subprocess_exit_during_handshake_raises_port_exit(
    tmp_path: Path,
) -> None:
    """If the subprocess exits before completing the handshake, surface
    :class:`PortExit`. ``FakeCodexProcess`` raises PortExit on EOF."""
    fake = FakeCodexProcess()  # empty queue → first read raises PortExit
    client = CodexClient(process=fake, codex_app_server_pid=1)
    with pytest.raises(PortExit):
        await client.start_session(
            workspace=tmp_path,
            prompt="P",
            issue=_issue(),
            approval_policy="never",
            sandbox_policy="workspace-write",
            read_timeout_ms=5000,
        )


async def test_start_session_thread_id_extraction_failure_raises(
    tmp_path: Path,
) -> None:
    """thread.start succeeds but response shape lacks ``threadId``."""
    fake = FakeCodexProcess(
        [
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            {"jsonrpc": "2.0", "id": 2, "result": {"unexpected": True}},
        ]
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)
    with pytest.raises(ResponseError):
        await client.start_session(
            workspace=tmp_path,
            prompt="P",
            issue=_issue(),
            approval_policy="never",
            sandbox_policy="workspace-write",
            read_timeout_ms=5000,
        )


async def test_start_session_id_mismatch_in_response_raises(tmp_path: Path) -> None:
    fake = FakeCodexProcess(
        [
            # response carries id=99 but the request was id=1
            {"jsonrpc": "2.0", "id": 99, "result": {}},
        ]
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)
    with pytest.raises(ResponseError):
        await client.start_session(
            workspace=tmp_path,
            prompt="P",
            issue=_issue(),
            approval_policy="never",
            sandbox_policy="workspace-write",
            read_timeout_ms=5000,
        )


# ---------------------------------------------------------------------------
# Session shape
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# initialized notification (codex 0.125.0+ ClientNotification schema)
# ---------------------------------------------------------------------------


async def test_start_session_emits_initialized_notification_after_initialize(
    tmp_path: Path,
) -> None:
    """ClientNotification schema: client MUST send ``{method: "initialized"}``
    after ``initialize`` ack, BEFORE any subsequent calls (thread/start etc.).
    """
    fake = FakeCodexProcess()
    # New-shape responses (codex 0.125.0+): result wrapped in {thread:{id}}
    # / {turn:{id, status}}.
    fake.queue(
        {"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}},
        {"jsonrpc": "2.0", "id": 2, "result": {"thread": {"id": "th-1"}}},
        {"jsonrpc": "2.0", "id": 3, "result": {"turn": {"id": "tn-1"}}},
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)
    await client.start_session(
        workspace=tmp_path,
        prompt="P",
        issue=_issue(),
        approval_policy="never",
        sandbox_policy="workspace-write",
        read_timeout_ms=5000,
    )

    # Order of writes must be:
    #   1. initialize request    (id=1, has method+id+params)
    #   2. initialized           (notification, method only — no id)
    #   3. thread/start request  (id=2)
    #   4. turn/start request    (id=3)
    methods = [f.get("method") for f in fake.written_frames]
    assert methods[0] == "initialize"
    assert methods[1] == "initialized"
    assert methods[2] == "thread/start"
    assert methods[3] == "turn/start"

    initialized_frame = fake.written_frames[1]
    # ClientNotification.InitializedNotification: method only — no id, no params.
    assert "id" not in initialized_frame
    assert initialized_frame == {"method": "initialized"}


async def test_initialized_sent_before_thread_start_request(tmp_path: Path) -> None:
    """``initialized`` MUST land on the wire before any non-initialize request
    so codex sees the post-handshake signal before it processes thread/start.
    """
    fake = FakeCodexProcess()
    fake.queue(
        {"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}},
        {"jsonrpc": "2.0", "id": 2, "result": {"thread": {"id": "th-x"}}},
        {"jsonrpc": "2.0", "id": 3, "result": {"turn": {"id": "tn-x"}}},
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)
    await client.start_session(
        workspace=tmp_path,
        prompt="P",
        issue=_issue(),
        approval_policy="never",
        sandbox_policy="workspace-write",
        read_timeout_ms=5000,
    )

    initialized_idx = next(
        i
        for i, f in enumerate(fake.written_frames)
        if f.get("method") == "initialized"
    )
    thread_start_idx = next(
        i
        for i, f in enumerate(fake.written_frames)
        if f.get("method") == "thread/start"
    )
    assert initialized_idx < thread_start_idx


def test_session_dataclass_is_frozen() -> None:
    s = Session(
        thread_id="th",
        first_turn_id="tn",
        codex_app_server_pid=1,
        started_at=datetime.now(),
    )
    assert s.session_id == "th-tn"
    with pytest.raises(Exception):
        s.thread_id = "other"  # type: ignore[misc]
