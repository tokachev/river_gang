"""Typed exception hierarchy for the Codex layer (SPED §10.6).

All subclasses defined here even when only a subset is raised by the current
task — later M5 tasks plug in the rest without churning this file.
"""

from __future__ import annotations


class CodexError(Exception):
    """Base class for all Codex-layer failures."""


# ---------------------------------------------------------------------------
# Launch / configuration
# ---------------------------------------------------------------------------


class CodexNotFound(CodexError):  # noqa: N818 -- spec-defined name
    """``codex.command`` could not be located/executed (typical exit 127)."""


class InvalidWorkspaceCwd(CodexError):  # noqa: N818 -- spec-defined name
    """The requested ``cwd`` is not inside the configured workspace root."""


# ---------------------------------------------------------------------------
# Stream / framing
# ---------------------------------------------------------------------------


class PortExit(CodexError):  # noqa: N818 -- spec-defined name
    """Subprocess exited (cleanly or otherwise) while a read was expected."""

    def __init__(self, message: str, *, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


class ResponseError(CodexError):  # noqa: N818 -- spec-defined name
    """Protocol frame could not be decoded or exceeded the size limit."""


# ---------------------------------------------------------------------------
# Timeouts (raised by later tasks; defined now)
# ---------------------------------------------------------------------------


class ResponseTimeout(CodexError):  # noqa: N818 -- spec-defined name
    """Sync request exceeded ``codex.read_timeout_ms``."""


class TurnTimeout(CodexError):  # noqa: N818 -- spec-defined name
    """Turn stream exceeded ``codex.turn_timeout_ms``."""


# ---------------------------------------------------------------------------
# Turn outcomes (raised by later tasks; defined now)
# ---------------------------------------------------------------------------


class TurnFailed(CodexError):  # noqa: N818 -- spec-defined name
    """Turn ended with an explicit failure event."""


class TurnCancelled(CodexError):  # noqa: N818 -- spec-defined name
    """Turn was cancelled (operator-initiated or timeout-driven)."""
