"""Engine abstract base class.

An engine wraps one dataframe backend (Polars or Spark) and exposes the small
set of primitives the rest of datacompare needs. Comparison orchestration,
checks, key detection and reporting are all engine-agnostic and speak only to
this interface. Frames are opaque to callers: pass them back to the same engine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from ..config import Options, Source


@dataclass
class DiffResult:
    """Outcome of joining source and target on the key columns.

    All list fields are already capped to Options.sample_cap; the *_count fields
    carry the true totals.
    """

    matched_rows: int = 0
    mismatched_rows: int = 0  # keys in both with >=1 differing compared cell
    missing_count: int = 0  # keys only in source
    extra_count: int = 0  # keys only in target
    total_cell_mismatches: int = 0
    # Sampled detail. Each mismatch: {"key": {...}, "column": str,
    # "source": Any, "target": Any}
    mismatches: List[Dict[str, Any]] = field(default_factory=list)
    missing: List[Dict[str, Any]] = field(default_factory=list)  # source-only key rows
    extra: List[Dict[str, Any]] = field(default_factory=list)  # target-only key rows


class Engine(ABC):
    """Backend interface. Frame type is engine-specific and opaque to callers."""

    name = "base"

    # --- loading -----------------------------------------------------------
    @abstractmethod
    def read_csv(self, source: Source) -> Any:
        """Read a CSV file into a native frame using the source's reader opts."""

    @abstractmethod
    def read_rows(self, rows: List[Dict[str, Any]]) -> Any:
        """Build a native frame from a list of uniform dict rows.

        Used by the XML reader after it flattens records to dicts.
        """

    # --- introspection -----------------------------------------------------
    @abstractmethod
    def columns(self, frame: Any) -> List[str]:
        """Ordered column names."""

    @abstractmethod
    def schema(self, frame: Any) -> Dict[str, str]:
        """Map of column name -> normalized type name (e.g. 'int', 'str')."""

    @abstractmethod
    def row_count(self, frame: Any) -> int:
        ...

    @abstractmethod
    def null_counts(self, frame: Any, columns: List[str]) -> Dict[str, int]:
        """Per-column null count for the given columns."""

    @abstractmethod
    def unique_counts(self, frame: Any, columns: List[str]) -> Dict[str, int]:
        """Per-column distinct-value count (used by key auto-detection)."""

    @abstractmethod
    def composite_unique_count(self, frame: Any, columns: List[str]) -> int:
        """Distinct count over a tuple of columns (for composite-key detection)."""

    # --- comparison --------------------------------------------------------
    @abstractmethod
    def duplicate_keys(
        self, frame: Any, keys: List[str], sample_cap: int
    ) -> Tuple[int, List[Dict[str, Any]]]:
        """Number of key values that occur more than once, plus sampled rows."""

    @abstractmethod
    def rename_columns(self, frame: Any, mapping: Dict[str, str]) -> Any:
        """Return a frame with columns renamed per mapping (source-side)."""

    @abstractmethod
    def join_and_diff(
        self,
        source: Any,
        target: Any,
        keys: List[str],
        compare_columns: List[str],
        options: Options,
    ) -> DiffResult:
        """Full-outer join on keys and diff the compared columns.

        Implementations honor options.number_tolerance, trim_strings,
        case_insensitive and options.sample_cap.
        """
