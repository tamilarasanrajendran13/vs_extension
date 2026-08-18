import polars as pl
import pytest

from datacompare.config import Options
from datacompare.engines.polars_engine import PolarsEngine


@pytest.fixture
def eng():
    return PolarsEngine()


def _src():
    return pl.DataFrame(
        {
            "id": [1, 2, 3, 4],
            "name": ["a", "b", "c", "d"],
            "val": [10.0, 20.0, None, 40.0],
        }
    )


def _tgt():
    return pl.DataFrame(
        {
            "id": [1, 2, 3, 5],
            "name": ["a", "B", "c", "e"],
            "val": [10.0, 20.0, None, 50.0],
        }
    )


def test_row_count_and_columns(eng):
    assert eng.row_count(_src()) == 4
    assert eng.columns(_src()) == ["id", "name", "val"]


def test_schema_normalized(eng):
    sch = eng.schema(_src())
    assert sch["id"] == "int"
    assert sch["name"] == "str"
    assert sch["val"] == "float"


def test_null_counts(eng):
    nc = eng.null_counts(_src(), ["id", "val"])
    assert nc["id"] == 0
    assert nc["val"] == 1


def test_unique_counts(eng):
    uc = eng.unique_counts(_src(), ["id", "name"])
    assert uc["id"] == 4
    assert uc["name"] == 4


def test_composite_unique_count(eng):
    df = pl.DataFrame({"a": [1, 1, 2], "b": ["x", "y", "x"]})
    assert eng.composite_unique_count(df, ["a", "b"]) == 3
    assert eng.composite_unique_count(df, ["a"]) == 2


def test_duplicate_keys(eng):
    df = pl.DataFrame({"id": [1, 1, 2, 3, 3]})
    count, samples = eng.duplicate_keys(df, ["id"], 10)
    assert count == 2
    assert len(samples) == 2


def test_join_and_diff_basic(eng):
    opts = Options()
    diff = eng.join_and_diff(_src(), _tgt(), ["id"], ["name", "val"], opts)
    # ids 1,2,3 in both; 4 only in source; 5 only in target.
    assert diff.missing_count == 1  # id 4
    assert diff.extra_count == 1  # id 5
    # id 2 name differs (b vs B, case-sensitive default).
    assert diff.mismatched_rows == 1
    assert diff.matched_rows == 2
    assert diff.total_cell_mismatches == 1
    assert diff.mismatches[0]["column"] == "name"


def test_case_insensitive_option(eng):
    opts = Options(case_insensitive=True)
    diff = eng.join_and_diff(_src(), _tgt(), ["id"], ["name", "val"], opts)
    # With case-insensitive, b == B, so no mismatch among the shared ids.
    assert diff.mismatched_rows == 0
    assert diff.matched_rows == 3


def test_number_tolerance(eng):
    s = pl.DataFrame({"id": [1, 2], "v": [10.00, 20.00]})
    t = pl.DataFrame({"id": [1, 2], "v": [10.02, 20.50]})
    # tolerance 0.05: row 1 within, row 2 outside.
    diff = eng.join_and_diff(s, t, ["id"], ["v"], Options(number_tolerance=0.05))
    assert diff.mismatched_rows == 1
    assert diff.mismatches[0]["key"] == {"id": "2"}


def test_null_equality(eng):
    s = pl.DataFrame({"id": [1], "v": [None]})
    t = pl.DataFrame({"id": [1], "v": [None]})
    diff = eng.join_and_diff(s, t, ["id"], ["v"], Options())
    assert diff.mismatched_rows == 0
    assert diff.matched_rows == 1


def test_one_sided_null_is_mismatch(eng):
    s = pl.DataFrame({"id": [1], "v": ["x"]})
    t = pl.DataFrame({"id": [1], "v": [None]})
    diff = eng.join_and_diff(s, t, ["id"], ["v"], Options())
    assert diff.mismatched_rows == 1
