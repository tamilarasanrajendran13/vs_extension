"""Pluggable comparison engines.

Use `get_engine(name)` to obtain an engine instance. Spark is imported lazily so
the Polars path never requires Java.
"""

from __future__ import annotations

from .base import Engine, DiffResult


def get_engine(name: str) -> Engine:
    name = (name or "polars").lower()
    if name == "polars":
        from .polars_engine import PolarsEngine

        return PolarsEngine()
    if name == "spark":
        # Imported here so `import datacompare` never pulls in pyspark/Java.
        from .spark_engine import SparkEngine

        return SparkEngine()
    raise ValueError("unknown engine '%s' (expected polars or spark)" % name)


__all__ = ["Engine", "DiffResult", "get_engine"]
