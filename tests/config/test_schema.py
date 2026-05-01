"""Tests for typed config dataclasses (SPED §5.3, §6.4)."""

from __future__ import annotations

import dataclasses

import pytest

from river_gang.config.schema import (
    AgentConfig,
    CodexConfig,
    EffectiveConfig,
    HooksConfig,
    PollingConfig,
    TrackerConfig,
    WorkspaceConfig,
)

_ALL_DATACLASSES = [
    TrackerConfig,
    PollingConfig,
    WorkspaceConfig,
    HooksConfig,
    AgentConfig,
    CodexConfig,
    EffectiveConfig,
]


@pytest.mark.parametrize("klass", _ALL_DATACLASSES)
def test_config_dataclasses_are_frozen(klass: type) -> None:
    assert dataclasses.is_dataclass(klass)
    assert klass.__dataclass_params__.frozen is True


def test_tracker_config_fields() -> None:
    cfg = TrackerConfig(
        kind="linear",
        endpoint="https://api.linear.app/graphql",
        api_key="abc",
        project_slug="proj",
        active_states=("Todo", "In Progress"),
        terminal_states=("Done",),
        start_state="In Progress",
        success_state="In Review",
    )
    assert cfg.kind == "linear"
    assert cfg.endpoint == "https://api.linear.app/graphql"
    assert cfg.api_key == "abc"
    assert cfg.project_slug == "proj"
    assert cfg.active_states == ("Todo", "In Progress")
    assert cfg.terminal_states == ("Done",)
    assert cfg.start_state == "In Progress"
    assert cfg.success_state == "In Review"


def test_polling_config_fields() -> None:
    cfg = PollingConfig(interval_ms=42)
    assert cfg.interval_ms == 42


def test_workspace_config_fields() -> None:
    cfg = WorkspaceConfig(root="/tmp/ws")
    assert cfg.root == "/tmp/ws"


def test_hooks_config_fields() -> None:
    cfg = HooksConfig(
        after_create=None,
        before_run="echo before",
        after_run=None,
        before_remove=None,
        timeout_ms=60000,
    )
    assert cfg.before_run == "echo before"
    assert cfg.timeout_ms == 60000
    assert cfg.after_create is None


def test_agent_config_fields() -> None:
    cfg = AgentConfig(
        max_concurrent_agents=5,
        max_turns=20,
        max_retry_backoff_ms=300000,
        max_concurrent_agents_by_state={"todo": 1},
    )
    assert cfg.max_concurrent_agents == 5
    assert cfg.max_concurrent_agents_by_state["todo"] == 1


def test_codex_config_fields() -> None:
    cfg = CodexConfig(
        command="codex app-server",
        approval_policy=None,
        thread_sandbox=None,
        turn_sandbox_policy=None,
        turn_timeout_ms=1,
        read_timeout_ms=2,
        stall_timeout_ms=3,
    )
    assert cfg.command == "codex app-server"
    assert cfg.turn_timeout_ms == 1


def test_effective_config_composes_subconfigs() -> None:
    tracker = TrackerConfig(
        kind="linear",
        endpoint="https://api.linear.app/graphql",
        api_key=None,
        project_slug=None,
        active_states=(),
        terminal_states=(),
        start_state=None,
        success_state=None,
    )
    polling = PollingConfig(interval_ms=30000)
    workspace = WorkspaceConfig(root="/tmp/ws")
    hooks = HooksConfig(
        after_create=None,
        before_run=None,
        after_run=None,
        before_remove=None,
        timeout_ms=60000,
    )
    agent = AgentConfig(
        max_concurrent_agents=10,
        max_turns=20,
        max_retry_backoff_ms=300000,
        max_concurrent_agents_by_state={},
    )
    codex = CodexConfig(
        command="codex app-server",
        approval_policy=None,
        thread_sandbox=None,
        turn_sandbox_policy=None,
        turn_timeout_ms=3600000,
        read_timeout_ms=5000,
        stall_timeout_ms=300000,
    )

    eff = EffectiveConfig(
        tracker=tracker,
        polling=polling,
        workspace=workspace,
        hooks=hooks,
        agent=agent,
        codex=codex,
    )

    assert eff.tracker is tracker
    assert eff.polling is polling
    assert eff.workspace is workspace
    assert eff.hooks is hooks
    assert eff.agent is agent
    assert eff.codex is codex


def test_tracker_active_states_immutable() -> None:
    cfg = TrackerConfig(
        kind="linear",
        endpoint="x",
        api_key=None,
        project_slug=None,
        active_states=("Todo",),
        terminal_states=("Done",),
        start_state=None,
        success_state=None,
    )
    # active_states must be a tuple (immutable). Fail loud if somebody changes
    # it to a list — mutability would let consumers patch shared default state.
    assert isinstance(cfg.active_states, tuple)
    assert isinstance(cfg.terminal_states, tuple)
