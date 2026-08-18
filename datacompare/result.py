"""Normalized, JSON-serializable result dataclasses.

These types are engine-agnostic. Both the Polars and Spark engines populate the
same shapes so the report and CLI never need to know which engine ran.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

# Cap on how many example rows we retain per drill-down section so the JSON and
# HTML stay bounded even when millions of rows differ. Full counts are always
# reported separately.
DEFAULT_SAMPLE_CAP = 500


@dataclass
class Mismatch:
    """A single differing cell between source and target for one key."""

    key: Dict[str, Any]
    column: str
    source: Any
    target: Any


@dataclass
class SchemaCheck:
    enabled: bool = False
    passed: Optional[bool] = None  # None => not run
    source_only_columns: List[str] = field(default_factory=list)
    target_only_columns: List[str] = field(default_factory=list)
    type_mismatches: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class NullCheck:
    enabled: bool = False
    source_nulls: Dict[str, int] = field(default_factory=dict)
    target_nulls: Dict[str, int] = field(default_factory=dict)


@dataclass
class DuplicateCheck:
    enabled: bool = False
    passed: Optional[bool] = None  # None => not run
    source_duplicate_keys: int = 0
    target_duplicate_keys: int = 0
    source_samples: List[Dict[str, Any]] = field(default_factory=list)
    target_samples: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class Summary:
    source_rows: int = 0
    target_rows: int = 0
    matched_rows: int = 0
    mismatched_rows: int = 0  # keys present in both but with >=1 differing cell
    missing_rows: int = 0  # keys only in source
    extra_rows: int = 0  # keys only in target
    total_cell_mismatches: int = 0
    match_pct: float = 0.0  # matched_rows / max(source_rows, target_rows) * 100


@dataclass
class ComparisonResult:
    name: str
    engine: str
    data_format: str
    key_columns: List[str]
    auto_detected_keys: bool
    timestamp: str
    summary: Summary = field(default_factory=Summary)
    schema_check: SchemaCheck = field(default_factory=SchemaCheck)
    null_check: NullCheck = field(default_factory=NullCheck)
    duplicate_check: DuplicateCheck = field(default_factory=DuplicateCheck)
    # Sampled drill-down data; counts live in summary.
    mismatches: List[Mismatch] = field(default_factory=list)
    missing: List[Dict[str, Any]] = field(default_factory=list)
    extra: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    passed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _self_test() -> None:
    r = ComparisonResult(
        name="t",
        engine="polars",
        data_format="csv",
        key_columns=["id"],
        auto_detected_keys=False,
        timestamp="2026-07-24T00:00:00",
    )
    r.mismatches.append(Mismatch(key={"id": 1}, column="v", source="a", target="b"))
    d = r.to_dict()
    assert d["name"] == "t"
    assert d["mismatches"][0]["column"] == "v"
    assert d["summary"]["match_pct"] == 0.0
    print("result.py self-test: OK")


if __name__ == "__main__":
    _self_test()
