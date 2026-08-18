"""Optional, toggleable data-quality checks run alongside the row comparison."""

from .schema_check import run_schema_check
from .null_check import run_null_check
from .duplicate_check import run_duplicate_check

__all__ = ["run_schema_check", "run_null_check", "run_duplicate_check"]
