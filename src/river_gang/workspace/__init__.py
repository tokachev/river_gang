"""Workspace manager (SPED §9)."""

from river_gang.workspace.hooks import (
    DEFAULT_LOG_TRUNCATE_BYTES,
    HookResult,
    run_hook,
    truncate_for_log,
)
from river_gang.workspace.manager import (
    EnsureResult,
    HookRunner,
    WorkspaceHookFailed,
    WorkspaceManager,
    WorkspaceManagerError,
    WorkspaceNotADirectory,
)
from river_gang.workspace.safety import (
    EmptyWorkspaceKey,
    InvalidWorkspaceKey,
    WorkspaceOutsideRoot,
    WorkspaceSafetyError,
    sanitize_key,
    validate_within_root,
)

__all__ = [
    "DEFAULT_LOG_TRUNCATE_BYTES",
    "EmptyWorkspaceKey",
    "EnsureResult",
    "HookResult",
    "HookRunner",
    "InvalidWorkspaceKey",
    "WorkspaceHookFailed",
    "WorkspaceManager",
    "WorkspaceManagerError",
    "WorkspaceNotADirectory",
    "WorkspaceOutsideRoot",
    "WorkspaceSafetyError",
    "run_hook",
    "sanitize_key",
    "truncate_for_log",
    "validate_within_root",
]
