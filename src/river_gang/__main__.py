"""Entry point: ``python -m river_gang``.

Delegates to :func:`river_gang.cli.main` and propagates its int return
value as the process exit code.
"""

from __future__ import annotations

import sys

from river_gang.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
