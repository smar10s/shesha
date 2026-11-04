"""Headless CLI for the Shesha SDR daemon."""
from __future__ import annotations

from .parser import build_parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
