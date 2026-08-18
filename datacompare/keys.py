"""Engine-agnostic automatic key-column detection.

When a test case omits `key_columns`, pick columns that uniquely identify a row.
Strategy:
  1. Consider only columns present in BOTH datasets.
  2. Prefer a single column that is fully unique and non-null in both, ranked by
     an id-like name bonus (id, key, code, *_id).
  3. If no single column qualifies, try composite keys of 2 then 3 columns drawn
     from the most-unique candidates until a combination is unique in both.
  4. If nothing is unique, fall back to all shared columns and record a warning.
"""

from __future__ import annotations

from itertools import combinations
from typing import Any, List, Tuple

from .engines.base import Engine

_ID_HINTS = ("id", "key", "code", "uuid", "guid", "pk", "number", "no")


def _name_bonus(col: str) -> int:
    low = col.lower()
    if low in _ID_HINTS:
        return 3
    if low.endswith("_id") or low.endswith("id") or low.endswith("_key"):
        return 2
    if any(h in low for h in _ID_HINTS):
        return 1
    return 0


def detect_keys(
    engine: Engine,
    source: Any,
    target: Any,
    max_composite: int = 3,
) -> Tuple[List[str], List[str]]:
    """Return (key_columns, warnings)."""
    warnings: List[str] = []
    scols = engine.columns(source)
    tcols = set(engine.columns(target))
    shared = [c for c in scols if c in tcols]
    if not shared:
        raise ValueError("source and target share no columns; cannot detect a key")

    s_rows = engine.row_count(source)
    t_rows = engine.row_count(target)

    s_unique = engine.unique_counts(source, shared)
    t_unique = engine.unique_counts(target, shared)
    s_nulls = engine.null_counts(source, shared)
    t_nulls = engine.null_counts(target, shared)

    def fully_unique(col: str) -> bool:
        return (
            s_unique.get(col, 0) == s_rows
            and t_unique.get(col, 0) == t_rows
            and s_nulls.get(col, 0) == 0
            and t_nulls.get(col, 0) == 0
        )

    # 1) Best single unique column, preferring id-like names.
    singles = [c for c in shared if fully_unique(col=c)]
    if singles:
        singles.sort(key=lambda c: (_name_bonus(c), -shared.index(c)), reverse=True)
        return [singles[0]], warnings

    # 2) Composite keys. Rank candidates by uniqueness ratio + name bonus.
    ranked = sorted(
        shared,
        key=lambda c: (
            s_unique.get(c, 0) / s_rows if s_rows else 0,
            _name_bonus(c),
        ),
        reverse=True,
    )
    # Limit the search space so this stays cheap on wide tables.
    pool = ranked[: min(len(ranked), 8)]
    for size in range(2, max_composite + 1):
        for combo in combinations(pool, size):
            cols = list(combo)
            if any(s_nulls.get(c, 0) > 0 or t_nulls.get(c, 0) > 0 for c in cols):
                continue
            if (
                engine.composite_unique_count(source, cols) == s_rows
                and engine.composite_unique_count(target, cols) == t_rows
            ):
                return cols, warnings

    # 3) Fallback: all shared columns as the key.
    warnings.append(
        "no unique key found; falling back to all shared columns as the key. "
        "Rows with identical values across all shared columns will be treated "
        "as duplicates."
    )
    return list(shared), warnings
