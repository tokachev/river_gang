"""Frozen typed config dataclasses (SPED §5.3, §6.4).

Defaults and coercion live in :mod:`river_gang.config.defaults`; this module
holds only the data shapes. ``$VAR`` indirection and path absolutization are
applied in a later layer (Task 5) — fields here may still be raw strings.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TrackerConfig:
    kind: str | None
    endpoint: str | None
    api_key: str | None
    project_slug: str | None
    active_states: tuple[str, ...]
    terminal_states: tuple[str, ...]
    start_state: str | None
    success_state: str | None


@dataclass(frozen=True)
class PollingConfig:
    interval_ms: int


@dataclass(frozen=True)
class WorkspaceConfig:
    root: str


@dataclass(frozen=True)
class HooksConfig:
    after_create: str | None
    before_run: str | None
    after_run: str | None
    before_remove: str | None
    timeout_ms: int


@dataclass(frozen=True)
class AgentConfig:
    max_concurrent_agents: int
    max_turns: int
    max_retry_backoff_ms: int
    max_concurrent_agents_by_state: dict[str, int]


@dataclass(frozen=True)
class CodexConfig:
    command: str
    approval_policy: str | None
    thread_sandbox: str | None
    turn_sandbox_policy: str | None
    turn_timeout_ms: int
    read_timeout_ms: int
    stall_timeout_ms: int


@dataclass(frozen=True)
class EffectiveConfig:
    tracker: TrackerConfig
    polling: PollingConfig
    workspace: WorkspaceConfig
    hooks: HooksConfig
    agent: AgentConfig
    codex: CodexConfig
