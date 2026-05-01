"""Tests for :mod:`river_gang.codex.protocol` (SPED §10.2, §17.5)."""

from __future__ import annotations

import json
from importlib.metadata import version as pkg_version

import pytest

from river_gang.codex.errors import ResponseError
from river_gang.codex.protocol import (
    METHOD_INITIALIZE,
    METHOD_THREAD_START,
    METHOD_TURN_START,
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

# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------


def test_build_initialize_request_envelope() -> None:
    req = build_initialize_request(id=1)
    assert "jsonrpc" not in req
    assert req["id"] == 1
    assert req["method"] == METHOD_INITIALIZE
    assert "params" in req


def test_build_initialize_request_includes_client_info_with_name_and_version() -> None:
    """SPED §17.5 + plan: clientInfo MUST advertise name='river-gang' and the
    package version."""
    req = build_initialize_request(id=1)
    info = req["params"]["clientInfo"]
    assert info["name"] == "river-gang"
    assert info["version"] == pkg_version("river_gang")


def test_build_initialize_request_round_trips_through_json() -> None:
    req = build_initialize_request(id=42)
    raw = json.dumps(req)
    decoded = json.loads(raw)
    assert decoded == req


# ---------------------------------------------------------------------------
# thread.start
# ---------------------------------------------------------------------------


def test_build_thread_start_request_envelope() -> None:
    req = build_thread_start_request(
        id=2,
        cwd="/abs/path/ws",
        approval_policy="never",
        sandbox_policy="workspace-write",
    )
    assert "jsonrpc" not in req
    assert req["id"] == 2
    assert req["method"] == METHOD_THREAD_START


def test_thread_start_carries_cwd_and_policies() -> None:
    """Codex 0.125.0+ ThreadStartParams uses ``sandbox`` (SandboxMode), not
    legacy ``sandboxPolicy``."""
    req = build_thread_start_request(
        id=2,
        cwd="/abs/path/ws",
        approval_policy="never",
        sandbox_policy="workspace-write",
    )
    params = req["params"]
    assert params["cwd"] == "/abs/path/ws"
    assert params["approvalPolicy"] == "never"
    assert params["sandbox"] == "workspace-write"
    # Legacy field MUST NOT be sent — codex schema validation rejects it.
    assert "sandboxPolicy" not in params


def test_thread_start_omits_title() -> None:
    """ThreadStartParams has no ``title`` field in the v2 schema."""
    req = build_thread_start_request(
        id=2,
        cwd="/abs",
        approval_policy="never",
        sandbox_policy="workspace-write",
    )
    assert "title" not in req["params"]


# ---------------------------------------------------------------------------
# turn.start (first turn)
# ---------------------------------------------------------------------------


def test_build_turn_start_request_envelope() -> None:
    req = build_turn_start_request(id=3, thread_id="th-1", prompt="Do the thing")
    assert "jsonrpc" not in req
    assert req["id"] == 3
    assert req["method"] == METHOD_TURN_START


def test_turn_start_carries_thread_id_and_input_array() -> None:
    """Codex 0.125.0+ TurnStartParams takes ``input: UserInput[]``, not
    ``prompt: string``. River-gang only sends text input."""
    req = build_turn_start_request(id=3, thread_id="th-1", prompt="Do the thing")
    params = req["params"]
    assert params["threadId"] == "th-1"
    assert params["input"] == [{"type": "text", "text": "Do the thing"}]
    # Legacy field must not leak.
    assert "prompt" not in params


def test_turn_start_omits_title() -> None:
    """TurnStartParams has no ``title`` field in the v2 schema."""
    req = build_turn_start_request(id=3, thread_id="th-1", prompt="P")
    assert "title" not in req["params"]


def test_turn_start_no_continuation_field_on_first_turn() -> None:
    req = build_turn_start_request(id=3, thread_id="th-1", prompt="P")
    # First-turn request carries the prompt body in `input`, not a
    # continuation marker.
    assert "continuation" not in req["params"]
    assert "guidance" not in req["params"]
    assert req["params"]["input"] == [{"type": "text", "text": "P"}]


# ---------------------------------------------------------------------------
# continuation turn (no original prompt resent)
# ---------------------------------------------------------------------------


def test_continuation_turn_request_envelope() -> None:
    req = build_continuation_turn_request(
        id=4, thread_id="th-1", guidance="continue"
    )
    assert req["method"] == METHOD_TURN_START
    assert req["params"]["threadId"] == "th-1"


def test_continuation_turn_omits_original_prompt() -> None:
    """SPED §10.2: continuation turns send only continuation guidance in the
    same ``input`` array shape as first turns. They MUST NOT resend the
    original prompt that's already in thread history."""
    req = build_continuation_turn_request(
        id=4, thread_id="th-1", guidance="please continue"
    )
    params = req["params"]
    assert "prompt" not in params
    assert "guidance" not in params  # legacy bespoke field, removed
    assert params["input"] == [{"type": "text", "text": "please continue"}]


# ---------------------------------------------------------------------------
# parse_response
# ---------------------------------------------------------------------------


def test_parse_response_returns_result_when_no_error() -> None:
    raw = {"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {"foo": True}}}
    res = parse_response(raw, expected_id=1)
    assert isinstance(res, JsonRpcResponse)
    assert res.id == 1
    assert res.error is None
    assert res.result == {"capabilities": {"foo": True}}


def test_parse_response_returns_error_object() -> None:
    raw = {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32000, "message": "boom", "data": {"x": 1}},
    }
    res = parse_response(raw, expected_id=7)
    assert res.result is None
    assert res.error is not None
    assert res.error.code == -32000
    assert res.error.message == "boom"
    assert res.error.data == {"x": 1}


def test_parse_response_id_mismatch_raises() -> None:
    raw = {"jsonrpc": "2.0", "id": 99, "result": {}}
    with pytest.raises(ResponseError):
        parse_response(raw, expected_id=1)


def test_parse_response_tolerates_missing_jsonrpc_field() -> None:
    """Codex 0.125.0+ schema does not carry a ``jsonrpc`` envelope field —
    parser must accept responses without it."""
    raw = {"id": 1, "result": {}}
    res = parse_response(raw, expected_id=1)
    assert res.id == 1
    assert res.result == {}


def test_parse_response_tolerates_legacy_jsonrpc_field() -> None:
    """If a future Codex version reintroduces the field, ignore it rather
    than reject the response."""
    raw = {"jsonrpc": "2.0", "id": 1, "result": {}}
    res = parse_response(raw, expected_id=1)
    assert res.id == 1


def test_parse_response_neither_result_nor_error_raises() -> None:
    raw = {"jsonrpc": "2.0", "id": 1}
    with pytest.raises(ResponseError):
        parse_response(raw, expected_id=1)


def test_parse_response_both_result_and_error_raises() -> None:
    raw = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {},
        "error": {"code": 1, "message": "x"},
    }
    with pytest.raises(ResponseError):
        parse_response(raw, expected_id=1)


def test_parse_response_notification_without_id_raises() -> None:
    """A notification (no ``id``) is not a valid response to a request."""
    raw = {"jsonrpc": "2.0", "method": "event", "params": {}}
    with pytest.raises(ResponseError):
        parse_response(raw, expected_id=1)


def test_parse_response_non_dict_result_raises() -> None:
    """``result`` MUST be an object — strings/ints/null aren't valid."""
    for bogus in ("not-a-dict", 42, None, [1, 2, 3]):
        with pytest.raises(ResponseError, match="result"):
            parse_response({"id": 1, "result": bogus}, expected_id=1)


def test_parse_response_non_dict_error_raises() -> None:
    """``error`` MUST be an object — strings/ints aren't valid."""
    for bogus in ("oops", 42, [1, 2]):
        with pytest.raises(ResponseError, match="error"):
            parse_response({"id": 1, "error": bogus}, expected_id=1)


# ---------------------------------------------------------------------------
# Identity extraction
# ---------------------------------------------------------------------------


def test_extract_thread_id_from_result() -> None:
    """ThreadStartResponse wraps the id inside a ``thread`` object."""
    assert extract_thread_id({"thread": {"id": "th-abc"}}) == "th-abc"


def test_extract_turn_id_from_result() -> None:
    """TurnStartResponse wraps the id inside a ``turn`` object."""
    assert extract_turn_id({"turn": {"id": "tn-xyz", "status": "inProgress"}}) == "tn-xyz"


def test_extract_thread_id_missing_thread_object_raises() -> None:
    with pytest.raises(ResponseError):
        extract_thread_id({})


def test_extract_thread_id_missing_id_inside_thread_raises() -> None:
    with pytest.raises(ResponseError):
        extract_thread_id({"thread": {}})


def test_extract_turn_id_missing_turn_object_raises() -> None:
    with pytest.raises(ResponseError):
        extract_turn_id({})


def test_extract_turn_id_missing_id_inside_turn_raises() -> None:
    with pytest.raises(ResponseError):
        extract_turn_id({"turn": {}})


def test_extract_thread_id_non_string_raises() -> None:
    with pytest.raises(ResponseError):
        extract_thread_id({"thread": {"id": 123}})


def test_extract_turn_id_non_string_raises() -> None:
    with pytest.raises(ResponseError):
        extract_turn_id({"turn": {"id": None}})


def test_extract_thread_id_rejects_legacy_top_level_field() -> None:
    """Pre-0.125.0 servers sent ``result.threadId`` at top level. New schema
    requires ``result.thread.id`` — extractor must not accidentally fall
    back to the legacy field, which would mask a real schema mismatch."""
    with pytest.raises(ResponseError):
        extract_thread_id({"threadId": "th-legacy"})


def test_extract_turn_id_rejects_legacy_top_level_field() -> None:
    with pytest.raises(ResponseError):
        extract_turn_id({"turnId": "tn-legacy"})


def test_compose_session_id() -> None:
    """SPED §10.2: session_id = "<thread_id>-<turn_id>"."""
    assert compose_session_id("th-1", "tn-1") == "th-1-tn-1"
    assert compose_session_id("abc", "xyz") == "abc-xyz"
