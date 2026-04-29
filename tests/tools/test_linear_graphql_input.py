"""Tests for ``linear_graphql`` tool input validation (SPED §10.5)."""

from __future__ import annotations

import pytest

from river_gang.tools.linear_graphql import (
    LinearGraphqlInvalidInput,
    count_operations,
    validate_input,
)

# ---------------------------------------------------------------------------
# validate_input — preferred shape
# ---------------------------------------------------------------------------


def test_validate_preferred_shape_with_variables() -> None:
    raw = {"query": "query Viewer { viewer { id } }", "variables": {"x": 1}}
    query, variables = validate_input(raw)
    assert query == "query Viewer { viewer { id } }"
    assert variables == {"x": 1}


def test_validate_preferred_shape_missing_variables_defaults_empty() -> None:
    raw = {"query": "query Viewer { viewer { id } }"}
    query, variables = validate_input(raw)
    assert query == "query Viewer { viewer { id } }"
    assert variables == {}


def test_validate_variables_explicit_empty_dict() -> None:
    raw = {"query": "{ viewer { id } }", "variables": {}}
    _, variables = validate_input(raw)
    assert variables == {}


def test_validate_variables_complex_nested() -> None:
    raw = {
        "query": "query Q($a: Int!) { x }",
        "variables": {"a": 1, "nested": {"k": [1, 2, 3]}},
    }
    _, variables = validate_input(raw)
    assert variables == {"a": 1, "nested": {"k": [1, 2, 3]}}


# ---------------------------------------------------------------------------
# validate_input — raw string shorthand
# ---------------------------------------------------------------------------


def test_validate_raw_string_shorthand() -> None:
    """Raw string ⇒ ``{query: <s>, variables: {}}``."""
    query, variables = validate_input("{ viewer { id } }")
    assert query == "{ viewer { id } }"
    assert variables == {}


def test_validate_raw_string_named_query_shorthand() -> None:
    query, _ = validate_input("query Viewer { viewer { id } }")
    assert query == "query Viewer { viewer { id } }"


def test_validate_raw_string_mutation_shorthand() -> None:
    query, _ = validate_input("mutation M { ok }")
    assert query == "mutation M { ok }"


# ---------------------------------------------------------------------------
# validate_input — rejection paths
# ---------------------------------------------------------------------------


def test_validate_empty_query_rejected() -> None:
    with pytest.raises(LinearGraphqlInvalidInput) as exc:
        validate_input({"query": ""})
    assert "query" in str(exc.value).lower()


def test_validate_whitespace_only_query_rejected() -> None:
    with pytest.raises(LinearGraphqlInvalidInput):
        validate_input({"query": "   \n\t "})


def test_validate_empty_string_shorthand_rejected() -> None:
    with pytest.raises(LinearGraphqlInvalidInput):
        validate_input("")


def test_validate_query_not_string_rejected() -> None:
    with pytest.raises(LinearGraphqlInvalidInput):
        validate_input({"query": 123})


def test_validate_query_missing_rejected() -> None:
    with pytest.raises(LinearGraphqlInvalidInput) as exc:
        validate_input({"variables": {}})
    assert "query" in str(exc.value).lower()


def test_validate_variables_non_dict_rejected_list() -> None:
    with pytest.raises(LinearGraphqlInvalidInput) as exc:
        validate_input({"query": "{ viewer { id } }", "variables": [1, 2]})
    assert "variables" in str(exc.value).lower()


def test_validate_variables_non_dict_rejected_string() -> None:
    with pytest.raises(LinearGraphqlInvalidInput):
        validate_input({"query": "{ viewer { id } }", "variables": "x"})


def test_validate_variables_non_dict_rejected_int() -> None:
    with pytest.raises(LinearGraphqlInvalidInput):
        validate_input({"query": "{ viewer { id } }", "variables": 42})


def test_validate_variables_explicit_none_treated_as_empty() -> None:
    """Explicit ``None`` for variables is benign — treat as empty map."""
    _, variables = validate_input({"query": "{ x }", "variables": None})
    assert variables == {}


def test_validate_input_top_level_non_dict_non_string_rejected() -> None:
    with pytest.raises(LinearGraphqlInvalidInput):
        validate_input([1, 2, 3])  # type: ignore[arg-type]


def test_validate_input_top_level_none_rejected() -> None:
    with pytest.raises(LinearGraphqlInvalidInput):
        validate_input(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# validate_input — operation count constraint
# ---------------------------------------------------------------------------


def test_validate_single_query_accepted() -> None:
    validate_input({"query": "query A { foo }"})


def test_validate_single_mutation_accepted() -> None:
    validate_input({"query": "mutation M { ok }"})


def test_validate_anonymous_single_operation_accepted() -> None:
    validate_input({"query": "{ viewer { id } }"})


def test_validate_multiple_operations_rejected() -> None:
    with pytest.raises(LinearGraphqlInvalidInput) as exc:
        validate_input({"query": "query A { foo } mutation B { bar }"})
    msg = str(exc.value)
    assert "2" in msg or "multiple" in msg.lower() or "operation" in msg.lower()


def test_validate_three_operations_rejected_count_in_message() -> None:
    """The error message should mention the operation count for debuggability."""
    doc = "query A { x } query B { y } mutation C { z }"
    with pytest.raises(LinearGraphqlInvalidInput) as exc:
        validate_input({"query": doc})
    assert "3" in str(exc.value)


def test_validate_fragment_only_document_rejected() -> None:
    """A fragment without an operation has zero operations — not callable."""
    with pytest.raises(LinearGraphqlInvalidInput):
        validate_input({"query": "fragment F on User { id }"})


def test_validate_invalid_graphql_rejected() -> None:
    with pytest.raises(LinearGraphqlInvalidInput) as exc:
        validate_input({"query": "this is not graphql {"})
    assert "graphql" in str(exc.value).lower() or "syntax" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# count_operations
# ---------------------------------------------------------------------------


def test_count_operations_named_query() -> None:
    assert count_operations("query A { foo }") == 1


def test_count_operations_named_mutation() -> None:
    assert count_operations("mutation M { ok }") == 1


def test_count_operations_anonymous_query() -> None:
    assert count_operations("{ foo }") == 1


def test_count_operations_multiple_named() -> None:
    assert count_operations("query A { x } mutation B { y }") == 2


def test_count_operations_three_operations() -> None:
    assert count_operations(
        "query A { a } query B { b } mutation C { c }"
    ) == 3


def test_count_operations_fragment_only_returns_zero() -> None:
    assert count_operations("fragment F on User { id name }") == 0


def test_count_operations_query_with_fragment_counts_only_operation() -> None:
    """A document with one operation + one fragment counts as one operation."""
    doc = """
    query Viewer { viewer { ...F } }
    fragment F on User { id name }
    """
    assert count_operations(doc) == 1


def test_count_operations_string_literal_with_braces_does_not_inflate() -> None:
    """Why we use the AST instead of regex — string literals like
    ``"query: '{...}'"`` would fool a naive ``\\bquery\\b`` matcher."""
    doc = '{ search(filter: "query A { x }") { id } }'
    assert count_operations(doc) == 1


def test_count_operations_invalid_graphql_raises() -> None:
    with pytest.raises(LinearGraphqlInvalidInput):
        count_operations("not valid {")


def test_count_operations_empty_document_raises() -> None:
    """Empty input has no AST to parse — surface as invalid input."""
    with pytest.raises(LinearGraphqlInvalidInput):
        count_operations("")
