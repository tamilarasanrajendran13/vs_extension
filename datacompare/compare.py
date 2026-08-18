"""Comparison orchestrator.

Loads both datasets through the selected engine, applies column mapping, detects
keys if needed, runs the enabled checks, diffs the rows, and assembles a
normalized ComparisonResult with the strict pass/fail verdict.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List

from .config import TestCase
from .engines import get_engine
from .readers import read_source
from .keys import detect_keys
from .checks import run_schema_check, run_null_check, run_duplicate_check
from .result import ComparisonResult, Summary, Mismatch, SchemaCheck, NullCheck, DuplicateCheck


def _validate_keys(engine, source, target, keys: List[str]) -> None:
    scols = set(engine.columns(source))
    tcols = set(engine.columns(target))
    missing_s = [k for k in keys if k not in scols]
    missing_t = [k for k in keys if k not in tcols]
    if missing_s or missing_t:
        raise ValueError(
            "key columns missing (source: %s, target: %s). "
            "Note: column_mapping renames source columns to their target names, "
            "so key_columns should use the target names."
            % (missing_s, missing_t)
        )


def run_comparison(tc: TestCase) -> ComparisonResult:
    engine = get_engine(tc.engine)
    source = read_source(engine, tc.data_format, tc.source)
    target = read_source(engine, tc.data_format, tc.target)

    # Rename source columns to their target names once; everything downstream
    # then speaks a single set of column names.
    if tc.options.column_mapping:
        source = engine.rename_columns(source, tc.options.column_mapping)

    warnings: List[str] = []

    # Keys: explicit or auto-detected.
    if tc.key_columns:
        _validate_keys(engine, source, target, tc.key_columns)
        keys = list(tc.key_columns)
        auto = False
    else:
        keys, kw = detect_keys(engine, source, target)
        warnings.extend(kw)
        auto = True

    scols = engine.columns(source)
    tset = set(engine.columns(target))
    shared = [c for c in scols if c in tset]
    ignore = set(tc.options.ignore_columns)
    compare_columns = [c for c in shared if c not in keys and c not in ignore]

    # Optional checks.
    schema_res = SchemaCheck()
    if tc.options.schema_check:
        schema_res = run_schema_check(engine, source, target, {}, tc.options.ignore_columns)

    null_res = NullCheck()
    if tc.options.null_check:
        null_res = run_null_check(
            engine, source, target, engine.columns(source), engine.columns(target)
        )

    dup_res = DuplicateCheck()
    if tc.options.duplicate_check:
        dup_res = run_duplicate_check(engine, source, target, keys, tc.options.sample_cap)

    # Row diff.
    diff = engine.join_and_diff(source, target, keys, compare_columns, tc.options)

    s_rows = engine.row_count(source)
    t_rows = engine.row_count(target)
    denom = max(s_rows, t_rows) or 1
    match_pct = round(diff.matched_rows / denom * 100.0, 4)

    summary = Summary(
        source_rows=s_rows,
        target_rows=t_rows,
        matched_rows=diff.matched_rows,
        mismatched_rows=diff.mismatched_rows,
        missing_rows=diff.missing_count,
        extra_rows=diff.extra_count,
        total_cell_mismatches=diff.total_cell_mismatches,
        match_pct=match_pct,
    )

    # Strict pass rule.
    passed = (
        diff.mismatched_rows == 0
        and diff.missing_count == 0
        and diff.extra_count == 0
    )
    if tc.options.schema_check and schema_res.passed is False:
        passed = False
    if tc.options.duplicate_check and dup_res.passed is False:
        passed = False

    result = ComparisonResult(
        name=tc.name,
        engine=tc.engine,
        data_format=tc.data_format,
        key_columns=keys,
        auto_detected_keys=auto,
        timestamp=datetime.now().isoformat(timespec="seconds"),
        summary=summary,
        schema_check=schema_res,
        null_check=null_res,
        duplicate_check=dup_res,
        mismatches=[
            Mismatch(key=m["key"], column=m["column"], source=m["source"], target=m["target"])
            for m in diff.mismatches
        ],
        missing=diff.missing,
        extra=diff.extra,
        warnings=warnings,
        passed=passed,
    )
    return result
