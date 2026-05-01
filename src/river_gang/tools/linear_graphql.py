"""``linear_graphql`` client-side tool (SPED §10.5).

Two responsibilities:

1. **Input validation** — :func:`validate_input` accepts the agent-supplied
   payload in either the object form ``{"query": str, "variables": dict}``
   or as a bare-string shorthand, and enforces the "one operation per call"
   rule via the AST-based :func:`count_operations` helper. A regex over the
   raw document would mis-count string literals like ``"query A { x }"``.

2. **Execution** — :class:`LinearGraphqlTool` reuses the already-configured
   :class:`LinearTransport` (no new credentials, no second client) and
   shapes every outcome into a :class:`ToolResult`:

   * 200 + ``data`` only             → ``success=True``
   * 200 + ``errors`` (with or
     without partial ``data``)        → ``success=False``, body preserved
   * non-200, transport exception,
     malformed JSON, missing auth     → ``success=False``, error_message set
   * input validation failure        → ``success=False``, no API call
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from graphql import GraphQLError, parse
from graphql.language.ast import OperationDefinitionNode

from river_gang.tracker.errors import (
    LinearApiRequest,
    LinearApiStatus,
    LinearGraphQLErrors,
    LinearUnknownPayload,
    MissingTrackerApiKey,
)


class LinearGraphqlInvalidInput(ValueError):  # noqa: N818 -- spec-defined
    """The agent-supplied tool input is malformed or violates §10.5."""


def validate_input(raw: Any) -> tuple[str, dict[str, Any]]:
    """Return ``(query, variables)`` after validating ``raw``.

    Raises:
        LinearGraphqlInvalidInput: malformed envelope, empty query, non-dict
            variables, malformed GraphQL, or document that contains zero or
            more-than-one operation.
    """
    query, variables = _normalise_envelope(raw)

    if not isinstance(query, str) or query.strip() == "":
        raise LinearGraphqlInvalidInput(
            "linear_graphql: 'query' must be a non-empty GraphQL document"
        )
    if variables is None:
        variables = {}
    if not isinstance(variables, dict):
        raise LinearGraphqlInvalidInput(
            f"linear_graphql: 'variables' must be an object/dict, "
            f"got {type(variables).__name__}"
        )

    op_count = count_operations(query)
    if op_count != 1:
        raise LinearGraphqlInvalidInput(
            f"linear_graphql: document must contain exactly one operation, "
            f"found {op_count}"
        )

    return query, variables


def count_operations(document: str) -> int:
    """Count ``OperationDefinition`` nodes in ``document`` via :mod:`graphql`.

    Fragment-only documents return 0. Invalid GraphQL raises
    :class:`LinearGraphqlInvalidInput`.
    """
    if not isinstance(document, str) or document.strip() == "":
        raise LinearGraphqlInvalidInput(
            "linear_graphql: cannot parse empty document"
        )
    try:
        parsed = parse(document)
    except GraphQLError as exc:
        raise LinearGraphqlInvalidInput(
            f"linear_graphql: invalid GraphQL syntax: {exc}"
        ) from exc

    return sum(
        1 for d in parsed.definitions if isinstance(d, OperationDefinitionNode)
    )


def _normalise_envelope(raw: Any) -> tuple[Any, Any]:
    """Resolve the two accepted shapes into ``(query, variables)``.

    Variables may legitimately be missing (defaults to ``{}`` upstream) or
    explicitly ``None``; both are passed through unchanged here so the
    caller can apply the default after the type checks run.
    """
    if isinstance(raw, str):
        return raw, {}
    if isinstance(raw, dict):
        return raw.get("query"), raw.get("variables", {})
    raise LinearGraphqlInvalidInput(
        f"linear_graphql: input must be a string or object, "
        f"got {type(raw).__name__}"
    )


@dataclass(frozen=True)
class ToolResult:
    """Outcome of a single :meth:`LinearGraphqlTool.execute` call.

    ``data`` and ``errors`` together preserve whatever GraphQL body the
    server returned (including partial data on error responses per §10.5).
    ``error_message`` is a short human-readable summary surfaced inside the
    DynamicToolCallResponse content text written back to the agent.
    """

    success: bool
    data: dict[str, Any] | None
    errors: list[dict[str, Any]] | None
    error_message: str | None


class _TransportLike(Protocol):
    """Subset of :class:`LinearTransport` the tool uses."""

    async def execute(
        self, query: str, variables: dict[str, Any]
    ) -> dict[str, Any]: ...


class LinearGraphqlTool:
    """Wraps a configured :class:`LinearTransport` to expose §10.5 semantics.

    The transport carries the auth/endpoint config from
    :func:`river_gang.config.resolution.resolve_and_validate`; the tool
    layer never reads tokens itself.
    """

    def __init__(self, *, transport: _TransportLike) -> None:
        self._transport = transport

    @property
    def transport(self) -> _TransportLike:
        return self._transport

    async def execute(self, raw_input: Any) -> ToolResult:
        try:
            query, variables = validate_input(raw_input)
        except LinearGraphqlInvalidInput as exc:
            return ToolResult(
                success=False, data=None, errors=None, error_message=str(exc)
            )

        try:
            data = await self._transport.execute(query, variables)
        except LinearGraphQLErrors as exc:
            return ToolResult(
                success=False,
                data=exc.data,
                errors=list(exc.errors),
                error_message=str(exc),
            )
        except (
            LinearApiStatus,
            LinearApiRequest,
            LinearUnknownPayload,
            MissingTrackerApiKey,
        ) as exc:
            return ToolResult(
                success=False, data=None, errors=None, error_message=str(exc)
            )

        return ToolResult(
            success=True, data=data, errors=None, error_message=None
        )


__all__ = [
    "LinearGraphqlInvalidInput",
    "LinearGraphqlTool",
    "ToolResult",
    "count_operations",
    "validate_input",
]
