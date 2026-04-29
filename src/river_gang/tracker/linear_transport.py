"""Linear GraphQL HTTP transport (SPED §11.2, §11.4).

Thin async wrapper around :class:`httpx.AsyncClient`. Single responsibility:
POST a GraphQL document + variables, decode the JSON envelope, and raise a
typed exception on every failure mode.

No retries here — the orchestrator owns retry/backoff. No introspection of
GraphQL document contents — :func:`execute` is opaque to the query string.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from river_gang.tracker.errors import (
    LinearApiRequest,
    LinearApiStatus,
    LinearGraphQLErrors,
    LinearUnknownPayload,
    MissingTrackerApiKey,
)

DEFAULT_TIMEOUT_SECONDS = 30.0


class LinearTransport:
    """POST GraphQL queries to a Linear-compatible endpoint.

    Args:
        endpoint: Full GraphQL endpoint URL.
        api_key: Linear API token. Sent verbatim in the ``Authorization``
            header — Linear convention is NO ``Bearer`` prefix (§11.2).
        timeout_seconds: Applied to connect/read/write/pool. Default 30s.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str | None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if api_key is None or api_key == "":
            raise MissingTrackerApiKey(
                "tracker.api_key is required to construct LinearTransport"
            )

        self._endpoint = endpoint
        self._timeout_seconds = timeout_seconds
        # ``trust_env=False`` so the client ignores ambient HTTP_PROXY/
        # ALL_PROXY/etc. We treat the endpoint URL as the single source of
        # truth; egress routing belongs in deployment config, not implicit
        # environment leakage.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            trust_env=False,
        )
        self._closed = False

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._client.aclose()

    async def execute(
        self, query: str, variables: dict[str, Any]
    ) -> dict[str, Any]:
        """POST ``{query, variables}`` to the endpoint and return ``data``.

        Raises:
            LinearApiRequest: connection/timeout/DNS/etc.
            LinearApiStatus: non-200 HTTP response.
            LinearGraphQLErrors: response carried ``errors[]`` (regardless of
                whether ``data`` was also present).
            LinearUnknownPayload: malformed JSON or unexpected envelope.
        """

        if self._closed:
            raise RuntimeError("LinearTransport is closed")

        payload = {"query": query, "variables": variables}

        try:
            response = await self._client.post(self._endpoint, json=payload)
        except httpx.HTTPError as exc:
            raise LinearApiRequest(f"transport failure: {exc}") from exc

        if response.status_code != 200:
            raise LinearApiStatus(response.status_code, response.text)

        try:
            envelope = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise LinearUnknownPayload(
                f"response body is not valid JSON: {exc}"
            ) from exc

        if not isinstance(envelope, dict):
            raise LinearUnknownPayload(
                f"GraphQL envelope must be an object, got {type(envelope).__name__}"
            )

        errors = envelope.get("errors")
        if errors:
            if not isinstance(errors, list):
                raise LinearUnknownPayload(
                    f"GraphQL 'errors' must be a list, got {type(errors).__name__}"
                )
            partial_data = envelope.get("data")
            if not isinstance(partial_data, dict):
                partial_data = None
            raise LinearGraphQLErrors(errors, data=partial_data)

        if "data" not in envelope:
            raise LinearUnknownPayload(
                "GraphQL envelope missing both 'data' and 'errors' keys"
            )

        data = envelope["data"]
        if not isinstance(data, dict):
            raise LinearUnknownPayload(
                f"GraphQL 'data' must be an object, got {type(data).__name__}"
            )

        return data
