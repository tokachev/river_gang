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
    fake replies with id-correlated responses queued in advance.
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
            "result": {"threadId": thread_id},
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {"turnId": turn_id},
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

    methods = [f["method"] for f in fake.written_frames]
    ids = [f["id"] for f in fake.written_frames]
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

    thread_start = fake.written_frames[1]
    assert thread_start["params"]["cwd"] == str(tmp_path)
    assert thread_start["params"]["approvalPolicy"] == "never"
    assert thread_start["params"]["sandboxPolicy"] == "workspace-write"


async def test_start_session_thread_start_carries_issue_title(tmp_path: Path) -> None:
    """SPED §10.2: include "<identifier>: <title>" when title supported."""
    fake = _make_fake_for_handshake()
    client = CodexClient(process=fake, codex_app_server_pid=1)
    await client.start_session(
        workspace=tmp_path,
        prompt="P",
        issue=_issue(identifier="RG-7", title="Wire up retries"),
        approval_policy="never",
        sandbox_policy="workspace-write",
        read_timeout_ms=5000,
    )

    thread_start = fake.written_frames[1]
    assert thread_start["params"]["title"] == "RG-7: Wire up retries"


async def test_start_session_first_turn_carries_prompt(tmp_path: Path) -> None:
    """SPED §10.2: first turn carries the rendered issue prompt body."""
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

    turn_start = fake.written_frames[2]
    assert turn_start["params"]["threadId"] == "th-1"
    assert turn_start["params"]["prompt"] == "The full rendered prompt body"
    # Must NOT carry continuation-guidance fields on the first turn
    assert "guidance" not in turn_start["params"]
    assert "continuation" not in turn_start["params"]


async def test_start_session_first_turn_carries_issue_title(tmp_path: Path) -> None:
    fake = _make_fake_for_handshake()
    client = CodexClient(process=fake, codex_app_server_pid=1)
    await client.start_session(
        workspace=tmp_path,
        prompt="P",
        issue=_issue(identifier="RG-9", title="Add CLI"),
        approval_policy="never",
        sandbox_policy="workspace-write",
        read_timeout_ms=5000,
    )
    turn_start = fake.written_frames[2]
    assert turn_start["params"]["title"] == "RG-9: Add CLI"


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
    init_request = fake.written_frames[0]
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
