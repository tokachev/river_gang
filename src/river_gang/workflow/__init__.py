"""WORKFLOW.md loader and parser (SPED §5)."""

from river_gang.workflow.errors import (
    FrontMatterNotAMap,
    MissingWorkflowFile,
    WorkflowError,
    WorkflowParseError,
)
from river_gang.workflow.loader import WorkflowDefinition, load_workflow
from river_gang.workflow.watcher import LastKnownGoodHolder, WorkflowWatcher

__all__ = [
    "FrontMatterNotAMap",
    "LastKnownGoodHolder",
    "MissingWorkflowFile",
    "WorkflowDefinition",
    "WorkflowError",
    "WorkflowParseError",
    "WorkflowWatcher",
    "load_workflow",
]
