import json
import os

from datacompare.config import load_test_case
from datacompare.compare import run_comparison
from datacompare.report.html import render_html


def _tc_path(repo_root, name):
    return os.path.join(repo_root, "testcases", name)


def test_csv_end_to_end(repo_root):
    tc = load_test_case(_tc_path(repo_root, "customers_csv.yaml"))
    r = run_comparison(tc)
    s = r.summary
    assert s.source_rows == 5
    assert s.target_rows == 5
    assert s.matched_rows == 3
    assert s.mismatched_rows == 1  # Bob's email changed
    assert s.missing_rows == 1  # Eve (id 5) only in source
    assert s.extra_rows == 1  # Frank (id 6) only in target
    assert s.total_cell_mismatches == 1
    assert r.key_columns == ["customer_id"]
    assert r.auto_detected_keys is False
    assert r.schema_check.passed is True
    assert r.duplicate_check.passed is True
    assert r.passed is False  # differences exist -> strict fail
    # The one mismatch is the email cell.
    assert r.mismatches[0].column == "email"


def test_csv_autokey(repo_root):
    tc = load_test_case(_tc_path(repo_root, "customers_csv_autokey.yaml"))
    r = run_comparison(tc)
    assert r.auto_detected_keys is True
    assert r.key_columns == ["customer_id"]
    assert r.summary.matched_rows == 3


def test_xml_end_to_end(repo_root):
    tc = load_test_case(_tc_path(repo_root, "orders_xml.yaml"))
    r = run_comparison(tc)
    s = r.summary
    assert s.source_rows == 4
    assert s.target_rows == 4
    assert s.matched_rows == 2  # 1001, 1003
    assert s.mismatched_rows == 1  # 1002 status changed
    assert s.missing_rows == 1  # 1004 only in source
    assert s.extra_rows == 1  # 1005 only in target
    assert r.mismatches[0].column == "status"
    assert r.passed is False


def test_html_render_smoke(repo_root):
    tc = load_test_case(_tc_path(repo_root, "customers_csv.yaml"))
    r = run_comparison(tc)
    html = render_html(r)
    assert "<html" in html
    assert "FAIL" in html
    assert "customers_csv_compare" in html
    # Report must be pure ASCII (Windows-paste safety).
    assert all(ord(c) < 128 for c in html)


def test_json_serializable(repo_root):
    tc = load_test_case(_tc_path(repo_root, "customers_csv.yaml"))
    r = run_comparison(tc)
    blob = json.dumps(r.to_dict(), default=str)
    back = json.loads(blob)
    assert back["summary"]["matched_rows"] == 3
    assert back["passed"] is False
