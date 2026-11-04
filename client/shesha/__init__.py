"""Shesha — SDR waterfall client."""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> None:
    """Open the TUI with no arguments, or run the CLI with arguments."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] != "tui":
        from .cli import main as cli_main
        cli_main(argv)
        return

    from .tui import main as tui_main
    tui_main(argv[1:] if argv else [])
