"""Null check: report per-column null counts for both datasets.

Informational by design: nulls are reported, not automatically a failure. A run
fails on nulls only if those nulls cause cell mismatches, which the main
comparison already captures."""

from __future__ import annotations

from typing import Any, List

from ..engines.base import Engine
from ..result import NullCheck


def run_null_check(
    engine: Engine, source: Any, target: Any, columns_source: List[str], columns_target: List[str]
) -> NullCheck:
    s = engine.null_counts(source, columns_source)
    t = engine.null_counts(target, columns_target)
    # Keep only columns that actually have nulls, to keep the report focused.
    s = {k: v for k, v in s.items() if v}
    t = {k: v for k, v in t.items() if v}
    return NullCheck(enabled=True, source_nulls=s, target_nulls=t)
