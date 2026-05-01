"""Tests for :mod:`river_gang.codex.usage` (SPED §13.5).

Schema reference: ``sandbox/codex-schema/v2/ThreadTokenUsageUpdatedNotification.json``.
The notification ``params`` carry ``tokenUsage.total`` (cumulative) and
``tokenUsage.last`` (delta); we extract from ``tokenUsage.total`` only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

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
# Helpers — build schema-shaped payloads
# ---------------------------------------------------------------------------


def _breakdown(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int | None = None,
    cached_input_tokens: int = 0,
    reasoning_output_tokens: int = 0,
) -> dict[str, Any]:
    """A camelCase ``TokenUsageBreakdown`` per the codex schema."""
    return {
        "cachedInputTokens": cached_input_tokens,
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "reasoningOutputTokens": reasoning_output_tokens,
        "totalTokens": (
            total_tokens
            if total_tokens is not None
            else input_tokens + output_tokens
        ),
    }


def _token_usage_params(
    *,
    last: dict[str, Any] | None = None,
    total: dict[str, Any] | None = None,
    model_context_window: int | None = None,
    thread_id: str = "th-1",
    turn_id: str = "tn-1",
) -> dict[str, Any]:
    """A schema-shaped ``ThreadTokenUsageUpdatedNotification.params``."""
    token_usage: dict[str, Any] = {
        "last": last if last is not None else _breakdown(),
        "total": total if total is not None else _breakdown(),
    }
    if model_context_window is not None:
        token_usage["modelContextWindow"] = model_context_window
    return {
        "threadId": thread_id,
        "turnId": turn_id,
        "tokenUsage": token_usage,
    }


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


def test_extract_from_thread_token_usage_updated_camel_case() -> None:
    """Codex emits camelCase ``inputTokens`` / ``outputTokens`` / ``totalTokens``."""
    snap = extract_cumulative_tokens(
        METHOD_TOKEN_USAGE_UPDATED,
        _token_usage_params(
            total=_breakdown(input_tokens=100, output_tokens=200, total_tokens=300),
        ),
    )
    assert snap == TokenSnapshot(
        input_tokens=100, output_tokens=200, total_tokens=300
    )


def test_extract_from_thread_token_usage_updated_snake_case_lenience() -> None:
    """SPED §13.5 lenience — accept snake_case from non-codex producers."""
    snap = extract_cumulative_tokens(
        METHOD_TOKEN_USAGE_UPDATED,
        {
            "threadId": "th-1",
            "turnId": "tn-1",
            "token_usage": {
                "last": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                "total": {
                    "input_tokens": 5,
                    "output_tokens": 10,
                    "total_tokens": 15,
                },
            },
        },
    )
    assert snap == TokenSnapshot(input_tokens=5, output_tokens=10, total_tokens=15)


def test_extract_ignores_last_breakdown_uses_total_only() -> None:
    """``tokenUsage.last`` is a delta — must NOT be treated as cumulative."""
    snap = extract_cumulative_tokens(
        METHOD_TOKEN_USAGE_UPDATED,
        _token_usage_params(
            last=_breakdown(input_tokens=1, output_tokens=2, total_tokens=3),
            total=_breakdown(input_tokens=10, output_tokens=20, total_tokens=30),
        ),
    )
    assert snap == TokenSnapshot(input_tokens=10, output_tokens=20, total_tokens=30)


def test_extract_zero_total_breakdown_returns_zero_snapshot() -> None:
    """An all-zero ``total`` breakdown is a legitimate cumulative snapshot,
    not absent — we must report zeros rather than ``None``."""
    snap = extract_cumulative_tokens(
        METHOD_TOKEN_USAGE_UPDATED,
        _token_usage_params(
            total=_breakdown(input_tokens=0, output_tokens=0, total_tokens=0),
        ),
    )
    assert snap == TokenSnapshot(input_tokens=0, output_tokens=0, total_tokens=0)


def test_extract_missing_total_tokens_derives_from_sum() -> None:
    """If the breakdown omits ``totalTokens`` but has input+output, derive it.

    The codex schema marks ``totalTokens`` required, but lenience absorbs
    minor producer drift (§13.5)."""
    snap = extract_cumulative_tokens(
        METHOD_TOKEN_USAGE_UPDATED,
        {
            "threadId": "th-1",
            "turnId": "tn-1",
            "tokenUsage": {
                "last": _breakdown(),
                "total": {"inputTokens": 7, "outputTokens": 13},
            },
        },
    )
    assert snap == TokenSnapshot(input_tokens=7, output_tokens=13, total_tokens=20)


def test_extract_returns_none_when_token_usage_absent() -> None:
    """Notification missing the ``tokenUsage`` wrapper carries no signal."""
    assert extract_cumulative_tokens(
        METHOD_TOKEN_USAGE_UPDATED,
        {"threadId": "th-1", "turnId": "tn-1"},
    ) is None


def test_extract_returns_none_when_total_missing() -> None:
    """A ``tokenUsage`` wrapper without ``total`` cannot produce a cumulative."""
    assert extract_cumulative_tokens(
        METHOD_TOKEN_USAGE_UPDATED,
        {
            "threadId": "th-1",
            "turnId": "tn-1",
            "tokenUsage": {"last": _breakdown()},
        },
    ) is None


def test_extract_returns_none_for_empty_payload() -> None:
    assert extract_cumulative_tokens(METHOD_TOKEN_USAGE_UPDATED, {}) is None


# ---------------------------------------------------------------------------
# extract_cumulative_tokens — IGNORE paths (non-cumulative methods)
# ---------------------------------------------------------------------------


def test_extract_returns_none_for_unrelated_method() -> None:
    """SPED §13.5: 'Do not treat generic usage maps as cumulative totals
    unless the event type defines them that way.'"""
    snap = extract_cumulative_tokens(
        "agentMessage",
        {"usage": {"inputTokens": 100, "outputTokens": 200, "totalTokens": 300}},
    )
    assert snap is None


def test_extract_returns_none_for_unrelated_method_with_token_usage_key() -> None:
    """Even if some other notification happens to carry a ``tokenUsage`` key,
    we only trust the dedicated ``thread/tokenUsage/updated`` method."""
    snap = extract_cumulative_tokens(
        "turn/completed",
        _token_usage_params(
            total=_breakdown(input_tokens=1, output_tokens=2, total_tokens=3),
        ),
    )
    assert snap is None


def test_extract_returns_none_when_payload_has_no_token_info() -> None:
    assert extract_cumulative_tokens("notification", {"text": "hi"}) is None


def test_extract_returns_none_for_non_dict_token_usage() -> None:
    snap = extract_cumulative_tokens(
        METHOD_TOKEN_USAGE_UPDATED,
        {"threadId": "th-1", "turnId": "tn-1", "tokenUsage": "not-a-dict"},
    )
    assert snap is None


def test_extract_returns_none_for_non_dict_total() -> None:
    snap = extract_cumulative_tokens(
        METHOD_TOKEN_USAGE_UPDATED,
        {
            "threadId": "th-1",
            "turnId": "tn-1",
            "tokenUsage": {"last": _breakdown(), "total": "not-a-dict"},
        },
    )
    assert snap is None


def test_extract_returns_none_for_non_int_token_values() -> None:
    """Be strict about types — junk values should not produce a snapshot."""
    snap = extract_cumulative_tokens(
        METHOD_TOKEN_USAGE_UPDATED,
        _token_usage_params(
            total={
                "inputTokens": "many",
                "outputTokens": "more",
                "totalTokens": "lots",
                "cachedInputTokens": 0,
                "reasoningOutputTokens": 0,
            },
        ),
    )
    assert snap is None


def test_extract_rejects_bool_token_values() -> None:
    """``True``/``False`` are int subclasses but never real token counts."""
    snap = extract_cumulative_tokens(
        METHOD_TOKEN_USAGE_UPDATED,
        _token_usage_params(
            total={
                "inputTokens": True,
                "outputTokens": False,
                "totalTokens": 1,
                "cachedInputTokens": 0,
                "reasoningOutputTokens": 0,
            },
        ),
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
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"turn": {"id": turn_id, "status": "inProgress"}},
    }


def _evt(method: str, **params: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "method": method, "params": dict(params)}


def _turn_completed(turn_id: str = "tn-1", status: str = "completed") -> dict[str, object]:
    return _evt("turn/completed", turn={"id": turn_id, "status": status})


async def test_stream_turn_populates_runtime_event_usage_for_token_event() -> None:
    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        _evt(
            METHOD_TOKEN_USAGE_UPDATED,
            **_token_usage_params(
                total=_breakdown(input_tokens=100, output_tokens=200, total_tokens=300),
            ),
        ),
        _turn_completed(),
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
        _turn_completed(),
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
    """An ``agentMessage`` event with a ``usage`` map MUST NOT populate
    ``RuntimeEvent.usage`` — that field is reserved for cumulative totals
    from ``thread/tokenUsage/updated`` only."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _evt(
            "agentMessage",
            usage={"inputTokens": 5, "outputTokens": 5, "totalTokens": 10},
        ),
        _turn_completed(),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)
    received: list[RuntimeEvent] = []
    await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=received.append,
        turn_timeout_ms=5000,
    )
    msg = next(e for e in received if e.event == "agentMessage")
    assert msg.usage is None
