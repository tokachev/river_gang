"""Raw GraphQL strings for the Linear tracker (SPED §11.2).

Kept isolated per spec note: "Linear GraphQL schema details can drift. Keep
query construction isolated and test the exact query fields/types REQUIRED
by this specification."

Server-side filters do the heavy lifting:

- ``project: { slugId: { eq: $projectSlug } }`` — scope to one project.
- ``state: { name: { in: $activeStates } }`` — exclude terminal/inactive
  states at the database level so pagination never wastes round-trips on
  issues we'd just throw away client-side.
"""

from __future__ import annotations

CANDIDATES_PAGE_SIZE: int = 50
STATE_REFRESH_PAGE_SIZE: int = 50
TERMINAL_FETCH_PAGE_SIZE: int = 50

CANDIDATES_QUERY: str = """
query Candidates($projectSlug: String!, $activeStates: [String!]!, $first: Int!, $after: String) {
  issues(
    first: $first
    after: $after
    filter: {
      project: { slugId: { eq: $projectSlug } }
      state: { name: { in: $activeStates } }
    }
  ) {
    nodes {
      id
      identifier
      title
      description
      priority
      state { name }
      branchName
      url
      labels { nodes { name } }
      inverseRelations {
        nodes {
          type
          issue {
            id
            identifier
            state { name }
          }
        }
      }
      createdAt
      updatedAt
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
""".strip()


# State refresh: minimal projection per §17.3 ("returns minimal normalized
# issues"). Variable type ``[ID!]`` is mandated by §11.2.
STATE_REFRESH_QUERY: str = """
query StateRefresh($issueIds: [ID!]!, $first: Int!, $after: String) {
  issues(
    first: $first
    after: $after
    filter: {
      id: { in: $issueIds }
    }
  ) {
    nodes {
      id
      identifier
      title
      state { name }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
""".strip()


# Terminal fetch: same projection as Candidates (full §4.1.1 fields) but
# filters by an explicit list of state names. Used by startup terminal
# workspace cleanup (§8.6).
TERMINAL_FETCH_QUERY: str = """
query TerminalFetch($projectSlug: String!, $stateNames: [String!]!, $first: Int!, $after: String) {
  issues(
    first: $first
    after: $after
    filter: {
      project: { slugId: { eq: $projectSlug } }
      state: { name: { in: $stateNames } }
    }
  ) {
    nodes {
      id
      identifier
      title
      description
      priority
      state { name }
      branchName
      url
      labels { nodes { name } }
      inverseRelations {
        nodes {
          type
          issue {
            id
            identifier
            state { name }
          }
        }
      }
      createdAt
      updatedAt
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
""".strip()
