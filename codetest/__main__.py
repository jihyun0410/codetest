"""``python -m codetest`` entry point."""
from __future__ import annotations

from .cli.cli_parser import app

if __name__ == "__main__":
    app()
