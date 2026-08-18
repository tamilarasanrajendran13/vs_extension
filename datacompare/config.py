"""Parse and validate a YAML test case into typed dataclasses.

A test case is the only thing a user writes by hand. Validation is strict and
fails fast with clear messages: a bad test case is a harness error (exit 2),
never a silent pass.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

VALID_ENGINES = ("polars", "spark")
VALID_FORMATS = ("csv", "xml")


class ConfigError(ValueError):
    """Raised when a test case is malformed or references missing files."""


@dataclass
class Options:
    schema_check: bool = True
    null_check: bool = True
    duplicate_check: bool = True
    ignore_columns: List[str] = field(default_factory=list)
    column_mapping: Dict[str, str] = field(default_factory=dict)
    number_tolerance: float = 0.0
    trim_strings: bool = True
    case_insensitive: bool = False
    sample_cap: int = 500


@dataclass
class Source:
    path: str
    # CSV reader options
    delimiter: str = ","
    has_header: bool = True
    encoding: str = "utf-8"
    # XML reader option: XPath-ish path to the repeating record element.
    record_path: Optional[str] = None


@dataclass
class TestCase:
    name: str
    engine: str
    data_format: str
    source: Source
    target: Source
    key_columns: List[str] = field(default_factory=list)
    options: Options = field(default_factory=Options)
    # Directory the YAML lives in, so relative data paths resolve predictably.
    base_dir: str = "."


def _require(mapping: Dict[str, Any], key: str, ctx: str) -> Any:
    if key not in mapping:
        raise ConfigError("missing required field '%s' in %s" % (key, ctx))
    return mapping[key]


def _build_source(raw: Any, ctx: str, base_dir: str) -> Source:
    if not isinstance(raw, dict):
        raise ConfigError("'%s' must be a mapping" % ctx)
    path = _require(raw, "path", ctx)
    if not isinstance(path, str) or not path:
        raise ConfigError("'%s.path' must be a non-empty string" % ctx)
    resolved = path if os.path.isabs(path) else os.path.join(base_dir, path)
    return Source(
        path=resolved,
        delimiter=str(raw.get("delimiter", ",")),
        has_header=bool(raw.get("has_header", True)),
        encoding=str(raw.get("encoding", "utf-8")),
        record_path=raw.get("record_path"),
    )


def _build_options(raw: Any) -> Options:
    if raw is None:
        return Options()
    if not isinstance(raw, dict):
        raise ConfigError("'options' must be a mapping")
    opts = Options(
        schema_check=bool(raw.get("schema_check", True)),
        null_check=bool(raw.get("null_check", True)),
        duplicate_check=bool(raw.get("duplicate_check", True)),
        ignore_columns=list(raw.get("ignore_columns", []) or []),
        column_mapping=dict(raw.get("column_mapping", {}) or {}),
        number_tolerance=float(raw.get("number_tolerance", 0.0)),
        trim_strings=bool(raw.get("trim_strings", True)),
        case_insensitive=bool(raw.get("case_insensitive", False)),
        sample_cap=int(raw.get("sample_cap", 500)),
    )
    if opts.number_tolerance < 0:
        raise ConfigError("'options.number_tolerance' must be >= 0")
    if opts.sample_cap < 1:
        raise ConfigError("'options.sample_cap' must be >= 1")
    return opts


def parse_test_case(raw: Dict[str, Any], base_dir: str = ".") -> TestCase:
    """Turn an already-parsed YAML mapping into a validated TestCase."""
    if not isinstance(raw, dict):
        raise ConfigError("test case must be a YAML mapping at the top level")

    name = _require(raw, "name", "test case")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError("'name' must be a non-empty string")

    engine = str(raw.get("engine", "polars")).lower()
    if engine not in VALID_ENGINES:
        raise ConfigError(
            "'engine' must be one of %s, got '%s'" % (VALID_ENGINES, engine)
        )

    data_format = str(_require(raw, "format", "test case")).lower()
    if data_format not in VALID_FORMATS:
        raise ConfigError(
            "'format' must be one of %s, got '%s'" % (VALID_FORMATS, data_format)
        )

    source = _build_source(_require(raw, "source", "test case"), "source", base_dir)
    target = _build_source(_require(raw, "target", "test case"), "target", base_dir)

    if data_format == "xml":
        # record_path may be set globally at top level as a convenience.
        top_rp = raw.get("record_path")
        if source.record_path is None:
            source.record_path = top_rp
        if target.record_path is None:
            target.record_path = top_rp
        if not source.record_path or not target.record_path:
            raise ConfigError(
                "xml format requires 'record_path' (top-level or per-source)"
            )

    key_columns = raw.get("key_columns", []) or []
    if not isinstance(key_columns, list) or not all(
        isinstance(k, str) for k in key_columns
    ):
        raise ConfigError("'key_columns' must be a list of column-name strings")

    return TestCase(
        name=name,
        engine=engine,
        data_format=data_format,
        source=source,
        target=target,
        key_columns=list(key_columns),
        options=_build_options(raw.get("options")),
        base_dir=base_dir,
    )


def load_test_case(path: str) -> TestCase:
    """Load and validate a test case from a YAML file on disk."""
    if not os.path.isfile(path):
        raise ConfigError("test case file not found: %s" % path)
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    base_dir = os.path.dirname(os.path.abspath(path))
    tc = parse_test_case(raw, base_dir=base_dir)
    # Verify data files exist now, so failures are config errors, not crashes.
    for role, src in (("source", tc.source), ("target", tc.target)):
        if not os.path.isfile(src.path):
            raise ConfigError("%s data file not found: %s" % (role, src.path))
    return tc


def _self_test() -> None:
    raw = {
        "name": "t",
        "engine": "polars",
        "format": "csv",
        "source": {"path": "a.csv"},
        "target": {"path": "b.csv"},
        "key_columns": ["id"],
        "options": {"schema_check": False, "number_tolerance": 0.5},
    }
    tc = parse_test_case(raw, base_dir="/data")
    assert tc.name == "t"
    assert tc.source.path == "/data/a.csv"
    assert tc.options.schema_check is False
    assert tc.options.number_tolerance == 0.5

    # Missing required field.
    try:
        parse_test_case({"engine": "polars", "format": "csv"})
        assert False, "should have raised"
    except ConfigError:
        pass

    # Bad engine.
    try:
        parse_test_case(
            {
                "name": "x",
                "engine": "duckdb",
                "format": "csv",
                "source": {"path": "a"},
                "target": {"path": "b"},
            }
        )
        assert False
    except ConfigError:
        pass

    # XML without record_path.
    try:
        parse_test_case(
            {
                "name": "x",
                "format": "xml",
                "source": {"path": "a"},
                "target": {"path": "b"},
            }
        )
        assert False
    except ConfigError:
        pass

    # XML with top-level record_path propagates.
    tc2 = parse_test_case(
        {
            "name": "x",
            "format": "xml",
            "record_path": "./record",
            "source": {"path": "a"},
            "target": {"path": "b"},
        }
    )
    assert tc2.source.record_path == "./record"
    assert tc2.target.record_path == "./record"
    print("config.py self-test: OK")


if __name__ == "__main__":
    _self_test()
