"""Tests for :mod:`river_gang.codex.usage` (SPED §13.5)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from river_gang.codex.client import CodexClient, RuntimeEvent, Session
from river_gang.codex.usage import (
    METHOD_TOKEN_USAGE_UPDATED,
    RateLimitSnapshot,
    TokenSnapshot,
    compute_token_delta,
    extract_cumulative_tokens,
    extract_rate_limits,
)
from tests.codex.fakes import FakeCodexProcess

# ---------------------------------------------------------------------------
# Dataclass shapes
# ---------------------------------------------------------------------------


def test_token_snapshot_is_frozen() -> None:
    s = TokenSnapshot(input_tokens=1, output_tokens=2, total_tokens=3)
    with pytest.raises(Exception):
        s.input_tokens = 99  # type: ignore[misc]


def test_rate_limit_snapshot_is_frozen() -> None:
    s = RateLimitSnapshot(limit=100, remaining=50, reset_at="2026-04-29T00:00:00Z")
    with pytest.raises(Exception):
        s.limit = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# extract_cumulative_tokens — thread/tokenUsage/updated path
# ---------------------------------------------------------------------------


def test_extract_from_thread_token_usage_updated_snake_case() -> None:
    snap = extract_cumulative_tokens(
        METHOD_TOKEN_USAGE_UPDATED,
        {
            "input_tokens": 100,
            "output_tokens": 200,
            "total_tokens": 300,
        },
    )
    assert snap == TokenSnapshot(
        input_tokens=100, output_tokens=200, total_tokens=300
    )


def test_extract_from_thread_token_usage_updated_camel_case() -> None:
    """Be lenient about field names per §13.5."""
    snap = extract_cumulative_tokens(
        METHOD_TOKEN_USAGE_UPDATED,
        {
            "inputTokens": 100,
            "outputTokens": 200,
            "totalTokens": 300,
        },
    )
    assert snap == TokenSnapshot(
        input_tokens=100, output_tokens=200, total_tokens=300
    )


def test_extract_from_thread_token_usage_updated_nested_under_usage() -> None:
    snap = extract_cumulative_tokens(
        METHOD_TOKEN_USAGE_UPDATED,
        {"usage": {"input_tokens": 5, "output_tokens": 10, "total_tokens": 15}},
    )
    assert snap == TokenSnapshot(input_tokens=5, output_tokens=10, total_tokens=15)


def test_extract_prefers_outer_zero_snapshot_over_nested_usage_fallback() -> None:
    """An outer payload that legitimately reports an all-zero
    :class:`TokenSnapshot` must take precedence over the nested
    ``payload["usage"]`` fallback. Previously the ``or`` chain would
    skip an all-zero outer (relying on dataclass truthiness, which is
    brittle); explicit ``is None`` chaining keeps the outer authority.
    """
    snap = extract_cumulative_tokens(
        METHOD_TOKEN_USAGE_UPDATED,
        {
            # Outer reports all-zero
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            # Nested would otherwise win under truthiness-based fallback
            "usage": {"input_tokens": 5, "output_tokens": 10, "total_tokens": 15},
        },
    )
    assert snap == TokenSnapshot(input_tokens=0, output_tokens=0, total_tokens=0)


def test_extract_from_thread_token_usage_updated_missing_total_derives_from_sum() -> None:
    """If the payload omits ``total_tokens`` but has input+output, derive it."""
    snap = extract_cumulative_tokens(
        METHOD_TOKEN_USAGE_UPDATED,
        {"input_tokens": 7, "output_tokens": 13},
    )
    assert snap == TokenSnapshot(input_tokens=7, output_tokens=13, total_tokens=20)


def test_extract_from_thread_token_usage_updated_empty_payload_returns_none() -> None:
    assert extract_cumulative_tokens(METHOD_TOKEN_USAGE_UPDATED, {}) is None


# ---------------------------------------------------------------------------
# extract_cumulative_tokens — total_token_usage wrapper path
# ---------------------------------------------------------------------------


def test_extract_from_total_token_usage_wrapper() -> None:
    """Any event carrying ``total_token_usage`` exposes a cumulative snapshot."""
    snap = extract_cumulative_tokens(
        "token_count",
        {
            "total_token_usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "total_tokens": 30,
            },
            "last_token_usage": {  # delta — must be ignored
                "input_tokens": 1,
                "output_tokens": 2,
                "total_tokens": 3,
            },
        },
    )
    assert snap == TokenSnapshot(input_tokens=10, output_tokens=20, total_tokens=30)


def test_extract_from_total_token_usage_camel_case_key() -> None:
    snap = extract_cumulative_tokens(
        "token_count",
        {
            "totalTokenUsage": {
                "inputTokens": 4,
                "outputTokens": 6,
                "totalTokens": 10,
            }
        },
    )
    assert snap == TokenSnapshot(input_tokens=4, output_tokens=6, total_tokens=10)


def test_extract_total_token_usage_works_for_arbitrary_method_name() -> None:
    """The wrapper-key extraction is method-agnostic — the spec says 'within
    token-count wrapper events' but doesn't fix one method name."""
    snap = extract_cumulative_tokens(
        "agent_message",
        {
            "total_token_usage": {
                "input_tokens": 1, "output_tokens": 2, "total_tokens": 3,
            }
        },
    )
    assert snap == TokenSnapshot(input_tokens=1, output_tokens=2, total_tokens=3)


# ---------------------------------------------------------------------------
# extract_cumulative_tokens — IGNORE paths
# ---------------------------------------------------------------------------


def test_extract_returns_none_for_last_token_usage_only_payload() -> None:
    """Delta-only payload (``last_token_usage`` without ``total_token_usage``)
    must NOT be reported as cumulative."""
    snap = extract_cumulative_tokens(
        "token_count",
        {
            "last_token_usage": {
                "input_tokens": 5, "output_tokens": 10, "total_tokens": 15,
            }
        },
    )
    assert snap is None


def test_extract_returns_none_for_method_named_last_token_usage() -> None:
    snap = extract_cumulative_tokens(
        "last_token_usage",
        {"input_tokens": 5, "output_tokens": 10, "total_tokens": 15},
    )
    assert snap is None


def test_extract_returns_none_for_generic_event_with_usage_map() -> None:
    """SPED §13.5: 'Do not treat generic usage maps as cumulative totals
    unless the event type defines them that way.'"""
    snap = extract_cumulative_tokens(
        "agent_message",
        {"usage": {"input_tokens": 100, "output_tokens": 200, "total_tokens": 300}},
    )
    assert snap is None


def test_extract_returns_none_when_payload_has_no_token_info() -> None:
    assert extract_cumulative_tokens("notification", {"text": "hi"}) is None


def test_extract_returns_none_for_non_dict_total_token_usage() -> None:
    snap = extract_cumulative_tokens(
        "token_count",
        {"total_token_usage": "not-a-dict"},
    )
    assert snap is None


def test_extract_returns_none_for_non_int_token_values() -> None:
    """Be strict about types — junk values should not produce a snapshot."""
    snap = extract_cumulative_tokens(
        METHOD_TOKEN_USAGE_UPDATED,
        {"input_tokens": "many", "output_tokens": "more", "total_tokens": "lots"},
    )
    assert snap is None


def test_extract_rejects_bool_token_values() -> None:
    """``True``/``False`` are int subclasses but never real token counts."""
    snap = extract_cumulative_tokens(
        METHOD_TOKEN_USAGE_UPDATED,
        {"input_tokens": True, "output_tokens": False, "total_tokens": 1},
    )
    assert snap is None


# ---------------------------------------------------------------------------
# Repeated absolute totals → caller computes deltas
# ---------------------------------------------------------------------------


def test_compute_token_delta_first_report_is_full_snapshot() -> None:
    current = TokenSnapshot(input_tokens=100, output_tokens=200, total_tokens=300)
    delta = compute_token_delta(previous=None, current=current)
    assert delta == TokenSnapshot(input_tokens=100, output_tokens=200, total_tokens=300)


def test_compute_token_delta_avoids_double_counting() -> None:
    prev = TokenSnapshot(input_tokens=100, output_tokens=200, total_tokens=300)
    curr = TokenSnapshot(input_tokens=110, output_tokens=215, total_tokens=325)
    delta = compute_token_delta(previous=prev, current=curr)
    assert delta == TokenSnapshot(input_tokens=10, output_tokens=15, total_tokens=25)


def test_compute_token_delta_no_change_returns_zero_snapshot() -> None:
    snap = TokenSnapshot(input_tokens=5, output_tokens=5, total_tokens=10)
    delta = compute_token_delta(previous=snap, current=snap)
    assert delta == TokenSnapshot(input_tokens=0, output_tokens=0, total_tokens=0)


def test_compute_token_delta_clamps_negative_to_zero() -> None:
    """If the cumulative total ever goes backwards (server reset, retry, etc.)
    we clamp to zero rather than emit a negative delta that would corrupt
    the running aggregate."""
    prev = TokenSnapshot(input_tokens=100, output_tokens=200, total_tokens=300)
    curr = TokenSnapshot(input_tokens=50, output_tokens=190, total_tokens=240)
    delta = compute_token_delta(previous=prev, current=curr)
    assert delta == TokenSnapshot(input_tokens=0, output_tokens=0, total_tokens=0)


# ---------------------------------------------------------------------------
# extract_rate_limits
# ---------------------------------------------------------------------------


def test_extract_rate_limits_from_dedicated_field() -> None:
    snap = extract_rate_limits(
        {"rate_limit": {"limit": 1000, "remaining": 900, "reset_at": "X"}},
    )
    assert snap == RateLimitSnapshot(limit=1000, remaining=900, reset_at="X")


def test_extract_rate_limits_camel_case_keys() -> None:
    snap = extract_rate_limits(
        {"rateLimit": {"limit": 100, "remaining": 50, "resetAt": "2026-01-01"}},
    )
    assert snap == RateLimitSnapshot(
        limit=100, remaining=50, reset_at="2026-01-01"
    )


def test_extract_rate_limits_partial_payload_keeps_what_present() -> None:
    snap = extract_rate_limits({"rate_limit": {"remaining": 50}})
    assert snap == RateLimitSnapshot(limit=None, remaining=50, reset_at=None)


def test_extract_rate_limits_returns_none_when_absent() -> None:
    assert extract_rate_limits({"text": "hi"}) is None


def test_extract_rate_limits_returns_none_for_non_dict_value() -> None:
    assert extract_rate_limits({"rate_limit": "not-a-dict"}) is None


def test_extract_rate_limits_returns_none_for_empty_rate_limit_object() -> None:
    """An empty rate_limit map carries no signal — return None."""
    assert extract_rate_limits({"rate_limit": {}}) is None


# ---------------------------------------------------------------------------
# Integration with stream_turn — RuntimeEvent.usage gets populated
# ---------------------------------------------------------------------------


def _session() -> Session:
    return Session(
        thread_id="th-1",
        first_turn_id="tn-0",
        codex_app_server_pid=12345,
        started_at=datetime.now(UTC),
    )


def _ack(turn_id: str = "tn-1", *, request_id: int = 1) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": {"turnId": turn_id}}


def _evt(method: str, **params: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "method": method, "params": dict(params)}


async def test_stream_turn_populates_runtime_event_usage_for_token_event() -> None:
    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        _evt(
            METHOD_TOKEN_USAGE_UPDATED,
            input_tokens=100,
            output_tokens=200,
            total_tokens=300,
        ),
        _evt("turn_completed"),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)
    received: list[RuntimeEvent] = []

    await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=received.append,
        turn_timeout_ms=5000,
    )

    token_events = [e for e in received if e.event == METHOD_TOKEN_USAGE_UPDATED]
    assert len(token_events) == 1
    usage = token_events[0].usage
    assert usage is not None
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 200
    assert usage["total_tokens"] == 300


async def test_stream_turn_leaves_runtime_event_usage_none_for_non_token_event() -> None:
    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        _evt("notification", text="hello"),
        _evt("turn_completed"),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)
    received: list[RuntimeEvent] = []
    await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=received.append,
        turn_timeout_ms=5000,
    )
    note = next(e for e in received if e.event == "notification")
    assert note.usage is None


async def test_stream_turn_does_not_promote_generic_usage_map_to_cumulative() -> None:
    """An ``agent_message`` event with a ``usage`` map MUST NOT populate
    ``RuntimeEvent.usage`` — that field is reserved for cumulative totals."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _evt(
            "agent_message",
            usage={"input_tokens": 5, "output_tokens": 5, "total_tokens": 10},
        ),
        _evt("turn_completed"),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)
    received: list[RuntimeEvent] = []
    await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=received.append,
        turn_timeout_ms=5000,
    )
    msg = next(e for e in received if e.event == "agent_message")
    assert msg.usage is None
