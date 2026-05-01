"""Typed exception hierarchy for the tracker layer (SPED §11.4).

Mirrors the error categories enumerated in §11.4 plus a few related sentinels
used by surrounding modules (selection, pagination, kind dispatch). Callers
distinguish transport vs. protocol vs. semantic failures by exception class —
no string sniffing.
"""

from __future__ import annotations

from typing import Any


class LinearError(Exception):
    """Base class for all tracker-layer failures."""


# ---------------------------------------------------------------------------
# Configuration / construction
# ---------------------------------------------------------------------------


class MissingTrackerApiKey(LinearError):  # noqa: N818 -- spec-defined name
    """``tracker.api_key`` was missing or empty when constructing the client."""


class MissingTrackerProjectSlug(LinearError):  # noqa: N818 -- spec-defined name
    """``tracker.project_slug`` is REQUIRED for ``tracker.kind=linear``."""


class UnsupportedTrackerKind(LinearError):  # noqa: N818 -- spec-defined name
    """A ``tracker.kind`` other than ``linear`` was requested."""


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


class IssueMissingRequiredField(LinearError):  # noqa: N818 -- spec-defined name
    """Issue payload omitted a §4.1.1 REQUIRED scalar.

    REQUIRED fields are ``id``, ``identifier``, ``title``, ``state``. Filtering
    at parse-time so downstream eligibility/dispatch logic can assume a fully
    populated :class:`river_gang.tracker.issue.Issue`.
    """


# ---------------------------------------------------------------------------
# Transport / protocol
# ---------------------------------------------------------------------------


class LinearApiRequest(LinearError):  # noqa: N818 -- spec-defined name
    """Transport-level failure (connect, read, timeout, DNS, ...)."""


class LinearApiStatus(LinearError):  # noqa: N818 -- spec-defined name
    """Non-200 HTTP status returned by the Linear endpoint."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Linear API returned HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


class LinearGraphQLErrors(LinearError):  # noqa: N818 -- spec-defined name
    """Response carried a non-empty top-level ``errors`` array.

    SPED §10.5 requires the ``linear_graphql`` tool to surface the GraphQL
    body (including any partial ``data``) alongside the ``errors`` list, so
    we keep the partial ``data`` on the exception for callers that need it.
    """

    def __init__(
        self,
        errors: list[dict[str, Any]],
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        first = errors[0].get("message", "<no message>") if errors else "<empty>"
        super().__init__(
            f"Linear GraphQL returned {len(errors)} error(s); first: {first}"
        )
        self.errors = errors
        self.data = data


class LinearUnknownPayload(LinearError):  # noqa: N818 -- spec-defined name
    """Response was 200 OK but the body did not match the GraphQL shape.

    Covers: malformed JSON, missing both ``data`` and ``errors`` keys,
    or ``data`` being a non-object value.
    """


# ---------------------------------------------------------------------------
# Pagination (used by issue listing in later tasks)
# ---------------------------------------------------------------------------


class LinearMissingEndCursor(LinearError):  # noqa: N818 -- spec-defined name
    """Connection page reported ``hasNextPage`` but omitted ``endCursor``."""


# ---------------------------------------------------------------------------
# Mutations / state transitions
# ---------------------------------------------------------------------------


class LinearStateNotFound(LinearError):  # noqa: N818 -- spec-defined name
    """Workflow state with the given name does not exist on the issue's team.

    Raised by :meth:`LinearClient.transition_state` when the resolver could
    not map the requested state name to a workflow ``stateId`` after fetching
    the team's full state list.
    """

    def __init__(self, state_name: str, *, team_id: str | None = None) -> None:
        if team_id is None:
            super().__init__(f"workflow state {state_name!r} not found on team")
        else:
            super().__init__(
                f"workflow state {state_name!r} not found on team {team_id}"
            )
        self.state_name = state_name
        self.team_id = team_id
