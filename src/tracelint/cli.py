"""Command-line interface (spec §II.10).

    tracelint check ./trace.json --tools ./tools.json          # exit 2 on a hard_defect
    tracelint check ./spans.json --format openinference        # lint OTel/OpenInference spans
    tracelint check ./traces/*.jsonl --rules R1 --json out.json --include-candidates
    tracelint check ./spans.json --sarif out.sarif             # for GitHub code scanning

``check`` lints one or more traces and returns a CI-usable exit code:

- ``0`` — linted cleanly (no ``hard_defect``).
- ``1`` — a lower gate the project opted into (``--fail-on hard_event`` or ``candidate``) was hit.
- ``2`` — a structurally-provable defect (``hard_defect``) was found.
- ``3`` — an input error: a missing, malformed, empty or wrong-format trace or tools file, an
  invalid configuration, an unknown rule, or a command-line usage error (argparse's own code, 2,
  would read as a defect).

Heuristic ``candidate`` findings never fail CI unless ``--fail-on candidate`` asks; a suppression (a
rule that could not run) is disclosed but is not a defect. Settings can live in the project's
``[tool.tracelint]`` or ``tracelint.toml`` (:mod:`tracelint.config`), with flags overriding them.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import NoReturn

from tracelint.findings import (
    EXIT_GATE,
    EXIT_HARD_DEFECT,
    EXIT_INPUT_ERROR,
    EXIT_OK,
    ConfidenceTier,
)
from tracelint.report import render_report, reports_to_dict, write_json
from tracelint.rules import lint_trace, rule_ids, select_rules
from tracelint.sources import SUPPORTED_FORMATS, load_source, load_sources
from tracelint.tools import ToolRegistry
from tracelint.trace import Trace


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


class _ArgumentParser(argparse.ArgumentParser):
    """argparse exits 2 on a usage error — the code tracelint reserves for a hard defect, so a
    mistyped flag in a CI step would read as "defect found". Usage errors exit 3 (input error).
    Subcommand parsers inherit this class."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_INPUT_ERROR, f"{self.prog}: error: {message}\n")


def _tolerate_unencodable_output() -> None:
    """Never crash printing a report. Redirected output on Windows uses the locale code page, and a
    report echoes the trace's own text (CJK, emoji), which that code page may not encode — the
    UnicodeEncodeError used to turn a clean run into exit 3. Unencodable characters are escaped."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except (ValueError, OSError):  # a closed or non-reconfigurable stream: leave it be
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
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
        default=None,
        help=(
            "input format (default: the config's, else native tracelint JSON). openinference/otel "
            "reads OpenTelemetry/OpenInference spans (Phoenix, OTLP, TRAIL); openai reads a chat "
            "message list; langfuse reads a Langfuse trace; langsmith reads a LangSmith run"
        ),
    )
    check.add_argument(
        "--rules",
        type=_csv,
        metavar="R1,R2a,...",
        help=f"subset of rules to run (default: all — {', '.join(rule_ids())})",
    )
    check.add_argument(
        "--fail-on",
        choices=[t.value for t in (ConfidenceTier.HARD_EVENT, ConfidenceTier.CANDIDATE)],
        help=(
            "also fail (exit 1) when a finding of this tier or above is found: hard_event, or "
            "candidate (default: only a hard_defect fails, with exit 2)"
        ),
    )
    check.add_argument(
        "--config",
        metavar="FILE",
        help="read settings from this file (default: the nearest tracelint.toml or "
        "pyproject.toml with [tool.tracelint])",
    )
    check.add_argument("--json", dest="json_out", metavar="OUT", help="write findings as JSON")
    check.add_argument(
        "--sarif",
        dest="sarif_out",
        metavar="OUT",
        help="write findings as SARIF 2.1.0 (for GitHub code scanning)",
    )
    check.add_argument("--html", dest="html_out", metavar="OUT", help="write an HTML report")
    check.add_argument(
        "--include-candidates",
        action="store_true",
        help="show heuristic candidate findings in the text report",
    )
    check.add_argument("--quiet", action="store_true", help="suppress the text report")
    check.set_defaults(func=_cmd_check)

    lf = sub.add_parser(
        "langfuse",
        help="pull a Langfuse trace to a file for `check` (or, advanced, check it in place)",
    )
    lfsub = lf.add_subparsers(dest="lf_command")

    # `pull` is the featured verb: fetch a trace to a file so `pull` -> `check` composes and the
    # file doubles as a saved fixture. Read-only; it never writes anything back to Langfuse.
    lfpull = lfsub.add_parser(
        "pull", help="fetch a Langfuse trace and write it to a file that `tracelint check` lints"
    )
    lfpull.add_argument("trace_id", help="Langfuse trace id")
    lfpull.add_argument(
        "-o", "--output", metavar="OUT", help="write the trace here (default: <trace-id>.json)"
    )
    lfpull.add_argument(
        "--tool-names",
        type=_csv,
        metavar="a,b,...",
        help="observation names to treat as tool calls (for span-based instrumentation)",
    )
    lfpull.set_defaults(func=_cmd_langfuse_pull)

    lfcheck = lfsub.add_parser(
        "check",
        help="(advanced) fetch a trace, lint it, and optionally write findings back as Scores",
    )
    lfcheck.add_argument("--trace", dest="trace_id", required=True, help="Langfuse trace id")
    lfcheck.add_argument("--tools", help="tools.json for schema-dependent rules (R1, R3)")
    lfcheck.add_argument(
        "--tool-names",
        type=_csv,
        metavar="a,b,...",
        help="observation names to treat as tool calls (for span-based instrumentation)",
    )
    lfcheck.add_argument(
        "--write-back",
        action="store_true",
        help="(advanced) write findings back to Langfuse as Scores (default: read-only plan)",
    )
    lfcheck.add_argument(
        "--include-candidates", action="store_true", help="show candidate findings in the report"
    )
    lfcheck.add_argument("--quiet", action="store_true", help="suppress the text report")
    lfcheck.set_defaults(func=_cmd_langfuse_check)

    init = sub.add_parser(
        "init", help="bootstrap a starter tools.json from a trace (discovers tools + schemas)"
    )
    init.add_argument("trace", help="trace file to read tools from (.json / .jsonl / a JSON array)")
    init.add_argument(
        "--format",
        dest="fmt",
        choices=list(SUPPORTED_FORMATS),
        default="native",
        help="input format (same choices as `check`; openinference/otel carry tool schemas)",
    )
    init.add_argument(
        "-o",
        "--output",
        metavar="OUT",
        help="write the starter tools.json here (default: print it to stdout)",
    )
    init.set_defaults(func=_cmd_init)

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
    from pathlib import Path

    from tracelint.config import Config, apply_ignores, find_config, load_config

    config_path = Path(args.config) if args.config else find_config()
    config = load_config(config_path) if config_path else Config()
    tools = args.tools or (str(config.tools) if config.tools else None)
    registry = ToolRegistry.load(tools) if tools else ToolRegistry()
    rules = select_rules(args.rules or config.rules)
    fail_on = ConfidenceTier(args.fail_on) if args.fail_on else config.fail_on

    reports = []
    linted: list[Trace] = []
    uris: list[str] = []
    used: set[int] = set()
    for trace, path in load_sources(args.traces, args.fmt or config.format or "native"):
        report = lint_trace(trace, rules, registry)
        used |= apply_ignores(report, config.ignores, path)
        if fail_on is not None:
            report.fail_on = fail_on
        reports.append(report)
        linted.append(trace)
        uris.append(path)
    for n, ignore in enumerate(config.ignores):
        if n not in used:
            print(
                f"tracelint: warning: {_shown_path(config.source)}: ignore #{n + 1} "
                f"({ignore.describe()}) matched no finding",
                file=sys.stderr,
            )

    if args.json_out:
        write_json(args.json_out, reports_to_dict(reports))
    if args.sarif_out:
        from tracelint.sarif import to_sarif

        write_json(args.sarif_out, to_sarif(reports, tool_version=_version(), uris=uris))
    if args.html_out:
        from tracelint.report import render_html, write_html

        write_html(
            args.html_out,
            render_html(title="tracelint report", reports=reports, traces=linted),
        )

    if not args.quiet:
        for report in reports:
            print(render_report(report, include_candidates=args.include_candidates))

    return max((r.exit_code for r in reports), default=EXIT_OK)


def _shown_path(path: object) -> str:
    """``path`` relative to the working directory when that is shorter to read."""
    import os

    try:
        return os.path.relpath(str(path))
    except ValueError:  # another drive on Windows
        return str(path)


def _safe_filename(name: str) -> str:
    """A filesystem-safe basename from a trace id (usually hex/UUID, so this rarely bites)."""
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)
    return cleaned or "trace"


def _cmd_langfuse_pull(args: argparse.Namespace) -> int:
    from pathlib import Path

    from tracelint.integrations.langfuse import LangfuseIntegration

    # fetch_trace turns any vendor/network/auth failure into a clean RuntimeError (-> exit 3) whose
    # message names the env vars to check but never echoes their values.
    trace = LangfuseIntegration().fetch_trace(args.trace_id, tool_names=args.tool_names)
    out = args.output or f"{_safe_filename(args.trace_id)}.json"
    Path(out).write_text(trace.to_json() + "\n", encoding="utf-8")
    print(f"wrote Langfuse trace {args.trace_id} to {out} (native format).")
    print(f"lint it: tracelint check {out}")
    return EXIT_OK


def _cmd_langfuse_check(args: argparse.Namespace) -> int:
    from tracelint.integrations.langfuse import LangfuseIntegration

    registry = ToolRegistry.load(args.tools) if args.tools else ToolRegistry()
    result = LangfuseIntegration().check(
        args.trace_id,
        registry=registry,
        tool_names=args.tool_names,
        write_back=args.write_back,
    )
    if not args.quiet:
        print(render_report(result.report, include_candidates=args.include_candidates))
        print()
        if args.write_back:
            print(f"wrote {result.written} score(s) back to Langfuse trace {args.trace_id}:")
        else:
            print("would write these scores to Langfuse (re-run with --write-back):")
        for plan in result.plans:
            target = f"obs {plan.observation_id}" if plan.observation_id else "trace"
            print(f"  {plan.name:34} {plan.value:<5} [{target}]")
    return EXIT_HARD_DEFECT if result.report.has_hard_defect else EXIT_OK


def _cmd_init(args: argparse.Namespace) -> int:
    from tracelint.contract import discover_contract

    draft = discover_contract(load_source(args.trace, args.fmt))
    payload = json.dumps(draft.to_dict(), indent=2)
    if args.output:
        from pathlib import Path

        Path(args.output).write_text(payload + "\n", encoding="utf-8")
        print(draft.summary())
        print(f"\nwrote starter contract to {args.output} — fill the behavior fields, then pass it "
              "to `tracelint check --tools`.")
    else:
        # stdout is the contract (pipe-able); the review summary goes to stderr so it never
        # corrupts the JSON.
        print(payload)
        print(draft.summary(), file=sys.stderr)
    return EXIT_OK


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
    _tolerate_unencodable_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None) or not getattr(args, "func", None):
        parser.print_help()
        return EXIT_OK
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError, KeyError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"tracelint: error: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    except Exception as exc:  # noqa: BLE001 — last-resort: never dump a traceback at a user
        # An unfamiliar trace shape should degrade gracefully, and (during outreach) become a bug
        # report rather than a scary stack trace.
        print(
            f"tracelint: could not process this input ({type(exc).__name__}: {exc}).\n"
            "This may be a trace shape tracelint doesn't handle yet — please report it at "
            "https://github.com/AshwinUgale/tracelint/issues with the trace and command.",
            file=sys.stderr,
        )
        return EXIT_INPUT_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
