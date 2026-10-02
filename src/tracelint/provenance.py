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
   set is deliberately **bounded** (exact / digit-reformat / substring / concatenation) to what a
   trace plausibly exhibits; arbitrary arithmetic is excluded to avoid numerology (a spurious
   match found by combining unrelated numbers).
2. **``generated`` is a legitimate source** — a value the *model* produced (an assistant thought)
   is not provenance for grounding an argument; only ``user`` / ``system`` / ``tool`` sources are
   added, so laundering a value through the model's own prior output never makes it "derivable."
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from typing import Any

from tracelint.trace import Message, Role, Step, ToolResult
from tracelint.valueutil import compact, digits, iter_scalars, normalize


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
    operation: str | None = None  # exact | digits | substring | concat | trivial
    source_step: int | None = None
    source_type: str | None = None


@dataclass
class _TextBlob:
    norm: str
    digits: str
    source_type: SourceType
    step: int


@dataclass
class ProvenanceGraph:
    """The set of values + text an agent had observed up to some step, with a derivability test."""

    nodes: list[ProvenanceNode] = field(default_factory=list)
    _texts: list[_TextBlob] = field(default_factory=list)
    _key_index: dict[str, list[ProvenanceNode]] = field(
        default_factory=dict, repr=False, compare=False
    )
    _keyed: int = field(default=-1, repr=False, compare=False)  # len(nodes) when indexed

    def add_value(self, value: Any, source_type: SourceType, step: int, path: str = "") -> None:
        self.nodes.append(ProvenanceNode(value, source_type, step, path))

    def add_text(self, text: str, source_type: SourceType, step: int) -> None:
        self._texts.append(_TextBlob(normalize(text), digits(text), source_type, step))

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
        vd = digits(value)

        # 1. Exact / normalized match to a tracked value.
        for node in self.nodes:
            if node.source_type is SourceType.GENERATED:
                continue
            if normalize(node.value) == vn:
                return Derivability(True, "exact", node.source_step, node.source_type.value)

        # 2. Digit-reformat match (1,234.56 vs 1234.56; ids with separators).
        if len(vd) >= 2:
            for node in self.nodes:
                if node.source_type is SourceType.GENERATED:
                    continue
                if digits(node.value) == vd:
                    return Derivability(True, "digits", node.source_step, node.source_type.value)

        # 3. Substring of a non-generated text blob (extraction from a message/result).
        if len(vn) >= 3:
            for blob in self._texts:
                if blob.source_type is SourceType.GENERATED:
                    continue
                if vn in blob.norm:
                    return Derivability(True, "substring", blob.step, blob.source_type.value)
        if len(vd) >= 3:
            for blob in self._texts:
                if blob.source_type is SourceType.GENERATED:
                    continue
                if vd and vd in blob.digits:
                    return Derivability(True, "digits", blob.step, blob.source_type.value)

        # 4. Concatenation of two tracked values (bounded — no arbitrary arithmetic).
        vals = [n for n in self.nodes if n.source_type is not SourceType.GENERATED]
        for a in vals:
            na = normalize(a.value)
            if not na or na == vn or len(na) >= len(vn):
                continue
            for b in vals:
                nb = normalize(b.value)
                for sep in ("", " ", "-", "/", "_"):
                    if na + sep + nb == vn:
                        return Derivability(True, "concat", a.source_step, a.source_type.value)

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
            node.source_step: ("exact", node.source_type) for node in self._by_key().get(key, [])
        }
        token = _token(key)
        for blob in self._texts:
            if blob.source_type is SourceType.GENERATED or blob.step in hits:
                continue
            if token.search(blob.norm):
                hits[blob.step] = ("substring", blob.source_type)
        return hits

    def _by_key(self) -> dict[str, list[ProvenanceNode]]:
        """Non-generated nodes by their letters and digits, rebuilt when nodes have been added."""
        if self._keyed != len(self.nodes):
            index: dict[str, list[ProvenanceNode]] = {}
            for node in self.nodes:
                if node.source_type is not SourceType.GENERATED:
                    index.setdefault(compact(node.value), []).append(node)
            self._key_index, self._keyed = index, len(self.nodes)
        return self._key_index


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
        if isinstance(step, Message):
            if step.role is Role.USER:
                graph.add_text(step.content, SourceType.USER, step.index)
            elif step.role is Role.SYSTEM:
                graph.add_text(step.content, SourceType.SYSTEM, step.index)
            # Assistant / tool-role messages are generated context, not provenance sources.
        elif isinstance(step, ToolResult):
            graph.add_text(_stringify(step.content), SourceType.TOOL, step.index)
            if step.error:
                graph.add_text(str(step.error), SourceType.TOOL, step.index)
            for scalar in iter_scalars(step.content):
                graph.add_value(scalar, SourceType.TOOL, step.index)
    return graph


def _stringify(content: Any) -> str:
    if isinstance(content, str):
        return content
    return " ".join(str(s) for s in iter_scalars(content))
