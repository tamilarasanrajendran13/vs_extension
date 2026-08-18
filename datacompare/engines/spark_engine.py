"""Spark comparison engine (lazy-imported; requires Java + pyspark).

Mirrors PolarsEngine's primitives against a Spark DataFrame so a test case can
switch engines with a single YAML field. A SparkSession is created on first use
and reused. If Java/pyspark are missing, construction raises with a clear
message rather than failing deep inside a comparison.
"""

from __future__ import annotations

from functools import reduce
from typing import Any, Dict, List, Tuple

from ..config import Options, Source
from .base import Engine, DiffResult

_SESSION = None


def _get_session():
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    try:
        from pyspark.sql import SparkSession
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "Spark engine requires pyspark. Install it and a Java runtime, "
            "or use engine: polars. Original error: %s" % exc
        )
    try:
        _SESSION = (
            SparkSession.builder.appName("datacompare")
            .master("local[*]")
            .config("spark.sql.shuffle.partitions", "8")
            .config("spark.ui.enabled", "false")
            .getOrCreate()
        )
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "could not start a local Spark session (is Java installed?): %s" % exc
        )
    _SESSION.sparkContext.setLogLevel("ERROR")
    return _SESSION


def _normalize_type(spark_type: str) -> str:
    s = spark_type.lower()
    if "int" in s or "long" in s or "short" in s or "byte" in s:
        return "int"
    if "double" in s or "float" in s or "decimal" in s:
        return "float"
    if "bool" in s:
        return "bool"
    if "timestamp" in s:
        return "datetime"
    if "date" in s:
        return "date"
    if "string" in s:
        return "str"
    return s


class SparkEngine(Engine):
    name = "spark"

    def __init__(self) -> None:
        self.spark = _get_session()
        from pyspark.sql import functions as F

        self._F = F

    # --- loading -----------------------------------------------------------
    def read_csv(self, source: Source) -> Any:
        return (
            self.spark.read.option("header", str(source.has_header).lower())
            .option("sep", source.delimiter)
            .option("inferSchema", "true")
            .option("encoding", source.encoding)
            .csv(source.path)
        )

    def read_rows(self, rows: List[Dict[str, Any]]) -> Any:
        from pyspark.sql.types import StructType, StructField, StringType

        cols: List[str] = []
        seen = set()
        for r in rows:
            for k in r.keys():
                if k not in seen:
                    seen.add(k)
                    cols.append(k)
        schema = StructType([StructField(c, StringType(), True) for c in cols])
        data = [
            tuple(None if r.get(c) is None else str(r.get(c)) for c in cols)
            for r in rows
        ]
        return self.spark.createDataFrame(data, schema)

    # --- introspection -----------------------------------------------------
    def columns(self, frame: Any) -> List[str]:
        return list(frame.columns)

    def schema(self, frame: Any) -> Dict[str, str]:
        return {f.name: _normalize_type(f.dataType.simpleString()) for f in frame.schema}

    def row_count(self, frame: Any) -> int:
        return int(frame.count())

    def null_counts(self, frame: Any, columns: List[str]) -> Dict[str, int]:
        F = self._F
        cols = [c for c in columns if c in frame.columns]
        if not cols:
            return {}
        agg = frame.select(
            [F.sum(F.col(c).isNull().cast("int")).alias(c) for c in cols]
        ).collect()[0]
        return {c: int(agg[c] or 0) for c in cols}

    def unique_counts(self, frame: Any, columns: List[str]) -> Dict[str, int]:
        F = self._F
        cols = [c for c in columns if c in frame.columns]
        if not cols:
            return {}
        agg = frame.agg(
            *[F.countDistinct(F.col(c)).alias(c) for c in cols]
        ).collect()[0]
        return {c: int(agg[c] or 0) for c in cols}

    def composite_unique_count(self, frame: Any, columns: List[str]) -> int:
        return int(frame.select(columns).distinct().count())

    # --- comparison --------------------------------------------------------
    def duplicate_keys(
        self, frame: Any, keys: List[str], sample_cap: int
    ) -> Tuple[int, List[Dict[str, Any]]]:
        F = self._F
        grouped = frame.groupBy(keys).count()
        dups = grouped.filter(F.col("count") > 1)
        count = int(dups.count())
        samples = [r.asDict() for r in dups.limit(sample_cap).collect()]
        return count, samples

    def rename_columns(self, frame: Any, mapping: Dict[str, str]) -> Any:
        for src, dst in mapping.items():
            if src in frame.columns:
                frame = frame.withColumnRenamed(src, dst)
        return frame

    def _norm(self, colname: str, options: Options):
        F = self._F
        e = F.col(colname).cast("string")
        if options.trim_strings:
            e = F.trim(e)
        if options.case_insensitive:
            e = F.lower(e)
        return e.alias(colname)

    def join_and_diff(
        self,
        source: Any,
        target: Any,
        keys: List[str],
        compare_columns: List[str],
        options: Options,
    ) -> DiffResult:
        F = self._F
        needed_s = [c for c in (list(keys) + list(compare_columns)) if c in source.columns]
        needed_t = [c for c in (list(keys) + list(compare_columns)) if c in target.columns]
        s = source.select([self._norm(c, options) for c in needed_s])
        t = target.select([self._norm(c, options) for c in needed_t])

        cap = options.sample_cap
        result = DiffResult()

        missing = s.join(t, keys, "left_anti")
        extra = t.join(s, keys, "left_anti")
        result.missing_count = int(missing.count())
        result.extra_count = int(extra.count())
        result.missing = [
            {k: r[k] for k in keys} for r in missing.select(keys).limit(cap).collect()
        ]
        result.extra = [
            {k: r[k] for k in keys} for r in extra.select(keys).limit(cap).collect()
        ]

        # Rename target compare columns to avoid ambiguity after the join.
        t2 = t
        for c in compare_columns:
            if c in t2.columns:
                t2 = t2.withColumnRenamed(c, c + "__t")
        both = s.join(t2, keys, "inner")

        usable = [
            c for c in compare_columns if c in both.columns and (c + "__t") in both.columns
        ]
        if not usable:
            result.matched_rows = int(both.count())
            return result

        tol = options.number_tolerance
        differ_cols: List[str] = []
        for c in usable:
            sc = F.col(c)
            tc = F.col(c + "__t")
            both_null = sc.isNull() & tc.isNull()
            one_null = sc.isNull() != tc.isNull()
            str_eq = F.coalesce(sc == tc, F.lit(False))
            if tol > 0:
                sf = sc.cast("double")
                tf = tc.cast("double")
                num_eq = F.coalesce(
                    sf.isNotNull() & tf.isNotNull() & (F.abs(sf - tf) <= F.lit(tol)),
                    F.lit(False),
                )
                eq = str_eq | num_eq
            else:
                eq = str_eq
            equal = both_null | ((~one_null) & eq)
            flag = "__d_" + c
            both = both.withColumn(flag, ~equal)
            differ_cols.append(flag)

        any_differ = reduce(lambda a, b: a | b, [F.col(f) for f in differ_cols])
        both = both.withColumn("__any", any_differ).cache()

        total = int(both.count())
        result.mismatched_rows = int(both.filter(F.col("__any")).count())
        result.matched_rows = total - result.mismatched_rows

        total_cells = 0
        samples: List[Dict[str, Any]] = []
        for c in usable:
            flag = "__d_" + c
            diffed = both.filter(F.col(flag))
            n = int(diffed.count())
            total_cells += n
            if n and len(samples) < cap:
                rows = diffed.limit(cap - len(samples)).collect()
                for r in rows:
                    samples.append(
                        {
                            "key": {k: r[k] for k in keys},
                            "column": c,
                            "source": r[c],
                            "target": r[c + "__t"],
                        }
                    )
        result.total_cell_mismatches = total_cells
        result.mismatches = samples
        both.unpersist()
        return result
