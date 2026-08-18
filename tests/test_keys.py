import polars as pl
import pytest

from datacompare.engines.polars_engine import PolarsEngine
from datacompare.keys import detect_keys, _name_bonus


@pytest.fixture
def eng():
    return PolarsEngine()


def test_single_unique_id_preferred(eng):
    s = pl.DataFrame({"customer_id": [1, 2, 3], "name": ["a", "b", "c"]})
    t = pl.DataFrame({"customer_id": [1, 2, 3], "name": ["a", "b", "c"]})
    keys, warns = detect_keys(eng, s, t)
    assert keys == ["customer_id"]
    assert warns == []


def test_prefers_id_name_over_other_unique(eng):
    # Both columns unique; id-like name should win.
    s = pl.DataFrame({"name": ["a", "b", "c"], "user_id": [10, 20, 30]})
    t = pl.DataFrame({"name": ["a", "b", "c"], "user_id": [10, 20, 30]})
    keys, _ = detect_keys(eng, s, t)
    assert keys == ["user_id"]


def test_composite_key_detected(eng):
    # Neither column is unique alone; only the pair uniquely identifies a row.
    s = pl.DataFrame({"region": ["N", "N", "S"], "code": [1, 2, 1]})
    t = pl.DataFrame({"region": ["N", "N", "S"], "code": [1, 2, 1]})
    keys, warns = detect_keys(eng, s, t)
    assert set(keys) == {"region", "code"}
    assert warns == []


def test_fallback_all_columns_when_no_unique(eng):
    s = pl.DataFrame({"a": [1, 1], "b": ["x", "x"]})
    t = pl.DataFrame({"a": [1, 1], "b": ["x", "x"]})
    keys, warns = detect_keys(eng, s, t)
    assert set(keys) == {"a", "b"}
    assert warns and "no unique key" in warns[0]


def test_no_shared_columns_raises(eng):
    s = pl.DataFrame({"a": [1]})
    t = pl.DataFrame({"b": [1]})
    with pytest.raises(ValueError):
        detect_keys(eng, s, t)


def test_name_bonus_ranking():
    assert _name_bonus("id") > _name_bonus("user_id")
    assert _name_bonus("user_id") > _name_bonus("customer")
    assert _name_bonus("random") == 0
