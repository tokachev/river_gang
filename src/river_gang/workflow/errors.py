"""Typed exceptions raised by the workflow loader (SPED §5.2)."""

from __future__ import annotations


class WorkflowError(Exception):
    """Base class for all workflow loader errors."""


class MissingWorkflowFile(WorkflowError):  # noqa: N818  -- spec-defined name
    """Raised when the WORKFLOW.md path cannot be read (SPED §5.1)."""


class WorkflowParseError(WorkflowError):
    """Raised when YAML front matter cannot be decoded or is malformed."""


class FrontMatterNotAMap(WorkflowError):  # noqa: N818  -- spec-defined name
    """Raised when YAML front matter decodes to a non-map value (SPED §5.2)."""
