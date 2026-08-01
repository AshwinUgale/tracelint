"""Command-line interface (spec §II.10).

    tracelint check ./trace.json --tools ./tools.json          # exit 2 on a hard_defect
    tracelint check ./traces/*.jsonl --rules R1 --json out.json --include-candidates

``check`` lints one or more traces and returns a CI-usable exit code:

- ``0`` — linted cleanly (no ``hard_defect``).
- ``2`` — a structurally-provable defect (``hard_defect``) was found.
- ``3`` — an input error (missing/malformed trace or tools file, or an unknown rule).

Heuristic ``candidate`` findings never fail CI on their own; a suppression (a rule that could not
run) is disclosed but is not a defect.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from tracelint.findings import EXIT_HARD_DEFECT, EXIT_INPUT_ERROR, EXIT_OK
from tracelint.report import render_report, reports_to_dict, write_json
from tracelint.rules import lint_trace, rule_ids, select_rules
from tracelint.tools import ToolRegistry
from tracelint.trace import Trace, load_traces


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tracelint",
        description="Deterministic, judge-free static analyzer for agent traces.",
    )
    parser.add_argument("--version", action="version", version=f"tracelint {_version()}")
    sub = parser.add_subparsers(dest="command")

    check = sub.add_parser("check", help="lint one or more agent traces")
    check.add_argument("traces", nargs="+", help="trace file(s): .json / .jsonl / a JSON array")
    check.add_argument("--tools", help="tool schemas + metadata (JSON) — ground truth for rules")
    check.add_argument(
        "--rules",
        type=_csv,
        metavar="R1,R2,...",
        help=f"subset of rules to run (default: all — {', '.join(rule_ids())})",
    )
    check.add_argument("--json", dest="json_out", metavar="OUT", help="write findings as JSON")
    check.add_argument(
        "--include-candidates",
        action="store_true",
        help="show heuristic candidate findings in the text report",
    )
    check.add_argument("--quiet", action="store_true", help="suppress the text report")
    check.set_defaults(func=_cmd_check)
    return parser


def _version() -> str:
    from tracelint import __version__

    return __version__


def _cmd_check(args: argparse.Namespace) -> int:
    registry = ToolRegistry.load(args.tools) if args.tools else ToolRegistry()
    rules = select_rules(args.rules)

    reports = []
    for path in args.traces:
        traces: list[Trace] = load_traces(path)
        for trace in traces:
            reports.append(lint_trace(trace, rules, registry))

    if args.json_out:
        write_json(args.json_out, reports_to_dict(reports))

    if not args.quiet:
        for report in reports:
            print(render_report(report, include_candidates=args.include_candidates))

    return EXIT_HARD_DEFECT if any(r.has_hard_defect for r in reports) else EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_OK
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"tracelint: error: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
