"""Command-line interface (spec §II.10).

    tracelint check ./trace.json --tools ./tools.json          # exit 2 on a hard_defect
    tracelint check ./spans.json --format openinference        # lint OTel/OpenInference spans
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

from tracelint.findings import EXIT_GATE, EXIT_HARD_DEFECT, EXIT_INPUT_ERROR, EXIT_OK
from tracelint.report import render_report, reports_to_dict, write_json
from tracelint.rules import lint_trace, rule_ids, select_rules
from tracelint.sources import SUPPORTED_FORMATS, load_source
from tracelint.tools import ToolRegistry
from tracelint.trace import Trace


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
        "--format",
        dest="fmt",
        choices=list(SUPPORTED_FORMATS),
        default="native",
        help=(
            "input format (default: native tracelint JSON). openinference/otel reads "
            "OpenTelemetry/OpenInference spans (Phoenix, OTLP, TRAIL); openai reads a chat "
            "message list; langfuse reads a Langfuse trace"
        ),
    )
    check.add_argument(
        "--rules",
        type=_csv,
        metavar="R1,R2,...",
        help=f"subset of rules to run (default: all — {', '.join(rule_ids())})",
    )
    check.add_argument("--json", dest="json_out", metavar="OUT", help="write findings as JSON")
    check.add_argument("--html", dest="html_out", metavar="OUT", help="write an HTML report")
    check.add_argument(
        "--include-candidates",
        action="store_true",
        help="show heuristic candidate findings in the text report",
    )
    check.add_argument("--quiet", action="store_true", help="suppress the text report")
    check.set_defaults(func=_cmd_check)

    demo = sub.add_parser("demo", help="run the keyless validation suite + recovery scorecard")
    demo.add_argument("--html", dest="html_out", metavar="OUT", help="write an HTML report")
    demo.add_argument("--runs", type=int, default=3, help="scorecard runs per fault (default 3)")
    demo.set_defaults(func=_cmd_demo)

    sc = sub.add_parser("scorecard", help="measure per-fault recovery on the built-in demo task")
    sc.add_argument(
        "--demo", action="store_true", help="run the built-in order-cancellation recovery task"
    )
    sc.add_argument(
        "--buggy", action="store_true", help="use the error-ignoring agent (for contrast)"
    )
    sc.add_argument(
        "--faults",
        type=_csv,
        metavar="timeout,error,...",
        help="fault types to inject (default: timeout,error,rate_limit)",
    )
    sc.add_argument("--runs", type=int, default=1, help="runs per fault (default 1)")
    sc.set_defaults(func=_cmd_scorecard)
    return parser


def _version() -> str:
    from tracelint import __version__

    return __version__


def _cmd_check(args: argparse.Namespace) -> int:
    registry = ToolRegistry.load(args.tools) if args.tools else ToolRegistry()
    rules = select_rules(args.rules)

    reports = []
    for path in args.traces:
        traces: list[Trace] = load_source(path, args.fmt)
        for trace in traces:
            reports.append(lint_trace(trace, rules, registry))

    if args.json_out:
        write_json(args.json_out, reports_to_dict(reports))
    if args.html_out:
        from tracelint.report import render_html, write_html

        write_html(args.html_out, render_html(title="tracelint report", reports=reports))

    if not args.quiet:
        for report in reports:
            print(render_report(report, include_candidates=args.include_candidates))

    return EXIT_HARD_DEFECT if any(r.has_hard_defect for r in reports) else EXIT_OK


def _cmd_demo(args: argparse.Namespace) -> int:
    from tracelint.agent import build_recovery_task
    from tracelint.injection import FaultType
    from tracelint.report import render_html, write_html
    from tracelint.rules import default_rules
    from tracelint.scorecard import render_scorecard, run_scorecard
    from tracelint.validation import validation_cases

    results = []
    all_ok = True
    for case in validation_cases():
        report = lint_trace(case.trace, default_rules(), case.registry)
        ok = case.check(report)
        all_ok = all_ok and ok
        results.append((case, report, ok))

    faults = [FaultType.TIMEOUT, FaultType.ERROR, FaultType.RATE_LIMIT]
    runs = max(1, args.runs)
    scorecards = [
        run_scorecard(build_recovery_task(buggy=False), faults, runs=runs),
        run_scorecard(build_recovery_task(buggy=True), faults, runs=runs),
    ]

    passed = sum(1 for *_r, ok in results if ok)
    print(f"validation: {passed}/{len(results)} cases behaved as expected")
    for case, _report, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {case.name} - {case.expectation}")
    print()
    for sc in scorecards:
        print(render_scorecard(sc))
        print()

    if args.html_out:
        from tracelint.agent.demo import run_ignored_error_demo

        wtrace, wtools = run_ignored_error_demo()
        wreport = lint_trace(wtrace, default_rules(), wtools.to_registry())
        html = render_html(
            title="tracelint demo",
            validation=results,
            scorecards=scorecards,
            worked=[(wtrace, wreport)],
        )
        write_html(args.html_out, html)
        print(f"wrote {args.html_out}")

    return EXIT_OK if all_ok else EXIT_GATE


def _cmd_scorecard(args: argparse.Namespace) -> int:
    if not args.demo:
        raise ValueError(
            "scorecard currently supports only --demo (external agents are future work)"
        )
    from tracelint.agent import build_recovery_task
    from tracelint.injection import FaultType
    from tracelint.scorecard import render_scorecard, run_scorecard

    fault_names = args.faults or ["timeout", "error", "rate_limit"]
    faults = [FaultType(name) for name in fault_names]  # ValueError → exit 3 on a bad name
    task = build_recovery_task(buggy=args.buggy)
    scorecard = run_scorecard(task, faults, runs=max(1, args.runs))
    print(render_scorecard(scorecard))
    return EXIT_OK


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
