"""Schema check: compare column presence and normalized types between the two
datasets. Column mapping is applied to the source side first so mapped columns
line up by their target names."""

from __future__ import annotations

from typing import Any, Dict, List

from ..engines.base import Engine
from ..result import SchemaCheck


def run_schema_check(
    engine: Engine,
    source: Any,
    target: Any,
    column_mapping: Dict[str, str],
    ignore_columns: List[str],
) -> SchemaCheck:
    s_schema = engine.schema(source)
    t_schema = engine.schema(target)

    # Apply mapping to source column names so comparison is by target name.
    if column_mapping:
        s_schema = {column_mapping.get(k, k): v for k, v in s_schema.items()}

    ignore = set(ignore_columns)
    s_cols = {c for c in s_schema if c not in ignore}
    t_cols = {c for c in t_schema if c not in ignore}

    source_only = sorted(s_cols - t_cols)
    target_only = sorted(t_cols - s_cols)
    type_mismatches = []
    for c in sorted(s_cols & t_cols):
        if s_schema[c] != t_schema[c]:
            type_mismatches.append(
                {"column": c, "source_type": s_schema[c], "target_type": t_schema[c]}
            )

    passed = not source_only and not target_only and not type_mismatches
    return SchemaCheck(
        enabled=True,
        passed=passed,
        source_only_columns=source_only,
        target_only_columns=target_only,
        type_mismatches=type_mismatches,
    )
