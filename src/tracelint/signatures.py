"""Call/result signatures for loop and redundancy detection (learning-doc 02 §4).

The whole game is the granularity of a signature (02 §4): too coarse and real loops hide; too
fine (hashing a raw payload with timestamps/ids) and nothing ever looks identical, so real repeats
hide too. Two derived notions are kept, for two different questions:

- ``result_class`` — a **coarse** bucket (``error`` / ``empty`` / ``status:<state>`` / ``ok``)
  that captures whether a *waiting* state advanced. A poll advances ``status:pending →
  status:completed``, giving different classes at the advancing step — which is how a legitimate
  poll is told apart from a stuck one.
- ``result_fingerprint`` — a **fine** canonical form of the whole result, for "the identical call
  produced the identical result". Both R4 and R5 compare repeats by it: on 10,541 real agent runs
  (experiments/terminal_bench) comparing the coarse class instead read a growing build log or
  training progress as "no change", and 55% of R4's loops were output that was progressing. A
  missing result is never identical to anything (``None`` is unknown, not equal).

:func:`is_poll_call` recognises a call that waits on something already running — empty input,
``sleep``, or a tool named for waiting — which repeats by design.

``normalize_args`` canonicalizes arguments and strips volatile fields (timestamps, request ids) so
semantically-identical calls compare equal.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from typing import Any

from tracelint.trace import ResultStatus, ToolCall, ToolResult
from tracelint.valueutil import normalize

# Result-class states that mean "still waiting" — a poll in progress, not a stuck loop.
WAITING_STATES = {"pending", "in_progress", "queued", "running", "processing", "waiting", "started"}

# Argument keys that vary run-to-run and must not make two identical calls look different.
VOLATILE_ARG_KEYS = {
    "timestamp",
    "ts",
    "time",
    "request_id",
    "requestid",
    "nonce",
    "idempotency_key",
    "trace_id",
    "traceid",
    "span_id",
}

_EMPTY_TEXT_RE = re.compile(r"^\s*(no results?|not found|none found|0 results?)\s*$", re.IGNORECASE)

# A tool whose name says it waits (``wait_shell_command``, ``pollJob``, ``sleep``).
_POLL_NAME_TOKENS = {"wait", "poll", "polling", "sleep"}
_NAME_TOKEN_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")
# An input that only waits: a ``sleep`` / ``wait`` command (``sleep 30 && tail build.log`` too).
_WAIT_INPUT_RE = re.compile(r"^\s*(sleep|wait)\b", re.IGNORECASE)
# Arguments that describe a call rather than feed it (Codex's ``summary: "Polling the build"``).
_DESCRIPTIVE_ARG_KEYS = {
    "summary", "description", "reason", "explanation", "thought", "title", "note", "comment",
}


def is_structured_error(result: ToolResult) -> bool:
    """True iff the result carries an unambiguous, structured error signal (shared with R2)."""
    if result.status is ResultStatus.ERROR:
        return True
    http = result.http_status
    if isinstance(http, int) and not isinstance(http, bool) and http >= 400:
        return True
    return bool(result.error)


def looks_empty(content: Any) -> bool:
    """True for an empty result — ``None``, an empty container, or an empty-ish phrase."""
    if content is None:
        return True
    if isinstance(content, (str, list, tuple, dict)) and len(content) == 0:
        return True
    return bool(isinstance(content, str) and _EMPTY_TEXT_RE.match(content))


def normalize_args(args: dict[str, Any]) -> str:
    """Canonical, volatile-field-stripped JSON of a call's arguments."""
    filtered = {k: v for k, v in args.items() if k.lower() not in VOLATILE_ARG_KEYS}
    return json.dumps(filtered, sort_keys=True, default=str)


def call_args_key(call: ToolCall) -> str:
    """The equality key the repeat rules (R4/R5) compare calls by.

    A call whose real arguments the trace did not record gets a key unique to that call: two
    *unknown* argument sets cannot be shown equal, so a redacted or uncaptured record must never
    make a loop or a redundant call (the rules disclose those calls as not checked instead).
    """
    if call.args_unavailable is not None:
        return f"\x00unknown-args:{call.index}:{call.call_id}"
    return normalize_args(call.args)


def result_class(result: ToolResult | None) -> str:
    """Coarse state bucket used to tell progress from no-progress."""
    if result is None:
        return "no_result"
    if is_structured_error(result):
        return "error"
    if looks_empty(result.content):
        return "empty"
    content = result.content
    if isinstance(content, dict):
        for key in ("status", "state"):
            if key in content:
                return f"status:{normalize(content[key])}"
    return "ok"


def result_fingerprint(result: ToolResult | None) -> str:
    """Fine-grained canonical form of the whole result (for identical-result detection)."""
    if result is None:
        return "no_result"
    return json.dumps(
        {"status": result.status.value, "content": result.content}, sort_keys=True, default=str
    )


def is_waiting_class(rc: str) -> bool:
    """True if a coarse ``result_class`` denotes a still-in-progress (waiting) state."""
    return rc.startswith("status:") and rc.split(":", 1)[1].strip() in WAITING_STATES


def command_text(call: ToolCall) -> str | None:
    """The shell command a call runs, read from the usual argument names, or ``None``."""
    for key in ("command", "cmd", "keystrokes", "script"):
        value = call.args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


# Programs whose exit status 1 means "searched, found nothing" (POSIX grep and its kin).
_SEARCH_PROGRAMS = {"grep", "egrep", "fgrep", "zgrep", "rg", "ag", "ack"}
_SHELL_OPERATORS = {"|", "||", "&&", ";", "&", "|&", ";;"}
_COMMAND_PREFIXES = {"sudo", "command", "exec", "env", "nice", "time", "nohup"}
_INTERRUPT_COMMANDS = {"c-c", "^c", "\x03", "ctrl+c", "ctrl-c"}


def _final_command(command: str) -> list[str]:
    """The words of the command whose exit status a shell reports: the last stage of the last
    pipeline, without leading ``VAR=value`` assignments, ``timeout N``, ``sudo`` and the like.
    Empty when the command can't be tokenized (unbalanced quotes)."""
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    stage: list[str] = []
    try:
        for token in lexer:
            stage = [] if token in _SHELL_OPERATORS else [*stage, token]
    except ValueError:
        return []
    while stage:
        head = os.path.basename(stage[0])
        if "=" in stage[0] and not stage[0].startswith("-"):
            stage = stage[1:]
        elif head == "timeout":
            stage = stage[2:]
        elif head in _COMMAND_PREFIXES:
            stage = stage[1:]
        else:
            break
    return stage


def is_search_command(command: str | None) -> bool:
    """True when the command's final program is a search (``grep``, ``rg``, ``git grep``, or
    ``xargs grep``), whose non-zero exit means it found nothing."""
    words = _final_command(command or "")
    if not words:
        return False
    program = os.path.basename(words[0])
    if program in _SEARCH_PROGRAMS:
        return True
    if program == "git" and len(words) > 1 and words[1] == "grep":
        return True
    return program == "xargs" and any(os.path.basename(w) in _SEARCH_PROGRAMS for w in words[1:])


def is_interrupt(command: str | None) -> bool:
    """True when the command is a Ctrl-C the agent sends to stop a running process."""
    return command is not None and command.strip().lower() in _INTERRUPT_COMMANDS


def nonzero_exit_convention(command: str | None, exit_code: int, output: Any) -> str | None:
    """Why a non-zero exit is not a failure, or ``None`` when it may be one.

    A shell's exit status isn't an error flag; two conventions account for every non-error
    non-zero exit in an audit of real agent runs (experiments/swebench, R2a re-audit):

    - ``"no_match"`` — a search that printed nothing: ``grep`` / ``rg`` exit 1, ``xargs grep`` 123.
    - ``"interrupted"`` — the agent itself sent Ctrl-C (exit 130) to stop a running process.
    """
    if command is None:
        return None
    if exit_code == 130 and is_interrupt(command):
        return "interrupted"
    printed = output.strip() if isinstance(output, str) else output
    if exit_code in (1, 123) and looks_empty(printed) and is_search_command(command):
        if exit_code == 1 or os.path.basename(_final_command(command)[0]) == "xargs":
            return "no_match"
    return None


def is_poll_call(call: ToolCall) -> bool:
    """True for a call that waits on something already running, so repeating it is the point:
    a tool named for waiting (``wait_shell_command``), or a call whose every input argument is
    empty (a terminal agent's ``keystrokes: ""``, Codex's ``chars: ""``) or a ``sleep`` / ``wait``
    command. Descriptive arguments (``summary``, ``description``) are not input."""
    tokens = {t.lower() for t in _NAME_TOKEN_RE.findall(call.name)}
    if tokens & _POLL_NAME_TOKENS:
        return True
    inputs = [
        v
        for k, v in call.args.items()
        if isinstance(v, str) and str(k).lower() not in _DESCRIPTIVE_ARG_KEYS
    ]
    return bool(inputs) and all(not v.strip() or _WAIT_INPUT_RE.match(v) for v in inputs)
