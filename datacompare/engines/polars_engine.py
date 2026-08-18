"""Polars comparison engine (default; pure Python, no Java).

Data is read with type inference so the schema check can report real types, then
cast to string for value comparison. Comparing textual representations is the
robust default for file-to-file diffing where source and target may infer
slightly different types; numeric tolerance re-introduces float semantics when
requested.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import polars as pl

from ..config import Options, Source
from .base import Engine, DiffResult


def _normalize_type(dtype: pl.DataType) -> str:
    s = str(dtype).lower()
    if "int" in s:
        return "int"
    if "float" in s or "decimal" in s:
        return "float"
    if "bool" in s:
        return "bool"
    if "datetime" in s:
        return "datetime"
    if "date" in s:
        return "date"
    if "str" in s or "utf8" in s:
        return "str"
    return s


def _norm_exprs(columns: List[str], options: Options) -> List[pl.Expr]:
    """Cast to string and apply trim/case normalization for comparison."""
    out = []
    for c in columns:
        e = pl.col(c).cast(pl.Utf8, strict=False)
        if options.trim_strings:
            e = e.str.strip_chars()
        if options.case_insensitive:
            e = e.str.to_lowercase()
        out.append(e.alias(c))
    return out


class PolarsEngine(Engine):
    name = "polars"

    # --- loading -----------------------------------------------------------
    def read_csv(self, source: Source) -> Any:
        return pl.read_csv(
            source.path,
            separator=source.delimiter,
            has_header=source.has_header,
            infer_schema_length=1000,
            encoding=source.encoding if source.encoding == "utf8" else "utf8-lossy",
        )

    def read_rows(self, rows: List[Dict[str, Any]]) -> Any:
        if not rows:
            return pl.DataFrame()
        # Union of keys so ragged records line up into a rectangular frame.
        cols: List[str] = []
        seen = set()
        for r in rows:
            for k in r.keys():
                if k not in seen:
                    seen.add(k)
                    cols.append(k)
        data = {c: [r.get(c) for r in rows] for c in cols}
        return pl.DataFrame(data, infer_schema_length=None)

    # --- introspection -----------------------------------------------------
    def columns(self, frame: Any) -> List[str]:
        return list(frame.columns)

    def schema(self, frame: Any) -> Dict[str, str]:
        return {name: _normalize_type(dt) for name, dt in frame.schema.items()}

    def row_count(self, frame: Any) -> int:
        return int(frame.height)

    def null_counts(self, frame: Any, columns: List[str]) -> Dict[str, int]:
        cols = [c for c in columns if c in frame.columns]
        if not cols:
            return {}
        nc = frame.select([pl.col(c).null_count().alias(c) for c in cols])
        row = nc.row(0, named=True)
        return {c: int(row[c]) for c in cols}

    def unique_counts(self, frame: Any, columns: List[str]) -> Dict[str, int]:
        cols = [c for c in columns if c in frame.columns]
        if not cols:
            return {}
        uc = frame.select([pl.col(c).n_unique().alias(c) for c in cols])
        row = uc.row(0, named=True)
        return {c: int(row[c]) for c in cols}

    def composite_unique_count(self, frame: Any, columns: List[str]) -> int:
        return int(frame.select(columns).unique().height)

    # --- comparison --------------------------------------------------------
    def duplicate_keys(
        self, frame: Any, keys: List[str], sample_cap: int
    ) -> Tuple[int, List[Dict[str, Any]]]:
        grouped = frame.group_by(keys).len()
        dups = grouped.filter(pl.col("len") > 1)
        count = int(dups.height)
        samples = dups.head(sample_cap).to_dicts()
        return count, samples

    def rename_columns(self, frame: Any, mapping: Dict[str, str]) -> Any:
        actual = {k: v for k, v in mapping.items() if k in frame.columns}
        return frame.rename(actual) if actual else frame

    def join_and_diff(
        self,
        source: Any,
        target: Any,
        keys: List[str],
        compare_columns: List[str],
        options: Options,
    ) -> DiffResult:
        needed = list(keys) + list(compare_columns)
        s = source.select([c for c in needed if c in source.columns]).with_columns(
            _norm_exprs([c for c in needed if c in source.columns], options)
        )
        t = target.select([c for c in needed if c in target.columns]).with_columns(
            _norm_exprs([c for c in needed if c in target.columns], options)
        )

        cap = options.sample_cap
        result = DiffResult()

        # Keys only in source / only in target via anti-joins.
        missing = s.join(t, on=keys, how="anti")
        extra = t.join(s, on=keys, how="anti")
        result.missing_count = int(missing.height)
        result.extra_count = int(extra.height)
        result.missing = missing.select(keys).head(cap).to_dicts()
        result.extra = extra.select(keys).head(cap).to_dicts()

        # Keys in both: inner join with suffix, then compare each column.
        both = s.join(t, on=keys, how="inner", suffix="__t")
        if both.height == 0 or not compare_columns:
            result.matched_rows = int(both.height)
            return result

        tol = options.number_tolerance
        differ_flags: List[str] = []
        for c in compare_columns:
            tcol = c + "__t"
            if c not in both.columns or tcol not in both.columns:
                continue
            sc = pl.col(c)
            tc = pl.col(tcol)
            both_null = sc.is_null() & tc.is_null()
            one_null = sc.is_null() ^ tc.is_null()
            str_eq = (sc == tc).fill_null(False)
            if tol > 0:
                sf = sc.cast(pl.Float64, strict=False)
                tf = tc.cast(pl.Float64, strict=False)
                num_eq = (
                    sf.is_not_null() & tf.is_not_null() & ((sf - tf).abs() <= tol)
                ).fill_null(False)
                eq = str_eq | num_eq
            else:
                eq = str_eq
            equal = both_null | ((~one_null) & eq)
            differ = (~equal).alias("__d_" + c)
            differ_flags.append("__d_" + c)
            both = both.with_columns(differ)

        if not differ_flags:
            result.matched_rows = int(both.height)
            return result

        any_differ = pl.any_horizontal([pl.col(f) for f in differ_flags]).alias("__any")
        both = both.with_columns(any_differ)

        result.mismatched_rows = int(both.filter(pl.col("__any")).height)
        result.matched_rows = int(both.height) - result.mismatched_rows

        # Cell-level counts and samples per column.
        total_cells = 0
        samples: List[Dict[str, Any]] = []
        for c in compare_columns:
            flag = "__d_" + c
            if flag not in both.columns:
                continue
            diffed = both.filter(pl.col(flag))
            n = int(diffed.height)
            total_cells += n
            if n and len(samples) < cap:
                take = diffed.head(cap - len(samples))
                for row in take.iter_rows(named=True):
                    samples.append(
                        {
                            "key": {k: row[k] for k in keys},
                            "column": c,
                            "source": row[c],
                            "target": row[c + "__t"],
                        }
                    )
        result.total_cell_mismatches = total_cells
        result.mismatches = samples
        return result
