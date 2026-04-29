"""Tests for :class:`LinearTransport` (SPED §11.2, §11.4)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from river_gang.tracker.errors import (
    LinearApiRequest,
    LinearApiStatus,
    LinearGraphQLErrors,
    LinearUnknownPayload,
    MissingTrackerApiKey,
)
from river_gang.tracker.linear_transport import LinearTransport

ENDPOINT = "https://api.linear.app/graphql"


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_key", [None, ""])
def test_missing_api_key_raises(bad_key: str | None) -> None:
    with pytest.raises(MissingTrackerApiKey):
        LinearTransport(endpoint=ENDPOINT, api_key=bad_key)


def test_valid_construction_does_not_raise() -> None:
    t = LinearTransport(endpoint=ENDPOINT, api_key="lin_secret_123")
    assert t.endpoint == ENDPOINT
    assert t.timeout_seconds == 30.0


def test_custom_timeout_applied() -> None:
    t = LinearTransport(
        endpoint=ENDPOINT, api_key="lin_secret_123", timeout_seconds=10.0
    )
    assert t.timeout_seconds == 10.0


# ---------------------------------------------------------------------------
# Happy path: POST, headers, payload, returned data
# ---------------------------------------------------------------------------


@respx.mock
async def test_execute_posts_query_and_returns_data() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        captured["json"] = httpx._content.encode_request(json=None, content=request.content)  # type: ignore[attr-defined]
        # decode body manually
        import json as _json

        captured["body"] = _json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"data": {"viewer": {"id": "u-1"}}})

    route = respx.post(ENDPOINT).mock(side_effect=handler)

    transport = LinearTransport(endpoint=ENDPOINT, api_key="lin_secret_123")
    try:
        result = await transport.execute(
            "query Viewer { viewer { id } }", {"foo": "bar"}
        )
    finally:
        await transport.aclose()

    assert route.called
    assert result == {"viewer": {"id": "u-1"}}

    assert captured["url"] == ENDPOINT
    assert captured["body"] == {
        "query": "query Viewer { viewer { id } }",
        "variables": {"foo": "bar"},
    }


@respx.mock
async def test_execute_sends_authorization_without_bearer_prefix() -> None:
    """Linear convention (§11.2): raw token in ``Authorization`` header."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"data": {"ok": True}})

    respx.post(ENDPOINT).mock(side_effect=handler)

    transport = LinearTransport(endpoint=ENDPOINT, api_key="lin_secret_123")
    try:
        await transport.execute("query { ok }", {})
    finally:
        await transport.aclose()

    assert seen["authorization"] == "lin_secret_123"
    assert "bearer" not in seen["authorization"].lower()


@respx.mock
async def test_execute_sends_content_type_and_accept_json() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content-type"] = request.headers.get("content-type", "")
        seen["accept"] = request.headers.get("accept", "")
        return httpx.Response(200, json={"data": {"ok": True}})

    respx.post(ENDPOINT).mock(side_effect=handler)

    transport = LinearTransport(endpoint=ENDPOINT, api_key="lin_secret_123")
    try:
        await transport.execute("query { ok }", {})
    finally:
        await transport.aclose()

    assert "application/json" in seen["content-type"]
    assert "application/json" in seen["accept"]


@respx.mock
async def test_execute_with_no_variables_sends_empty_dict() -> None:
    body_seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        body_seen.update(_json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"data": {"ok": True}})

    respx.post(ENDPOINT).mock(side_effect=handler)

    transport = LinearTransport(endpoint=ENDPOINT, api_key="k")
    try:
        await transport.execute("query { ok }", {})
    finally:
        await transport.aclose()

    assert body_seen["variables"] == {}


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


@respx.mock
async def test_non_200_raises_linear_api_status() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(503, text="upstream is down")
    )

    transport = LinearTransport(endpoint=ENDPOINT, api_key="k")
    try:
        with pytest.raises(LinearApiStatus) as exc_info:
            await transport.execute("query { ok }", {})
    finally:
        await transport.aclose()

    err = exc_info.value
    assert err.status == 503
    assert "upstream is down" in err.body


@respx.mock
async def test_401_unauthorized_also_maps_to_linear_api_status() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(401, json={"message": "Bad token"})
    )

    transport = LinearTransport(endpoint=ENDPOINT, api_key="k")
    try:
        with pytest.raises(LinearApiStatus) as exc_info:
            await transport.execute("query { ok }", {})
    finally:
        await transport.aclose()
    assert exc_info.value.status == 401


@respx.mock
async def test_transport_exception_maps_to_linear_api_request() -> None:
    respx.post(ENDPOINT).mock(side_effect=httpx.ConnectError("boom"))

    transport = LinearTransport(endpoint=ENDPOINT, api_key="k")
    try:
        with pytest.raises(LinearApiRequest):
            await transport.execute("query { ok }", {})
    finally:
        await transport.aclose()


@respx.mock
async def test_timeout_exception_maps_to_linear_api_request() -> None:
    respx.post(ENDPOINT).mock(side_effect=httpx.TimeoutException("slow"))

    transport = LinearTransport(endpoint=ENDPOINT, api_key="k")
    try:
        with pytest.raises(LinearApiRequest):
            await transport.execute("query { ok }", {})
    finally:
        await transport.aclose()


@respx.mock
async def test_graphql_errors_array_raises_linear_graphql_errors() -> None:
    payload = {
        "errors": [
            {"message": "Field 'viewer' is not allowed", "extensions": {"code": "X"}},
            {"message": "second"},
        ]
    }
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=payload))

    transport = LinearTransport(endpoint=ENDPOINT, api_key="k")
    try:
        with pytest.raises(LinearGraphQLErrors) as exc_info:
            await transport.execute("query { ok }", {})
    finally:
        await transport.aclose()

    err = exc_info.value
    assert len(err.errors) == 2
    assert err.errors[0]["message"] == "Field 'viewer' is not allowed"


@respx.mock
async def test_graphql_errors_with_partial_data_still_raises() -> None:
    """Even when ``data`` is non-null alongside ``errors``, fail loudly so
    callers don't silently consume a partial response."""
    payload = {
        "data": {"viewer": None},
        "errors": [{"message": "permission denied"}],
    }
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=payload))

    transport = LinearTransport(endpoint=ENDPOINT, api_key="k")
    try:
        with pytest.raises(LinearGraphQLErrors):
            await transport.execute("query { ok }", {})
    finally:
        await transport.aclose()


@respx.mock
async def test_malformed_json_maps_to_linear_unknown_payload() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            content=b"<html>not json</html>",
            headers={"content-type": "application/json"},
        )
    )

    transport = LinearTransport(endpoint=ENDPOINT, api_key="k")
    try:
        with pytest.raises(LinearUnknownPayload):
            await transport.execute("query { ok }", {})
    finally:
        await transport.aclose()


@respx.mock
async def test_missing_data_and_errors_keys_maps_to_unknown_payload() -> None:
    """Server returns 200 + valid JSON but neither ``data`` nor ``errors``."""
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )

    transport = LinearTransport(endpoint=ENDPOINT, api_key="k")
    try:
        with pytest.raises(LinearUnknownPayload):
            await transport.execute("query { ok }", {})
    finally:
        await transport.aclose()


@respx.mock
async def test_data_is_non_dict_maps_to_unknown_payload() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"data": [1, 2, 3]})
    )

    transport = LinearTransport(endpoint=ENDPOINT, api_key="k")
    try:
        with pytest.raises(LinearUnknownPayload):
            await transport.execute("query { ok }", {})
    finally:
        await transport.aclose()


# ---------------------------------------------------------------------------
# Resource cleanup / no internal retry
# ---------------------------------------------------------------------------


@respx.mock
async def test_transport_does_not_retry_internally() -> None:
    """Plan §11: retry handled higher up. A single failure must surface
    immediately without the transport silently re-trying."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(500, text="internal")
    )

    transport = LinearTransport(endpoint=ENDPOINT, api_key="k")
    try:
        with pytest.raises(LinearApiStatus):
            await transport.execute("query { ok }", {})
    finally:
        await transport.aclose()

    assert route.call_count == 1


async def test_aclose_is_idempotent() -> None:
    transport = LinearTransport(endpoint=ENDPOINT, api_key="k")
    await transport.aclose()
    await transport.aclose()


@respx.mock
async def test_execute_after_aclose_raises_runtime_error() -> None:
    transport = LinearTransport(endpoint=ENDPOINT, api_key="k")
    await transport.aclose()
    with pytest.raises(RuntimeError):
        await transport.execute("query { ok }", {})


# ---------------------------------------------------------------------------
# Default timeout = 30s applied to httpx client
# ---------------------------------------------------------------------------


def test_default_timeout_is_30_seconds() -> None:
    transport = LinearTransport(endpoint=ENDPOINT, api_key="k")
    assert transport.timeout_seconds == 30.0
    # verify it's actually wired into the underlying client
    timeout = transport._client.timeout  # noqa: SLF001 -- read-only assertion
    assert timeout.connect == 30.0
    assert timeout.read == 30.0
    assert timeout.write == 30.0
    assert timeout.pool == 30.0
