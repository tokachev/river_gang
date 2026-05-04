"""Tests for ``apply_defaults`` (SPED §6.4 cheat sheet)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from river_gang.config.defaults import (
    DEFAULT_ACTIVE_STATES,
    DEFAULT_START_STATE,
    DEFAULT_SUCCESS_STATE,
    DEFAULT_TERMINAL_STATES,
    ConfigCoercionError,
    apply_defaults,
)
from river_gang.config.schema import EffectiveConfig


def test_apply_defaults_empty_dict_returns_full_defaults() -> None:
    cfg = apply_defaults({})

    assert isinstance(cfg, EffectiveConfig)
    # tracker defaults
    assert cfg.tracker.kind is None  # required, but default-pass at this stage
    assert cfg.tracker.endpoint == "https://api.linear.app/graphql"
    assert cfg.tracker.api_key is None
    assert cfg.tracker.project_slug is None
    assert cfg.tracker.active_states == DEFAULT_ACTIVE_STATES
    assert cfg.tracker.terminal_states == DEFAULT_TERMINAL_STATES
    assert cfg.tracker.start_state == DEFAULT_START_STATE
    assert cfg.tracker.success_state == DEFAULT_SUCCESS_STATE

    # polling defaults
    assert cfg.polling.interval_ms == 30000

    # workspace default — raw string, NOT yet absolutized (Task 5 owns that)
    assert cfg.workspace.root == str(Path(tempfile.gettempdir()) / "symphony_workspaces")
    assert cfg.workspace.repository is None

    # hooks defaults
    assert cfg.hooks.after_create is None
    assert cfg.hooks.before_run is None
    assert cfg.hooks.after_run is None
    assert cfg.hooks.before_remove is None
    assert cfg.hooks.timeout_ms == 60000

    # agent defaults
    assert cfg.agent.max_concurrent_agents == 10
    assert cfg.agent.max_turns == 20
    assert cfg.agent.max_retry_backoff_ms == 300000
    assert cfg.agent.max_concurrent_agents_by_state == {}

    # codex defaults
    assert cfg.codex.command == "codex app-server"
    assert cfg.codex.approval_policy is None
    assert cfg.codex.thread_sandbox is None
    assert cfg.codex.turn_sandbox_policy is None
    assert cfg.codex.turn_timeout_ms == 3600000
    assert cfg.codex.read_timeout_ms == 5000
    assert cfg.codex.stall_timeout_ms == 300000


def test_default_active_terminal_states_match_spec() -> None:
    assert DEFAULT_ACTIVE_STATES == ("Todo", "In Progress")
    assert DEFAULT_TERMINAL_STATES == (
        "Closed",
        "Cancelled",
        "Canceled",
        "Duplicate",
        "Done",
    )


def test_apply_defaults_overrides_take_effect() -> None:
    raw = {
        "tracker": {
            "kind": "linear",
            "endpoint": "https://example.test/graphql",
            "api_key": "$LINEAR_API_KEY",
            "project_slug": "river-gang",
            "active_states": ["Todo", "Doing"],
            "terminal_states": ["Done"],
        },
        "polling": {"interval_ms": 5000},
        "workspace": {
            "root": "~/ws",
            "repository": "https://github.com/tokachev/river_gang.git",
        },
        "hooks": {
            "after_create": "echo create",
            "before_run": "echo before",
            "after_run": "echo after",
            "before_remove": "echo remove",
            "timeout_ms": 1234,
        },
        "agent": {
            "max_concurrent_agents": 3,
            "max_turns": 7,
            "max_retry_backoff_ms": 999,
            "max_concurrent_agents_by_state": {"Todo": 2, "In Progress": 1},
        },
        "codex": {
            "command": "my-codex run",
            "approval_policy": "never",
            "thread_sandbox": "workspace-write",
            "turn_sandbox_policy": "workspace-write",
            "turn_timeout_ms": 100,
            "read_timeout_ms": 200,
            "stall_timeout_ms": 300,
        },
    }
    cfg = apply_defaults(raw)

    assert cfg.tracker.kind == "linear"
    assert cfg.tracker.endpoint == "https://example.test/graphql"
    assert cfg.tracker.api_key == "$LINEAR_API_KEY"
    assert cfg.tracker.project_slug == "river-gang"
    assert cfg.tracker.active_states == ("Todo", "Doing")
    assert cfg.tracker.terminal_states == ("Done",)
    assert cfg.polling.interval_ms == 5000
    assert cfg.workspace.root == "~/ws"
    assert cfg.workspace.repository == "https://github.com/tokachev/river_gang.git"
    assert cfg.hooks.after_create == "echo create"
    assert cfg.hooks.timeout_ms == 1234
    assert cfg.agent.max_concurrent_agents == 3
    assert cfg.agent.max_turns == 7
    assert cfg.agent.max_retry_backoff_ms == 999
    # per-state map: keys lowercased
    assert cfg.agent.max_concurrent_agents_by_state == {"todo": 2, "in progress": 1}
    assert cfg.codex.command == "my-codex run"
    assert cfg.codex.approval_policy == "never"
    assert cfg.codex.thread_sandbox == "workspace-write"
    assert cfg.codex.turn_sandbox_policy == "workspace-write"
    assert cfg.codex.turn_timeout_ms == 100
    assert cfg.codex.read_timeout_ms == 200
    assert cfg.codex.stall_timeout_ms == 300


def test_max_concurrent_agents_by_state_drops_invalid_entries() -> None:
    raw = {
        "agent": {
            "max_concurrent_agents_by_state": {
                "Todo": 4,
                "In Progress": 0,        # non-positive — drop
                "Review": -1,            # non-positive — drop
                "Cancelled": "two",      # non-numeric — drop
                "Done": 1.5,             # non-int float — drop
                "Closed": True,          # bool — drop (bool is int subclass, but not allowed)
            }
        }
    }
    cfg = apply_defaults(raw)
    assert cfg.agent.max_concurrent_agents_by_state == {"todo": 4}


def test_max_concurrent_agents_by_state_lowercases_keys() -> None:
    raw = {"agent": {"max_concurrent_agents_by_state": {"In Progress": 3}}}
    cfg = apply_defaults(raw)
    assert cfg.agent.max_concurrent_agents_by_state == {"in progress": 3}


def test_unknown_top_level_keys_are_ignored() -> None:
    raw = {
        "tracker": {"kind": "linear"},
        "future_extension_block": {"foo": "bar"},
        "another_unknown": 42,
    }
    cfg = apply_defaults(raw)
    assert cfg.tracker.kind == "linear"
    # accessing should not raise; only known fields exposed
    assert not hasattr(cfg, "future_extension_block")
    assert not hasattr(cfg, "another_unknown")


def test_unknown_nested_keys_are_ignored() -> None:
    raw = {
        "tracker": {"kind": "linear", "secret_extension": "yes"},
        "agent": {"max_turns": 5, "future_field": True},
    }
    cfg = apply_defaults(raw)
    assert cfg.tracker.kind == "linear"
    assert cfg.agent.max_turns == 5


@pytest.mark.parametrize(
    ("raw", "field_path", "expected"),
    [
        ({"polling": {"interval_ms": "5000"}}, "polling.interval_ms", 5000),
        ({"hooks": {"timeout_ms": "1500"}}, "hooks.timeout_ms", 1500),
        ({"agent": {"max_turns": "7"}}, "agent.max_turns", 7),
        ({"agent": {"max_concurrent_agents": "3"}}, "agent.max_concurrent_agents", 3),
        (
            {"agent": {"max_retry_backoff_ms": "200"}},
            "agent.max_retry_backoff_ms",
            200,
        ),
        ({"codex": {"turn_timeout_ms": "100"}}, "codex.turn_timeout_ms", 100),
        ({"codex": {"read_timeout_ms": "200"}}, "codex.read_timeout_ms", 200),
        ({"codex": {"stall_timeout_ms": "300"}}, "codex.stall_timeout_ms", 300),
    ],
)
def test_string_to_int_coercion(
    raw: dict[str, object], field_path: str, expected: int
) -> None:
    cfg = apply_defaults(raw)
    obj: object = cfg
    for part in field_path.split("."):
        obj = getattr(obj, part)
    assert obj == expected
    assert isinstance(obj, int)
    assert not isinstance(obj, bool)


@pytest.mark.parametrize(
    "raw",
    [
        {"polling": {"interval_ms": "not-a-number"}},
        {"agent": {"max_turns": "abc"}},
        {"hooks": {"timeout_ms": "12.5x"}},
        {"codex": {"turn_timeout_ms": [1, 2]}},
    ],
)
def test_non_numeric_coercion_raises(raw: dict[str, object]) -> None:
    with pytest.raises(ConfigCoercionError):
        apply_defaults(raw)


def test_bool_is_not_accepted_for_int_field() -> None:
    """``True``/``False`` must not silently coerce to 1/0 for numeric fields."""
    with pytest.raises(ConfigCoercionError):
        apply_defaults({"polling": {"interval_ms": True}})


def test_active_states_accept_tuple_and_list() -> None:
    cfg = apply_defaults({"tracker": {"active_states": ["A", "B"]}})
    assert cfg.tracker.active_states == ("A", "B")


def test_active_states_non_list_raises_coercion_error() -> None:
    with pytest.raises(ConfigCoercionError):
        apply_defaults({"tracker": {"active_states": "Todo"}})


def test_active_states_non_string_entries_raise_coercion_error() -> None:
    with pytest.raises(ConfigCoercionError):
        apply_defaults({"tracker": {"active_states": ["Todo", 5]}})


def test_start_and_success_state_overrides() -> None:
    cfg = apply_defaults(
        {
            "tracker": {
                "start_state": "Doing",
                "success_state": "Needs Review",
            }
        }
    )
    assert cfg.tracker.start_state == "Doing"
    assert cfg.tracker.success_state == "Needs Review"


def test_start_and_success_state_explicit_null_disables_transition() -> None:
    cfg = apply_defaults(
        {"tracker": {"start_state": None, "success_state": None}}
    )
    assert cfg.tracker.start_state is None
    assert cfg.tracker.success_state is None


def test_start_and_success_state_empty_string_disables_transition() -> None:
    cfg = apply_defaults(
        {"tracker": {"start_state": "", "success_state": ""}}
    )
    assert cfg.tracker.start_state is None
    assert cfg.tracker.success_state is None


def test_apply_defaults_does_not_mutate_input() -> None:
    raw = {
        "tracker": {"kind": "linear", "active_states": ["A", "B"]},
        "agent": {"max_concurrent_agents_by_state": {"Todo": 2}},
    }
    snapshot = {
        "tracker_kind": raw["tracker"]["kind"],
        "active_states": list(raw["tracker"]["active_states"]),
        "by_state": dict(raw["agent"]["max_concurrent_agents_by_state"]),
    }
    apply_defaults(raw)
    assert raw["tracker"]["kind"] == snapshot["tracker_kind"]
    assert raw["tracker"]["active_states"] == snapshot["active_states"]
    assert raw["agent"]["max_concurrent_agents_by_state"] == snapshot["by_state"]


def test_subsection_must_be_a_map() -> None:
    with pytest.raises(ConfigCoercionError):
        apply_defaults({"tracker": "linear"})


def test_hook_value_must_be_string_or_null() -> None:
    with pytest.raises(ConfigCoercionError):
        apply_defaults({"hooks": {"after_create": 42}})


def test_codex_command_must_be_string() -> None:
    with pytest.raises(ConfigCoercionError):
        apply_defaults({"codex": {"command": 123}})
