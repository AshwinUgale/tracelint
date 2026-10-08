"""Non-zero exits that aren't failures, and where R2a's text heuristic looks for an error.

From the R2a re-audit of real agent runs (experiments/swebench): every exit-code false positive was
a search that found nothing or a Ctrl-C the agent sent, and every text-heuristic false positive was
an error word inside a viewed file rather than an error the tool reported.
"""

from __future__ import annotations

import pytest

from tracelint import ToolRegistry, build_trace, lint_trace
from tracelint.rules import ToolErrorEventRule
from tracelint.signatures import is_search_command, nonzero_exit_convention
from tracelint.trace import ResultStatus, ToolCall, ToolResult


@pytest.mark.parametrize(
    ("command", "search"),
    [
        ("grep -n foo a.py", True),
        ('cd /repo && grep -n -A 5 "get\\|set" query.py', True),  # a quoted \| is not a pipe
        ('grep -n "def save" a.py | grep -v "class"', True),  # the last stage decides
        ('find . -name "*.pyx" | xargs grep -l "dmp"', True),
        ("rg -n sys.path tests/", True),
        ("git grep -n foo", True),
        ("timeout 60 grep foo bar", True),
        ("LC_ALL=C grep foo bar", True),
        ("grep foo a.py | head -5", False),  # head's status, not grep's
        ("cd x && pytest -q 2>&1", False),
        ('python -c "print(1 | 2)"', False),
        ("grep 'unbalanced", False),  # can't be tokenized: not assumed a search
    ],
)
def test_is_search_command(command, search):
    assert is_search_command(command) is search


@pytest.mark.parametrize(
    ("command", "code", "output", "convention"),
    [
        ("grep -n foo a.py", 1, "", "no_match"),
        ('find . | xargs grep -l "dmp"', 123, "", "no_match"),
        ("C-c", 130, "^C", "interrupted"),
        ("grep -n foo a.py", 1, "a.py:3: foo", None),  # printed a match: not a no-match
        ("grep -n foo missing.py", 2, "", None),  # 2 is grep's own error
        ("grep -n foo a.py", 123, "", None),  # 123 only means no-match for xargs
        ("pytest -q", 1, "", None),
        ("python run.py", 130, "", None),  # interrupted by something other than the agent
        (None, 1, "", None),
    ],
)
def test_nonzero_exit_convention(command, code, output, convention):
    assert nonzero_exit_convention(command, code, output) == convention


def _r2a(content, command="cat notes.txt", tool="execute_bash"):
    steps = [
        ToolCall("c1", tool, {"command": command}),
        ToolResult("c1", content, status=ResultStatus.UNKNOWN),
    ]
    return lint_trace(build_trace("r", steps), [ToolErrorEventRule()], ToolRegistry())


@pytest.mark.parametrize(
    "content",
    [
        # A viewed file's own source, numbered like `cat -n`: was every heuristic false positive.
        "Here's the result of running `cat -n` on a.py:\n   12\t    raise ValueError('bad')\n"
        "   13\texcept KeyError:",
        "def check(x):\n    if x < 0:\n        raise ValueError('negative')\n    return x",
        "The build failed last week; see the notes below.",  # an error word inside prose
        "Docs: errors are reported via the Error object.",
        # a test whose name ends in "error" (found on the held-out audit sample)
        "test_connection_error (__main__.RequestsTestCase) ... ok\ntest_get (x.Tests) ... ok",
    ],
)
def test_an_error_word_inside_other_text_is_not_a_reported_error(content):
    assert _r2a(content).active_findings == []


@pytest.mark.parametrize(
    "content",
    [
        "ERROR: Invalid `path` parameter: /repo/a.txt does not exist.",
        "Traceback (most recent call last):\n  File \"x.py\", line 1\nValueError: bad",
        "New Terminal Output:\nroot@box:/app# pytest\nbash: pytest: command not found\n$",
        "ls: cannot access 'src': No such file or directory",
        "fatal: not a git repository (or any of the parent directories): .git",
        "make: *** No targets specified and no makefile found.  Stop.",
        "<tool_use_error>InputValidationError: the required parameter `todos` is missing",
        "ModuleNotFoundError: No module named 'psycopg2'",
        "Failed to connect to upstream",
        "[rank0]: TypeError: cannot unpack non-iterable NoneType object",  # a log-tagged line
        "[12:00:01] ERROR: worker crashed",
    ],
)
def test_an_error_the_tool_reports_at_a_line_start_is_a_candidate(content):
    (finding,) = _r2a(content).active_findings
    assert finding.evidence["signal"] == "exception_text"


def test_the_output_of_a_deliberate_ctrl_c_is_not_scanned():
    # The stopped command's log usually ends in a KeyboardInterrupt traceback.
    log = "running tests...\nTraceback (most recent call last):\nKeyboardInterrupt"
    assert _r2a(log, command="C-c").active_findings == []
    assert _r2a(log, command="pytest").active_findings != []


def test_an_empty_search_result_is_not_an_error():
    assert _r2a("", command="grep -n foo a.py").active_findings == []
    (finding,) = _r2a("", command="cat a.py").active_findings  # other empty results still are
    assert finding.evidence["signal"] == "empty_result"
