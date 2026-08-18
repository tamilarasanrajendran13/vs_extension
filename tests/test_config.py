import pytest

from datacompare.config import parse_test_case, ConfigError, Options


def _base():
    return {
        "name": "t",
        "engine": "polars",
        "format": "csv",
        "source": {"path": "a.csv"},
        "target": {"path": "b.csv"},
    }


def test_minimal_valid():
    tc = parse_test_case(_base(), base_dir="/data")
    assert tc.name == "t"
    assert tc.engine == "polars"
    assert tc.source.path == "/data/a.csv"
    assert isinstance(tc.options, Options)
    assert tc.options.schema_check is True


def test_options_override():
    raw = _base()
    raw["options"] = {"schema_check": False, "number_tolerance": 0.5, "sample_cap": 10}
    tc = parse_test_case(raw)
    assert tc.options.schema_check is False
    assert tc.options.number_tolerance == 0.5
    assert tc.options.sample_cap == 10


def test_missing_required_field():
    raw = _base()
    del raw["name"]
    with pytest.raises(ConfigError):
        parse_test_case(raw)


def test_bad_engine():
    raw = _base()
    raw["engine"] = "duckdb"
    with pytest.raises(ConfigError):
        parse_test_case(raw)


def test_bad_format():
    raw = _base()
    raw["format"] = "parquet"
    with pytest.raises(ConfigError):
        parse_test_case(raw)


def test_negative_tolerance_rejected():
    raw = _base()
    raw["options"] = {"number_tolerance": -1}
    with pytest.raises(ConfigError):
        parse_test_case(raw)


def test_xml_requires_record_path():
    raw = _base()
    raw["format"] = "xml"
    with pytest.raises(ConfigError):
        parse_test_case(raw)


def test_xml_top_level_record_path_propagates():
    raw = _base()
    raw["format"] = "xml"
    raw["record_path"] = "./rec"
    tc = parse_test_case(raw)
    assert tc.source.record_path == "./rec"
    assert tc.target.record_path == "./rec"


def test_key_columns_must_be_list_of_strings():
    raw = _base()
    raw["key_columns"] = "id"
    with pytest.raises(ConfigError):
        parse_test_case(raw)
