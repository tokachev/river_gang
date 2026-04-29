"""CLI entry point (SPED §17.7).

``river-gang [WORKFLOW.md] [--port N]``

Resolves a WORKFLOW.md path (positional or default ``./WORKFLOW.md``)
and hands off to :func:`river_gang.orchestrator.startup.start_service`.
The service's int return value becomes the process exit code (0 for a
clean shutdown, nonzero for a startup failure).

Exit-code policy:

- 0 — service ran and shut down cleanly (SIGINT/SIGTERM or
  programmatic shutdown).
- 1 — workflow file missing OR :func:`start_service` returned 1
  (validation failure, malformed workflow, etc.).
- 2 — :func:`start_service` raised an unexpected exception. The error
  string is surfaced on stderr so operators can grep the journal.

``KeyboardInterrupt`` outside ``start_service``'s own signal-handler
path (e.g. raised by ``asyncio.run`` itself) is treated as graceful
shutdown → exit 0.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from river_gang.orchestrator.startup import start_service

DEFAULT_WORKFLOW_FILENAME = "WORKFLOW.md"

PROG_NAME = "river-gang"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG_NAME,
        description=(
            "Symphony orchestrator: poll a tracker, claim issues, "
            "drive a coding agent. Reads configuration from a "
            "WORKFLOW.md file in the current directory by default."
        ),
    )
    parser.add_argument(
        "workflow_path",
        nargs="?",
        default=None,
        help=(
            f"Path to WORKFLOW.md. Defaults to ./{DEFAULT_WORKFLOW_FILENAME} "
            "if omitted."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            "Port for the OPTIONAL HTTP extension. Overrides "
            "server.port from WORKFLOW.md when both are present."
        ),
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse ``argv`` (or :data:`sys.argv` slice) into a :class:`Namespace`.

    ``--help`` exits with code 0 via argparse's default behaviour;
    invalid integer values for ``--port`` exit with code 2 (also
    argparse default).
    """
    parser = _build_parser()
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns process exit code.

    See module docstring for the full exit-code policy.
    """
    args = parse_args(argv)
    path = _resolve_workflow_path(args.workflow_path)

    if not path.is_file():
        _emit_missing_workflow_error(path, used_default=args.workflow_path is None)
        return 1

    try:
        return asyncio.run(
            start_service(workflow_path=path, port=args.port)
        )
    except KeyboardInterrupt:
        # asyncio.run sometimes re-raises KeyboardInterrupt even after the
        # in-loop signal handler converted SIGINT to a Shutdown message.
        # Either way, KI here means "operator wants out" → graceful exit.
        return 0
    except Exception as exc:  # noqa: BLE001 -- top-level fault barrier
        print(f"{PROG_NAME}: unexpected startup failure: {exc}", file=sys.stderr)
        return 2


def _resolve_workflow_path(workflow_path: str | None) -> Path:
    if workflow_path is None:
        return (Path.cwd() / DEFAULT_WORKFLOW_FILENAME).resolve()
    return Path(workflow_path).resolve()


def _emit_missing_workflow_error(path: Path, *, used_default: bool) -> None:
    if used_default:
        message = (
            f"workflow file not found: {path}\n"
            f"  (default ./{DEFAULT_WORKFLOW_FILENAME} resolved against cwd)\n"
            "  pass an explicit workflow path as the first positional argument"
        )
    else:
        message = f"workflow file not found: {path}"
    print(message, file=sys.stderr)


__all__ = [
    "DEFAULT_WORKFLOW_FILENAME",
    "PROG_NAME",
    "main",
    "parse_args",
]
