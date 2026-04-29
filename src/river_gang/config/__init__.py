"""Typed config layer (SPED §5.3, §6)."""

from river_gang.config.defaults import (
    DEFAULT_ACTIVE_STATES,
    DEFAULT_TERMINAL_STATES,
    ConfigCoercionError,
    apply_defaults,
)
from river_gang.config.resolution import (
    normalize_path,
    resolve_and_validate,
    resolve_env_vars,
)
from river_gang.config.schema import (
    AgentConfig,
    CodexConfig,
    EffectiveConfig,
    HooksConfig,
    PollingConfig,
    TrackerConfig,
    WorkspaceConfig,
)
from river_gang.config.validation import (
    SUPPORTED_TRACKER_KINDS,
    ValidationResult,
    format_error_for_operator,
    validate_for_dispatch,
)

__all__ = [
    "DEFAULT_ACTIVE_STATES",
    "DEFAULT_TERMINAL_STATES",
    "SUPPORTED_TRACKER_KINDS",
    "AgentConfig",
    "CodexConfig",
    "ConfigCoercionError",
    "EffectiveConfig",
    "HooksConfig",
    "PollingConfig",
    "TrackerConfig",
    "ValidationResult",
    "WorkspaceConfig",
    "apply_defaults",
    "format_error_for_operator",
    "normalize_path",
    "resolve_and_validate",
    "resolve_env_vars",
    "validate_for_dispatch",
]
