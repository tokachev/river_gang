"""High-level Linear client (SPED §11).

Wraps :class:`LinearTransport` to expose normalized domain operations:

- :meth:`LinearClient.fetch_candidate_issues` — eligible issues for the
  configured project (§8.1, §17.3).
- :meth:`LinearClient.fetch_issue_states_by_ids` — minimal refresh used by
  active-run reconciliation (§8.5 part B).
- :meth:`LinearClient.fetch_issues_by_states` — terminal-state listing used
  by startup terminal workspace cleanup (§8.6).

All three methods paginate over the Linear ``IssueConnection`` shape
(``nodes`` + ``pageInfo.{hasNextPage,endCursor}``). Pagination integrity
errors raise :class:`LinearMissingEndCursor`.

No retry policy here; the orchestrator owns retry/backoff (§11.4).
"""

from __future__ import annotations

from typing import Any

from river_gang.tracker.errors import (
    LinearMissingEndCursor,
    LinearUnknownPayload,
    MissingTrackerProjectSlug,
)
from river_gang.tracker.issue import Issue, parse_issue
from river_gang.tracker.linear_transport import LinearTransport
from river_gang.tracker.queries import (
    CANDIDATES_PAGE_SIZE,
    CANDIDATES_QUERY,
    STATE_REFRESH_PAGE_SIZE,
    STATE_REFRESH_QUERY,
    TERMINAL_FETCH_PAGE_SIZE,
    TERMINAL_FETCH_QUERY,
)


class LinearClient:
    """Operations against a single Linear project.

    Args:
        transport: configured :class:`LinearTransport`.
        project_slug: Linear project ``slugId`` (REQUIRED, non-empty).
    """

    def __init__(
        self,
        *,
        transport: LinearTransport,
        project_slug: str,
    ) -> None:
        if not project_slug:
            raise MissingTrackerProjectSlug(
                "tracker.project_slug is required to construct LinearClient"
            )
        self._transport = transport
        self._project_slug = project_slug

    @property
    def project_slug(self) -> str:
        return self._project_slug

    async def fetch_candidate_issues(self, active_states: list[str]) -> list[Issue]:
        """Paginate through all candidate issues matching ``active_states``.

        Empty ``active_states`` short-circuits to ``[]`` without an API call
        (§17.2 conformance bullet).

        Raises:
            LinearMissingEndCursor: a page reported ``hasNextPage=true`` but
                omitted (or returned an empty) ``endCursor``.
        """
        if not active_states:
            return []

        return await self._paginate_issues(
            CANDIDATES_QUERY,
            base_variables={
                "projectSlug": self._project_slug,
                "activeStates": list(active_states),
            },
            page_size=CANDIDATES_PAGE_SIZE,
        )

    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        """Minimal-projection refresh for a known set of issue IDs (§8.5, §17.3).

        Empty ``issue_ids`` short-circuits to ``[]`` without an API call.

        The Linear server may return fewer rows than requested (issue deleted
        or out of scope). Callers infer absence by id-set diff.

        Raises:
            LinearMissingEndCursor: pagination integrity violation.
        """
        if not issue_ids:
            return []

        return await self._paginate_issues(
            STATE_REFRESH_QUERY,
            base_variables={"issueIds": list(issue_ids)},
            page_size=STATE_REFRESH_PAGE_SIZE,
        )

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        """Full-projection issue list filtered by an explicit state-name set.

        Used for startup terminal workspace cleanup (§8.6). Empty
        ``state_names`` short-circuits to ``[]`` without an API call
        (§17.3 conformance bullet).

        Raises:
            LinearMissingEndCursor: pagination integrity violation.
        """
        if not state_names:
            return []

        return await self._paginate_issues(
            TERMINAL_FETCH_QUERY,
            base_variables={
                "projectSlug": self._project_slug,
                "stateNames": list(state_names),
            },
            page_size=TERMINAL_FETCH_PAGE_SIZE,
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _paginate_issues(
        self,
        query: str,
        *,
        base_variables: dict[str, Any],
        page_size: int,
    ) -> list[Issue]:
        results: list[Issue] = []
        cursor: str | None = None

        while True:
            variables: dict[str, Any] = {
                **base_variables,
                "first": page_size,
                "after": cursor,
            }
            data = await self._transport.execute(query, variables)

            connection = _require_connection(data, "issues")
            for node in _require_nodes(connection):
                results.append(parse_issue(node))

            page_info = _require_page_info(connection)
            if not bool(page_info.get("hasNextPage")):
                return results

            end_cursor = page_info.get("endCursor")
            if not isinstance(end_cursor, str) or end_cursor == "":
                raise LinearMissingEndCursor(
                    "Linear returned hasNextPage=true with missing/empty endCursor"
                )
            cursor = end_cursor


# ---------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------


def _require_connection(data: dict[str, Any], key: str) -> dict[str, Any]:
    connection = data.get(key)
    if not isinstance(connection, dict):
        raise LinearUnknownPayload(
            f"GraphQL response missing {key!r} connection object"
        )
    return connection


def _require_nodes(connection: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = connection.get("nodes")
    if not isinstance(nodes, list):
        raise LinearUnknownPayload("connection 'nodes' must be a list")
    typed_nodes: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            raise LinearUnknownPayload(
                f"connection node must be an object, got {type(node).__name__}"
            )
        typed_nodes.append(node)
    return typed_nodes


def _require_page_info(connection: dict[str, Any]) -> dict[str, Any]:
    page_info = connection.get("pageInfo")
    if not isinstance(page_info, dict):
        raise LinearUnknownPayload("connection missing 'pageInfo' object")
    return page_info
