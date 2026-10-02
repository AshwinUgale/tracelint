"""Project configuration — the CI contract for ``tracelint check``.

A project keeps its settings in ``[tool.tracelint]`` in ``pyproject.toml``, or in a
``tracelint.toml`` (same keys, at the top level), so the command line in CI stays short and the
contract is versioned with the code::

    [tool.tracelint]
    format = "openinference"     # the default --format
    tools = "tools.json"         # the default --tools, relative to this file
    rules = ["R1", "R2a", "R2b"] # the default --rules (default: all)
    fail_on = "hard_event"       # also fail CI (exit 1) on hard events; or "candidate"

    [[tool.tracelint.ignore]]
    rule = "R3"
    tool = "add_to_cart"
    field = "note"
    reason = "free-text note the model writes"

The nearest file wins: the current directory, then its parents, stopping at the repository root
(the directory holding ``.git``); in each directory ``tracelint.toml`` is read before a
``pyproject.toml`` with a ``[tool.tracelint]`` table. ``--config`` names a file instead. Flags given
on the command line override the file.

An **ignore** accepts findings of one rule, optionally narrowed to a tool, an argument field or
trace files (a glob), and must say why. An ignored finding stays in the report, shown with its
reason and counted, but no longer counts toward the exit code. An ignore that matches nothing is
reported, so stale entries don't pile up unnoticed. A misspelled key or value fails the run (exit
3) rather than being skipped: a typo in a gate must not silently loosen it.
"""

from __future__ import annotations

import fnmatch
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tracelint.findings import ConfidenceTier, LintReport
from tracelint.identity import FindingKey, finding_key

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib

CONFIG_FILE = "tracelint.toml"
PYPROJECT = "pyproject.toml"

_KEYS = {"format", "tools", "rules", "fail_on", "ignore"}
_IGNORE_KEYS = {"rule", "tool", "field", "path", "reason"}
_FAIL_ON = {tier.value: tier for tier in ConfidenceTier}


class ConfigError(ValueError):
    """The configuration file is invalid; ``tracelint check`` exits 3 with this message."""


@dataclass(frozen=True)
class Ignore:
    """One accepted kind of finding, with the reason it is accepted."""

    rule: str
    reason: str
    tool: str | None = None
    field: str | None = None
    path: str | None = None  # a glob over the trace path as given to `tracelint check`

    def matches(self, key: FindingKey, trace_path: str) -> bool:
        if key.rule != self.rule:
            return False
        if self.tool is not None and self.tool not in key.tools:
            return False
        if self.field is not None and self.field not in key.fields:
            return False
        if self.path is not None:
            return fnmatch.fnmatch(trace_path.replace("\\", "/"), self.path)
        return True

    def describe(self) -> str:
        scope = [f"{name}={value}" for name, value in self._narrowing() if value is not None]
        return " ".join([self.rule, *scope])

    def _narrowing(self) -> list[tuple[str, str | None]]:
        return [("tool", self.tool), ("field", self.field), ("path", self.path)]


@dataclass(frozen=True)
class Config:
    """Settings for ``tracelint check``; ``None`` fields fall back to the command's defaults."""

    source: Path | None = None
    format: str | None = None
    tools: Path | None = None
    rules: list[str] | None = None
    fail_on: ConfidenceTier | None = None
    ignores: tuple[Ignore, ...] = ()


def find_config(start: Path | None = None) -> Path | None:
    """The nearest configuration file at or above ``start`` (default: the current directory),
    stopping at the repository root."""
    directory = (start or Path.cwd()).resolve()
    for candidate in (directory, *directory.parents):
        own = candidate / CONFIG_FILE
        if own.is_file():
            return own
        pyproject = candidate / PYPROJECT
        if pyproject.is_file() and _tool_table(_read_toml(pyproject)) is not None:
            return pyproject
        if (candidate / ".git").exists():
            return None
    return None


def load_config(path: Path) -> Config:
    """Read and validate the configuration in ``path``."""
    data = _read_toml(path)
    if path.name == PYPROJECT:
        table = _tool_table(data)
        if table is None:
            raise ConfigError(f"{path}: no [tool.tracelint] table")
    else:
        table = data
    return _parse(table, path)


def apply_ignores(report: LintReport, ignores: tuple[Ignore, ...], trace_path: str) -> set[int]:
    """Mark the findings in ``report`` that an ignore accepts; return the indices of the ignores
    that matched."""
    used: set[int] = set()
    for finding in report.active_findings:
        key = finding_key(finding)
        for i, ignore in enumerate(ignores):
            if ignore.matches(key, trace_path):
                finding.ignored_reason = ignore.reason
                used.add(i)
                break
    return used


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: not valid TOML: {exc}") from exc


def _tool_table(data: dict[str, Any]) -> dict[str, Any] | None:
    table = data.get("tool", {}).get("tracelint")
    return table if isinstance(table, dict) else None


def _parse(table: dict[str, Any], path: Path) -> Config:
    from tracelint.rules import rule_ids  # deferred: the rules package imports the findings model
    from tracelint.sources import SUPPORTED_FORMATS

    def fail(message: str) -> ConfigError:
        return ConfigError(f"{path}: {message}")

    unknown = sorted(set(table) - _KEYS)
    if unknown:
        raise fail(f"unknown key {unknown[0]!r}; expected one of {', '.join(sorted(_KEYS))}")

    fmt = table.get("format")
    if fmt is not None and fmt not in SUPPORTED_FORMATS:
        raise fail(f"format {fmt!r} is not one of {', '.join(SUPPORTED_FORMATS)}")

    tools = table.get("tools")
    if tools is not None and not isinstance(tools, str):
        raise fail("tools must be a path (a string)")

    known_rules = rule_ids()
    rules = table.get("rules")
    if rules is not None:
        if not isinstance(rules, list) or not all(isinstance(r, str) for r in rules):
            raise fail('rules must be a list of rule ids, e.g. ["R1", "R2b"]')
        for rule in rules:
            if rule not in known_rules:
                raise fail(f"unknown rule {rule!r}; known rules: {', '.join(known_rules)}")

    fail_on = table.get("fail_on")
    if fail_on is not None and fail_on not in _FAIL_ON:
        raise fail(f"fail_on {fail_on!r} is not one of {', '.join(_FAIL_ON)}")

    entries = table.get("ignore", [])
    if not isinstance(entries, list):
        raise fail("ignore must be a list of tables ([[tool.tracelint.ignore]])")
    ignores = tuple(
        _parse_ignore(entry, n, known_rules, fail) for n, entry in enumerate(entries, 1)
    )

    return Config(
        source=path,
        format=fmt,
        tools=(path.parent / tools) if tools is not None else None,
        rules=rules,
        fail_on=_FAIL_ON[fail_on] if fail_on is not None else None,
        ignores=ignores,
    )


def _parse_ignore(entry: Any, n: int, known_rules: list[str], fail: Any) -> Ignore:
    where = f"ignore #{n}"
    if not isinstance(entry, dict):
        raise fail(f"{where} must be a table with rule and reason")
    unknown = sorted(set(entry) - _IGNORE_KEYS)
    if unknown:
        raise fail(
            f"{where}: unknown key {unknown[0]!r}; expected {', '.join(sorted(_IGNORE_KEYS))}"
        )
    rule = entry.get("rule")
    if rule not in known_rules:
        raise fail(f"{where}: rule {rule!r} is not a known rule ({', '.join(known_rules)})")
    reason = entry.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise fail(f"{where} ({rule}) needs a reason: say why this finding is accepted")
    narrowing = {name: entry.get(name) for name in ("tool", "field", "path")}
    for name, value in narrowing.items():
        if value is not None and not isinstance(value, str):
            raise fail(f"{where}: {name} must be a string")
    return Ignore(rule=rule, reason=reason.strip(), **narrowing)
