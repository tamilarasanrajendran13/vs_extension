"""Duplicate-key check: detect key values that occur more than once.

Duplicate keys make the row join ambiguous, so under the strict pass rule a run
with duplicate keys on either side FAILS. This check both reports and gates."""

from __future__ import annotations

from typing import Any, List

from ..engines.base import Engine
from ..result import DuplicateCheck


def run_duplicate_check(
    engine: Engine, source: Any, target: Any, keys: List[str], sample_cap: int
) -> DuplicateCheck:
    s_count, s_samples = engine.duplicate_keys(source, keys, sample_cap)
    t_count, t_samples = engine.duplicate_keys(target, keys, sample_cap)
    passed = s_count == 0 and t_count == 0
    return DuplicateCheck(
        enabled=True,
        passed=passed,
        source_duplicate_keys=s_count,
        target_duplicate_keys=t_count,
        source_samples=s_samples,
        target_samples=t_samples,
    )
