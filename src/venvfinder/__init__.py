"""venvfinder — locate and inspect Python virtual environments (uv, venv, conda)."""

from importlib.metadata import PackageNotFoundError, version

from .app import main

try:
    __version__ = version("venvfinder")
except PackageNotFoundError:  # running from a source checkout without install
    __version__ = "0.0.0"

__all__ = ["__version__", "main"]
