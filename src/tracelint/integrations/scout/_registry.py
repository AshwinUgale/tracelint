"""Entry point for Inspect's registry (``[project.entry-points.inspect_ai]`` in pyproject.toml).

Inspect imports this module to find the ``tracelint/...`` scanners — on a ``scout scan
tracelint/<name>`` lookup, and whenever it loads every extension. The entry point is installed with
tracelint itself, extra or not, so without Inspect Scout this module registers nothing rather than
fail (which Inspect would log as a warning on every run). With Scout installed, a failing import is
a real error and is not hidden.
"""

from __future__ import annotations

from importlib.util import find_spec

if find_spec("inspect_scout") is not None:
    from tracelint.integrations.scout.scanners import (  # noqa: F401 - imported to register
        malformed_arguments,
        redundant_calls,
        tool_errors,
        tool_loops,
        tracelint,
    )
