"""In-memory fake tracker for orchestrator integration tests (Task 24a).

Duck-typed double for :class:`river_gang.tracker.client.LinearClient`. No
inheritance — orchestrator code accepts the tracker via a structural
:class:`Protocol`, so this fake stays independent of the real client's
implementation details.

Configuration knobs (all optional):

- ``candidates``: list of :class:`Issue` returned by
  :meth:`fetch_candidate_issues`.
- ``state_refreshes``: ``{issue_id: state_name}`` map driving
  :meth:`fetch_issue_states_by_ids`. Unknown ids are silently omitted to
  match SPED §17.3 ("Linear server may return fewer issues than requested").
- ``terminal_by_state``: ``{state_name: list[Issue]}`` map driving
  :meth:`fetch_issues_by_states`.

Failure injection: ``fail_next_*`` methods queue a one-shot exception that
fires on the next matching call; subsequent calls revert to normal
behaviour. The call is still recorded BEFORE the exception is raised so
``.calls`` ordering assertions still hold.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from river_gang.tracker.errors import LinearError
from river_gang.tracker.issue import Issue


class FakeTracker:
    def __init__(
        self,
        candidates: Iterable[Issue] = (),
        *,
        state_refreshes: dict[str, str] | None = None,
        terminal_by_state: dict[str, list[Issue]] | None = None,
    ) -> None:
        self._candidates: list[Issue] = list(candidates)
        self._state_refreshes: dict[str, str] = dict(state_refreshes or {})
        self._terminal_by_state: dict[str, list[Issue]] = {
            k: list(v) for k, v in (terminal_by_state or {}).items()
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # Mutation observation surfaces. Tests assert ordering against these
        # lists and on the ``calls`` log; both are populated even when the
        # one-shot failure injection fires (so call-ordering invariants
        # survive the injection).
        self.transitions: list[tuple[str, str]] = []
        self.comments: list[tuple[str, str]] = []
        self._next_candidates_error: LinearError | None = None
        self._next_state_refresh_error: LinearError | None = None
        self._next_terminal_error: LinearError | None = None
        self._next_transition_error: LinearError | None = None
        self._next_comment_error: LinearError | None = None

    # ------------------------------------------------------------------
    # Configuration mutators
    # ------------------------------------------------------------------

    def set_candidates(self, candidates: Iterable[Issue]) -> None:
        self._candidates = list(candidates)

    def set_state_refreshes(self, refreshes: dict[str, str]) -> None:
        self._state_refreshes = dict(refreshes)

    def set_terminal_by_state(
        self, terminal: dict[str, list[Issue]]
    ) -> None:
        self._terminal_by_state = {k: list(v) for k, v in terminal.items()}

    def fail_next_candidates(self, error: LinearError) -> None:
        self._next_candidates_error = error

    def fail_next_state_refreshes(self, error: LinearError) -> None:
        self._next_state_refresh_error = error

    def fail_next_terminal(self, error: LinearError) -> None:
        self._next_terminal_error = error

    def fail_next_transition(self, error: BaseException) -> None:
        self._next_transition_error = error  # type: ignore[assignment]

    def fail_next_comment(self, error: BaseException) -> None:
        self._next_comment_error = error  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # LinearClient surface
    # ------------------------------------------------------------------

    async def fetch_candidate_issues(
        self, active_states: list[str]
    ) -> list[Issue]:
        self.calls.append(
            ("fetch_candidate_issues", {"active_states": list(active_states)})
        )
        if self._next_candidates_error is not None:
            err = self._next_candidates_error
            self._next_candidates_error = None
            raise err
        if not active_states:
            return []
        return list(self._candidates)

    async def fetch_issue_states_by_ids(
        self, issue_ids: list[str]
    ) -> list[Issue]:
        self.calls.append(
            ("fetch_issue_states_by_ids", {"issue_ids": list(issue_ids)})
        )
        if self._next_state_refresh_error is not None:
            err = self._next_state_refresh_error
            self._next_state_refresh_error = None
            raise err
        if not issue_ids:
            return []

        results: list[Issue] = []
        for issue_id in issue_ids:
            new_state = self._state_refreshes.get(issue_id)
            if new_state is None:
                continue
            results.append(_minimal_refresh_issue(issue_id, new_state))
        return results

    async def transition_state(
        self, issue_id: str, state_name: str
    ) -> None:
        self.calls.append(
            (
                "transition_state",
                {"issue_id": issue_id, "state_name": state_name},
            )
        )
        self.transitions.append((issue_id, state_name))
        if self._next_transition_error is not None:
            err = self._next_transition_error
            self._next_transition_error = None
            raise err

    async def add_comment(self, issue_id: str, body: str) -> None:
        self.calls.append(
            ("add_comment", {"issue_id": issue_id, "body": body})
        )
        self.comments.append((issue_id, body))
        if self._next_comment_error is not None:
            err = self._next_comment_error
            self._next_comment_error = None
            raise err

    async def fetch_issues_by_states(
        self, state_names: list[str]
    ) -> list[Issue]:
        self.calls.append(
            ("fetch_issues_by_states", {"state_names": list(state_names)})
        )
        if self._next_terminal_error is not None:
            err = self._next_terminal_error
            self._next_terminal_error = None
            raise err
        if not state_names:
            return []
        results: list[Issue] = []
        for state in state_names:
            results.extend(self._terminal_by_state.get(state, ()))
        return results


def _minimal_refresh_issue(issue_id: str, state: str) -> Issue:
    """Synthesise a minimal-projection issue (matches SPED §17.3 shape)."""
    return Issue(
        id=issue_id,
        identifier=issue_id,
        title=f"Title {issue_id}",
        state=state,
        description=None,
        priority=None,
        branch_name=None,
        url=None,
        labels=(),
        blocked_by=(),
        created_at=None,
        updated_at=None,
    )


# Re-export ``replace`` so test code can build modified Issue copies if
# the orchestrator tests want to compare states between fetches.
__all__ = ["FakeTracker", "replace"]
