"""Spark engine tests. Skipped automatically if a local Spark session cannot
start (e.g. Java not installed), so the suite stays green without Java while
still exercising Spark wherever it is available."""

import pytest


def _spark_engine_or_skip():
    try:
        from datacompare.engines.spark_engine import SparkEngine

        return SparkEngine()
    except Exception as exc:  # RuntimeError when Java/pyspark unavailable
        pytest.skip("Spark unavailable: %s" % exc)


def test_spark_matches_polars_end_to_end(repo_root):
    eng = _spark_engine_or_skip()
    import os
    from datacompare.config import Source
    from datacompare.config import Options

    src = eng.read_csv(
        Source(path=os.path.join(repo_root, "sample_data", "customers_source.csv"))
    )
    tgt = eng.read_csv(
        Source(path=os.path.join(repo_root, "sample_data", "customers_target.csv"))
    )
    assert eng.row_count(src) == 5
    diff = eng.join_and_diff(src, tgt, ["customer_id"], ["name", "email", "balance", "country"], Options())
    assert diff.matched_rows == 3
    assert diff.mismatched_rows == 1
    assert diff.missing_count == 1
    assert diff.extra_count == 1


def test_spark_full_run(repo_root):
    _spark_engine_or_skip()
    import os
    from datacompare.config import load_test_case
    from datacompare.compare import run_comparison

    tc = load_test_case(os.path.join(repo_root, "testcases", "customers_spark.yaml"))
    r = run_comparison(tc)
    assert r.engine == "spark"
    assert r.summary.matched_rows == 3
    assert r.summary.mismatched_rows == 1
    assert r.passed is False
