"""Provenance graph + derivability test (spec §II.4, R3; learning-doc 02 §2).

Provenance answers: *where could this argument value have come from?* Applied to an agent trace,
the question is whether a value the agent put in a tool call is **traceable** to something the
agent actually observed — the user's input, a prior tool result, the system prompt, a constant —
possibly through a recognized transform (reformatting, substring extraction, concatenation). A
value with no such path is **unexplained**: nothing in the run accounts for it, the signature of
a fabricated argument.

Two honesty constraints from learning-doc 02 §2 shape the design:

1. **Operate on normalized values and named operations, not raw containment** — otherwise the
   check both over-trusts (a comma-free reformat looks absent) and under-trusts. The transform
   set is deliberately **bounded** (exact / digit-reformat / the same number written differently /
   substring / concatenation) to what a trace plausibly exhibits; arbitrary arithmetic is excluded
   to avoid numerology (a spurious match found by combining unrelated numbers). For the same
   reason a value's digits must come from *one* number in the text: ``ORD-58213`` is not derived
   from a total of 58 and a quantity of 213.
2. **``generated`` is a legitimate source** — a value the *model* produced (an assistant thought)
   is not provenance for grounding an argument; only ``user`` / ``system`` / ``tool`` sources are
   added, so laundering a value through the model's own prior output never makes it "derivable."

A graph is built once and grown step by step (:meth:`ProvenanceGraph.observe`), and its values are
indexed, so checking every call of a long trace stays close to linear in the trace's size.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from functools import lru_cache
from typing import Any

from tracelint.trace import Message, Role, Step, ToolResult
from tracelint.valueutil import compact, digits, iter_scalars, normalize, number, numbers_in


class SourceType(str, Enum):
    """Where a tracked value originated (spec §II.4)."""

    USER = "user"
    TOOL = "tool"
    SYSTEM = "system"
    CONSTANT = "constant"
    GENERATED = "generated"
    DERIVED = "derived"


@dataclass(frozen=True)
class ProvenanceNode:
    """A value available to the agent, tagged with where it came from."""

    value: Any
    source_type: SourceType
    source_step: int
    source_path: str = ""


@dataclass
class Derivability:
    """The result of testing whether a value is traceable to non-generated provenance."""

    derivable: bool
    operation: str | None = None  # exact | digits | number | substring | concat | trivial
    source_step: int | None = None
    source_type: str | None = None


# The joins step 5 of ``derive`` recognizes between two tracked values.
_SEPARATORS = ("", " ", "-", "/", "_")

# One number as written in text: digit groups joined by a single formatting character (1,234.56;
# 2024-06-01; 555-0100; a space) or a parenthesis ((555) 123-4567). Numbers separated by anything
# else, or on separate lines, are separate numbers.
_DIGIT_RUN = re.compile(r"\d+(?:(?:[,./\- ]|\) ?\(?| \(|\()\d+)*")


@dataclass
class _TextBlob:
    norm: str
    digit_runs: str  # the digits of each number in the text, "|"-separated
    source_type: SourceType
    step: int


@dataclass
class _Index:
    """Lookups over a graph's non-generated nodes, so deriving a value is not a scan."""

    size: int = 0  # how many of the graph's nodes are indexed
    by_norm: dict[str, tuple[int, ProvenanceNode]] = field(default_factory=dict)  # first + position
    by_digits: dict[str, ProvenanceNode] = field(default_factory=dict)
    by_key: dict[str, list[ProvenanceNode]] = field(default_factory=dict)
    numbers: dict[Decimal, tuple[int, SourceType]] = field(default_factory=dict)
    lengths: set[int] = field(default_factory=set)  # of the normalized values


@dataclass
class ProvenanceGraph:
    """The set of values + text an agent had observed up to some step, with a derivability test."""

    nodes: list[ProvenanceNode] = field(default_factory=list)
    _texts: list[_TextBlob] = field(default_factory=list)
    _text_numbers: dict[Decimal, tuple[int, SourceType]] = field(
        default_factory=dict, repr=False, compare=False
    )
    _idx: _Index = field(default_factory=_Index, repr=False, compare=False)

    def add_value(self, value: Any, source_type: SourceType, step: int, path: str = "") -> None:
        self.nodes.append(ProvenanceNode(value, source_type, step, path))

    def add_text(self, text: str, source_type: SourceType, step: int) -> None:
        raw = str(text)
        runs = "|".join(digits(run) for run in _DIGIT_RUN.findall(raw))
        self._texts.append(_TextBlob(normalize(raw), runs, source_type, step))
        if source_type is not SourceType.GENERATED:
            for found in numbers_in(raw):
                _keep_first(self._text_numbers, found, step, source_type)

    def observe(self, step: Step) -> None:
        """Add what ``step`` showed the agent: a user or system message, or a tool result.

        Assistant turns (the model's own thoughts) and tool-call arguments are never sources, so a
        value can never become derivable merely because the model emitted it earlier.
        """
        if isinstance(step, Message):
            if step.role is Role.USER:
                self.add_text(step.content, SourceType.USER, step.index)
            elif step.role is Role.SYSTEM:
                self.add_text(step.content, SourceType.SYSTEM, step.index)
            # Assistant / tool-role messages are generated context, not provenance sources.
        elif isinstance(step, ToolResult):
            self.add_text(_stringify(step.content), SourceType.TOOL, step.index)
            if step.error:
                self.add_text(str(step.error), SourceType.TOOL, step.index)
            for scalar in iter_scalars(step.content):
                self.add_value(scalar, SourceType.TOOL, step.index)

    def derive(self, value: Any, *, strict: bool = False) -> Derivability:
        """Test whether ``value`` traces to some non-generated source via a bounded transform.

        ``strict`` asks only whether this value itself was available, give or take case and
        separators (``A-100`` for ``A100``): equal to an observed value, or in observed text as a
        whole token rather than part of a longer one (``1200`` is not in ``12000``). It skips the
        digit and concatenation matches, which say a value *could be assembled* from what was seen.
        """
        vn = normalize(value)
        if len(vn) < 2:
            # Too short/trivial to call a fabrication (a units flag, a single digit).
            return Derivability(True, "trivial")
        if strict:
            return self._available(value)
        index = self._index()
        vd = digits(value)

        # 1. Exact / normalized match to a tracked value.
        if vn in index.by_norm:
            return _from_node("exact", index.by_norm[vn][1])

        # 2. Digit-reformat match (1,234.56 vs 1234.56; ids with separators).
        if len(vd) >= 2 and vd in index.by_digits:
            return _from_node("digits", index.by_digits[vd])

        # 3. Substring of a non-generated text blob (extraction from a message/result).
        if len(vn) >= 3:
            for blob in self._texts:
                if blob.source_type is not SourceType.GENERATED and vn in blob.norm:
                    return Derivability(True, "substring", blob.step, blob.source_type.value)
        # ...or its digits inside one number of the text (a phone number written with separators).
        if len(vd) >= 3:
            for blob in self._texts:
                if blob.source_type is not SourceType.GENERATED and vd in blob.digit_runs:
                    return Derivability(True, "digits", blob.step, blob.source_type.value)

        # 4. The same number written differently (1200.0 vs "$1,200").
        amount = number(value)
        if amount is not None:
            found = _earliest(index.numbers.get(amount), self._text_numbers.get(amount))
            if found is not None:
                return Derivability(True, "number", found[0], found[1].value)

        # 5. Concatenation of two tracked values (bounded — no arbitrary arithmetic): ``vn`` split
        # into an observed value, a separator, and another observed value.
        first: tuple[int, ProvenanceNode] | None = None
        for length in index.lengths:
            if not 0 < length < len(vn):
                continue
            head = index.by_norm.get(vn[:length])
            if head is None or (first is not None and head[0] >= first[0]):
                continue
            if any(_joins(vn, length, sep, index) for sep in _SEPARATORS):
                first = head
        if first is not None:
            return _from_node("concat", first[1])

        return Derivability(False)

    def sources_of(self, value: Any) -> list[int] | None:
        """Every step where ``value`` itself was observed, in order: what ``derive(value,
        strict=True)`` looks for, all of it. ``None`` for a value too trivial to trace."""
        key = compact(value)
        if len(normalize(value)) < 2 or not key:
            return None
        return sorted(self._hits(key))

    def _available(self, value: Any) -> Derivability:
        """``derive(strict=True)``: was this value itself observed, ignoring case and separators?"""
        key = compact(value)
        if not key:
            return Derivability(True, "trivial")  # separators only
        hits = self._hits(key)
        if not hits:
            return Derivability(False)
        step = min(hits)
        operation, source_type = hits[step]
        return Derivability(True, operation, step, source_type.value)

    def _hits(self, key: str) -> dict[int, tuple[str, SourceType]]:
        """Step -> (operation, source type) for each source holding the value whose letters and
        digits are ``key``: an equal value, or a whole token of text."""
        hits = {
            node.source_step: ("exact", node.source_type)
            for node in self._index().by_key.get(key, [])
        }
        token = _token(key)
        for blob in self._texts:
            if blob.source_type is SourceType.GENERATED or blob.step in hits:
                continue
            if token.search(blob.norm):
                hits[blob.step] = ("substring", blob.source_type)
        return hits

    def _index(self) -> _Index:
        """The lookups, extended to nodes added since the last call (``nodes`` may also have been
        appended to directly)."""
        index = self._idx
        if index.size > len(self.nodes):  # the list was replaced: start over
            index = self._idx = _Index()
        for position in range(index.size, len(self.nodes)):
            node = self.nodes[position]
            if node.source_type is SourceType.GENERATED:
                continue
            norm = normalize(node.value)
            index.by_norm.setdefault(norm, (position, node))
            index.lengths.add(len(norm))
            node_digits = digits(node.value)
            if len(node_digits) >= 2:
                index.by_digits.setdefault(node_digits, node)
            index.by_key.setdefault(compact(node.value), []).append(node)
            amount = number(node.value)
            if amount is not None:
                _keep_first(index.numbers, amount, node.source_step, node.source_type)
        index.size = len(self.nodes)
        return index


def _from_node(operation: str, node: ProvenanceNode) -> Derivability:
    return Derivability(True, operation, node.source_step, node.source_type.value)


def _joins(vn: str, length: int, sep: str, index: _Index) -> bool:
    """Whether the rest of ``vn`` after its first ``length`` characters is ``sep`` followed by an
    observed value."""
    tail = vn[length:]
    if not tail.startswith(sep):
        return False
    if not sep and vn[length - 1].isdigit() and tail[:1].isdigit():
        return False  # two numbers run together (58, 213 -> 58213) read as a different number
    return tail[len(sep) :] in index.by_norm


def _keep_first(
    seen: dict[Decimal, tuple[int, SourceType]], amount: Decimal, step: int, source: SourceType
) -> None:
    """Record where ``amount`` was observed, keeping the earliest step."""
    if amount not in seen or step < seen[amount][0]:
        seen[amount] = (step, source)


def _earliest(
    *found: tuple[int, SourceType] | None,
) -> tuple[int, SourceType] | None:
    present = [f for f in found if f is not None]
    return min(present, key=lambda f: f[0]) if present else None


@lru_cache(maxsize=1024)
def _token(key: str) -> re.Pattern[str]:
    """``key``'s letters and digits in order, at most a few separators apart, and not part of a
    longer run of letters or digits."""
    return re.compile(r"(?<![^\W_])" + r"[\W_]{0,3}".join(map(re.escape, key)) + r"(?![^\W_])")


def build_provenance(steps: list[Step], up_to_index: int) -> ProvenanceGraph:
    """Build the graph of everything the agent had observed *before* ``up_to_index``.

    Only user/system messages and tool results are sources — assistant turns (the model's own
    thoughts) and prior tool-call arguments are excluded, so a value can never become derivable
    merely because the model emitted it earlier.
    """
    graph = ProvenanceGraph()
    for step in steps:
        if step.index >= up_to_index:
            break
        graph.observe(step)
    return graph


def _stringify(content: Any) -> str:
    """A result's text, one scalar per line, so a number in one field never runs into the next."""
    if isinstance(content, str):
        return content
    return "\n".join(str(s) for s in iter_scalars(content))
