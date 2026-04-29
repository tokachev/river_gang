"""river_gang — Symphony Service Specification v1 implementation."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("river-gang")
except PackageNotFoundError:  # editable install before metadata exists
    __version__ = "0.0.0"

__all__ = ["__version__"]
