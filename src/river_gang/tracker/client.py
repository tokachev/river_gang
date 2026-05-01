"""High-level Linear client (SPED §11).

Wraps :class:`LinearTransport` to expose normalized domain operations:

- :meth:`LinearClient.fetch_candidate_issues` — eligible issues for the
  configured project (§8.1, §17.3).
- :meth:`LinearClient.fetch_issue_states_by_ids` — minimal refresh used by
  active-run reconciliation (§8.5 part B).
- :meth:`LinearClient.fetch_issues_by_states` — terminal-state listing used
  by startup terminal workspace cleanup (§8.6).
- :meth:`LinearClient.transition_state` / :meth:`LinearClient.add_comment` —
  orchestrator-side ticket mutations driving the §16.5 lifecycle (start
  state on dispatch, success state on worker exit, failure comment).

All three read methods paginate over the Linear ``IssueConnection`` shape
(``nodes`` + ``pageInfo.{hasNextPage,endCursor}``). Pagination integrity
errors raise :class:`LinearMissingEndCursor`.

No retry policy here; the orchestrator owns retry/backoff (§11.4).
"""

from __future__ import annotations

import asyncio
from typing import Any

from river_gang.tracker.errors import (
    LinearMissingEndCursor,
    LinearStateNotFound,
    LinearUnknownPayload,
    MissingTrackerProjectSlug,
)
from river_gang.tracker.issue import Issue, parse_issue
from river_gang.tracker.linear_transport import LinearTransport
from river_gang.tracker.queries import (
    CANDIDATES_PAGE_SIZE,
    CANDIDATES_QUERY,
    COMMENT_CREATE_MUTATION,
    ISSUE_UPDATE_STATE_MUTATION,
    STATE_REFRESH_PAGE_SIZE,
    STATE_REFRESH_QUERY,
    STATES_FOR_ISSUE_QUERY,
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
        # team_id → {state_name_lower → state_id}. Populated lazily on the
        # first ``transition_state`` call per team. The lock serialises the
        # populate path so concurrent transitions don't issue duplicate
        # ``StatesForIssue`` queries against the same team.
        self._state_id_cache: dict[str, dict[str, str]] = {}
        self._state_cache_lock = asyncio.Lock()

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
    # Mutations
    # ------------------------------------------------------------------

    async def transition_state(self, issue_id: str, state_name: str) -> None:
        """Move ``issue_id`` to the workflow state named ``state_name``.

        Raises:
            LinearStateNotFound: ``state_name`` is not on the issue's team.
            LinearUnknownPayload: server reported ``issueUpdate.success: false``
                or returned an unexpected envelope shape.
            LinearError: any transport / GraphQL error from
                :meth:`LinearTransport.execute` propagates verbatim.
        """
        state_id = await self._resolve_state_id(issue_id, state_name)
        data = await self._transport.execute(
            ISSUE_UPDATE_STATE_MUTATION,
            {"id": issue_id, "stateId": state_id},
        )
        result = data.get("issueUpdate")
        if not isinstance(result, dict):
            raise LinearUnknownPayload(
                "issueUpdate response missing 'issueUpdate' object"
            )
        if not bool(result.get("success")):
            raise LinearUnknownPayload(
                f"issueUpdate returned success=false for issue {issue_id!r}"
            )

    async def add_comment(self, issue_id: str, body: str) -> None:
        """Post a comment with body ``body`` on ``issue_id``.

        Raises:
            LinearUnknownPayload: server reported ``commentCreate.success:
                false`` or returned an unexpected envelope shape.
            LinearError: any transport / GraphQL error propagates verbatim.
        """
        data = await self._transport.execute(
            COMMENT_CREATE_MUTATION,
            {"issueId": issue_id, "body": body},
        )
        result = data.get("commentCreate")
        if not isinstance(result, dict):
            raise LinearUnknownPayload(
                "commentCreate response missing 'commentCreate' object"
            )
        if not bool(result.get("success")):
            raise LinearUnknownPayload(
                f"commentCreate returned success=false for issue {issue_id!r}"
            )

    async def _resolve_state_id(self, issue_id: str, state_name: str) -> str:
        """Return the workflow ``stateId`` for ``state_name`` on the issue's team.

        Cache layout: ``{team_id: {state_name_lower: state_id}}``. We don't key
        the cache by issue id because all issues on the same team share a
        workflow state set; one ``StatesForIssue`` query per team suffices for
        the lifetime of the client.

        Raises:
            LinearStateNotFound: the state isn't defined on the team.
            LinearUnknownPayload: server returned an unexpected shape (no
                ``team`` block, missing ``id``, malformed ``states.nodes``).
        """
        target = state_name.lower()
        # Fast path: scan the existing cache for any team that has this name.
        for team_states in self._state_id_cache.values():
            cached = team_states.get(target)
            if cached is not None:
                return cached

        async with self._state_cache_lock:
            # Re-check inside the lock — another caller may have populated us
            # while we awaited the lock.
            for team_states in self._state_id_cache.values():
                cached = team_states.get(target)
                if cached is not None:
                    return cached

            data = await self._transport.execute(
                STATES_FOR_ISSUE_QUERY, {"id": issue_id}
            )
            team_id, name_to_id = _parse_states_for_issue(data)
            self._state_id_cache[team_id] = name_to_id

        resolved = name_to_id.get(target)
        if resolved is None:
            raise LinearStateNotFound(state_name, team_id=team_id)
        return resolved

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


def _parse_states_for_issue(
    data: dict[str, Any],
) -> tuple[str, dict[str, str]]:
    """Decode a ``StatesForIssue`` response into ``(team_id, name_lower→id)``.

    Raises :class:`LinearUnknownPayload` on any envelope-level surprise
    (missing ``issue``, missing ``team``, non-string ``team.id``, malformed
    ``states.nodes``). Empty state lists are tolerated and produce an empty
    name map — the caller will surface ``LinearStateNotFound`` from the
    failed lookup, which is more informative than rejecting the team here.
    """
    issue = data.get("issue")
    if not isinstance(issue, dict):
        raise LinearUnknownPayload(
            "StatesForIssue response missing 'issue' object"
        )
    team = issue.get("team")
    if not isinstance(team, dict):
        raise LinearUnknownPayload(
            "StatesForIssue response missing 'issue.team' object"
        )
    team_id = team.get("id")
    if not isinstance(team_id, str) or team_id == "":
        raise LinearUnknownPayload(
            "StatesForIssue response missing 'issue.team.id'"
        )
    states_obj = team.get("states")
    if not isinstance(states_obj, dict):
        raise LinearUnknownPayload(
            "StatesForIssue response missing 'issue.team.states' object"
        )
    nodes = states_obj.get("nodes")
    if not isinstance(nodes, list):
        raise LinearUnknownPayload(
            "StatesForIssue response 'states.nodes' must be a list"
        )

    name_to_id: dict[str, str] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = node.get("id")
        node_name = node.get("name")
        if not isinstance(node_id, str) or node_id == "":
            continue
        if not isinstance(node_name, str) or node_name == "":
            continue
        name_to_id[node_name.lower()] = node_id
    return team_id, name_to_id
