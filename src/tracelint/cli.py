"""Command-line interface (spec §II.10).

Phase 0 ships a stub so the ``tracelint`` console script resolves and ``--version`` works; the
real ``check`` / ``scorecard`` subcommands and their CI exit codes are wired up in later phases
as the rules land.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from tracelint import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tracelint",
        description="Deterministic, judge-free static analyzer for agent traces.",
    )
    parser.add_argument("--version", action="version", version=f"tracelint {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    # No subcommands yet (Phase 0). Print help so the entry point is useful.
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
