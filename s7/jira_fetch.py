#!/usr/bin/env python3
"""
Docket - fetch a ticket.

    python scripts/jira_fetch.py PROJ-110
    python scripts/jira_fetch.py PROJ-110 --json

The acceptance-criteria search is a four-tier hunt, because Jira does not return
field NAMES in the issue payload - only opaque customfield_XXXXX keys. So there
is no single field ID to configure. We look, in priority order, and we report HOW
we found it.

    tier 0  explicitly configured field IDs   (JIRA_AC_FIELD_IDS)
    tier 1  scan customfield_* keys for an AC-ish name
    tier 2  same, against renderedFields
    tier 3  an "Acceptance Criteria" heading inside the description
    ---     not_found

`ac_source` is not diagnostics. It is a GATE INPUT.

If ac_source == "not_found", the ticket has no acceptance criteria anywhere in
Jira. That is a comprehension failure we can prove without asking a model - zero
tokens, zero latency, no chance of an LLM inventing criteria to be helpful. The
cheapest gate in the pipeline is the one that never calls anything.

Same for labels: no docket-ready label means we stop before the first token.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jira_client import JiraClient, JiraError, from_env  # noqa: E402

# Normalised custom-field names that mean "acceptance criteria".
AC_FIELD_NAMES = {"acceptancecriteria", "acceptcriteria", "ac", "criteria"}

# A heading that ends the AC block in a description.
_HEADING = re.compile(r"^\s*(h[1-6]\.|#{1,6}\s|\*{1,2}[A-Z][^*]{2,30}\*{1,2}\s*$)", re.I)


def safe_str(value: Any) -> str:
    """Jira returns display-name dicts for people, priorities, statuses."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for k in ("displayName", "name", "value", "content"):
            if value.get(k):
                return str(value[k])
        return json.dumps(value)[:200]
    if isinstance(value, list):
        return ", ".join(safe_str(v) for v in value)
    return str(value)


def parse_ac_field_ids(raw: str | None) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def find_acceptance_criteria(fields: dict, rendered: dict,
                             ac_field_ids: list[str]) -> tuple[str, str]:
    """
    Returns (text, source). source is one of:
        configured_field:<id> | configured_rendered:<id> | custom_field:<id>
        | rendered_field:<id> | description_section | not_found
    """
    # tier 0 - someone told us exactly where it is. Trust them.
    for key in ac_field_ids:
        if fields.get(key) is not None:
            return safe_str(fields[key]), f"configured_field:{key}"
        if rendered.get(key) is not None:
            return safe_str(rendered[key]), f"configured_rendered:{key}"

    # tier 1/2 - scan custom fields. Jira hides names, so match the key itself.
    for source, blob in (("custom_field", fields), ("rendered_field", rendered)):
        for key, value in (blob or {}).items():
            if not key.startswith("customfield_"):
                continue
            if value is None:
                continue
            normalised = key.replace("customfield_", "").replace("_", "").lower()
            if normalised in AC_FIELD_NAMES:
                text = safe_str(value).strip()
                if text:
                    return text, f"{source}:{key}"

    # tier 3 - a heading in the description.
    description = safe_str(fields.get("description")) or safe_str(rendered.get("description"))
    if description:
        captured: list[str] = []
        capturing = False
        for line in description.splitlines():
            low = line.strip().lower()
            if not capturing:
                if "acceptance criteria" in low:
                    capturing = True
                continue
            if line.strip() and _HEADING.match(line):
                break
            captured.append(line)
        text = "\n".join(captured).strip()
        if text:
            return text, "description_section"

    return "", "not_found"


def fetch(issue_key: str, client: JiraClient | None = None,
          ac_field_ids: list[str] | None = None,
          description_limit: int = 8000) -> dict:
    """Ticket key -> everything Docket needs. Never raises for a missing AC."""
    client = client or from_env()
    ac_field_ids = ac_field_ids or parse_ac_field_ids(os.environ.get("JIRA_AC_FIELD_IDS"))

    issue = client.get_issue(issue_key, expand_rendered=True)
    fields = issue.get("fields") or {}
    rendered = issue.get("renderedFields") or {}

    ac_text, ac_source = find_acceptance_criteria(fields, rendered, ac_field_ids)

    # fixVersions is where a release lives when Jira knows it. Often it doesn't -
    # which is why release stays overridable rather than assumed.
    fix_versions = [safe_str(v) for v in (fields.get("fixVersions") or [])]

    return {
        "issue": issue_key,
        "summary": safe_str(fields.get("summary")),
        "description": (safe_str(fields.get("description"))
                        or safe_str(rendered.get("description")))[:description_limit],
        "labels": fields.get("labels") or [],
        "priority": safe_str(fields.get("priority")),
        "assignee": safe_str(fields.get("assignee")),
        "reporter": safe_str(fields.get("reporter")),
        "status_name": safe_str(fields.get("status")),
        "issue_type": safe_str(fields.get("issuetype")),
        "acceptance_criteria": ac_text,
        "acceptance_criteria_source": ac_source,
        "fix_versions": fix_versions,
        "release": fix_versions[0] if fix_versions else None,
        # Keys only, never values. A custom field can hold anything, including
        # things that should not travel into a prompt or a ledger.
        "custom_field_keys": [k for k in fields if k.startswith("customfield_")
                              and fields[k] is not None],
    }


def preflight(ticket: dict, trigger_label: str | None = None) -> list[dict]:
    """
    Deterministic gates. No model, no tokens, no latency.

    Each returns pass | fail | skip with a reason a human can act on. Run these
    BEFORE the spec agent - there is no point paying for a model to discover that
    a ticket has no acceptance criteria when Jira already told us.
    """
    out = []

    if trigger_label:
        has = trigger_label in (ticket.get("labels") or [])
        out.append({
            "check": "trigger_label",
            "result": "pass" if has else "fail",
            "detail": f"label '{trigger_label}' "
                      + ("present" if has else
                         f"absent (labels: {', '.join(ticket['labels']) or 'none'})"),
            "question": None if has else
                        f"Add the '{trigger_label}' label to {ticket['issue']} when it's ready.",
        })

    ac_ok = ticket.get("acceptance_criteria_source") != "not_found"
    out.append({
        "check": "acceptance_criteria_present",
        "result": "pass" if ac_ok else "fail",
        "detail": f"found via {ticket['acceptance_criteria_source']}" if ac_ok
                  else "no acceptance criteria in any custom field, rendered field, "
                       "or description section",
        "question": None if ac_ok else
                    f"{ticket['issue']} has no acceptance criteria. Add them to the AC "
                    f"field, or an 'Acceptance Criteria' section in the description.",
    })

    has_desc = len((ticket.get("description") or "").strip()) >= 40
    out.append({
        "check": "description_substantive",
        "result": "pass" if has_desc else "fail",
        "detail": f"{len(ticket.get('description') or '')} chars",
        "question": None if has_desc else
                    f"{ticket['issue']} has little or no description. What should be built, "
                    f"and why?",
    })
    return out


def to_ticket_text(ticket: dict) -> str:
    """What the spec agent reads. AC first - it's the thing that matters most."""
    ac = ticket.get("acceptance_criteria") or "(none found in Jira)"
    return "\n".join([
        f"Issue: {ticket['issue']}",
        f"Type: {ticket.get('issue_type')}   Priority: {ticket.get('priority')}",
        f"Summary: {ticket.get('summary')}",
        f"Labels: {', '.join(ticket.get('labels') or []) or 'none'}",
        "",
        f"=== Acceptance Criteria (source: {ticket.get('acceptance_criteria_source')}) ===",
        ac,
        "",
        "=== Description ===",
        ticket.get("description") or "(empty)",
    ])


# ---------------------------------------------------------------- self-test

class _FakeClient:
    def __init__(self, issue):
        self._issue = issue

    def get_issue(self, key, expand_rendered=True):
        return self._issue


def _self_test() -> int:
    ok = []

    # tier 3: AC as a description heading, terminated by the next heading
    t = fetch("P-1", _FakeClient({"fields": {
        "summary": "Retry billing timeouts",
        "description": "Some intro.\n\nh2. Acceptance Criteria\n"
                       "* max 3 attempts\n* exponential backoff\n\n"
                       "h2. Notes\nignore me",
        "labels": ["docket-ready"],
        "status": {"name": "Open"}, "priority": {"name": "High"},
        "reporter": {"displayName": "Jane PO"},
    }, "renderedFields": {}}))
    ok.append(("tier 3: AC from description heading", t["acceptance_criteria_source"] == "description_section"))
    ok.append(("tier 3: stops at next heading", "ignore me" not in t["acceptance_criteria"]))
    ok.append(("tier 3: captured both criteria", "exponential" in t["acceptance_criteria"]))

    # tier 0: explicitly configured field wins over everything
    t = fetch("P-2", _FakeClient({"fields": {
        "customfield_10300": "AC from the real field",
        "description": "h2. Acceptance Criteria\nAC from description",
        "labels": [],
    }, "renderedFields": {}}), ac_field_ids=["customfield_10300"])
    ok.append(("tier 0: configured field wins", t["acceptance_criteria"] == "AC from the real field"))
    ok.append(("tier 0: source names the field",
               t["acceptance_criteria_source"] == "configured_field:customfield_10300"))

    # not_found -> the free gate
    t = fetch("P-3", _FakeClient({"fields": {
        "summary": "test", "description": "test", "labels": [],
    }, "renderedFields": {}}))
    ok.append(("not_found when absent", t["acceptance_criteria_source"] == "not_found"))
    checks = {c["check"]: c for c in preflight(t, trigger_label="docket-ready")}
    ok.append(("no AC -> gate fails, zero tokens", checks["acceptance_criteria_present"]["result"] == "fail"))
    ok.append(("no label -> gate fails", checks["trigger_label"]["result"] == "fail"))
    ok.append(("thin description -> gate fails", checks["description_substantive"]["result"] == "fail"))
    ok.append(("questions are actionable",
               all(c["question"] for c in checks.values() if c["result"] == "fail")))

    # Jira's display-name dicts
    t = fetch("P-4", _FakeClient({"fields": {
        "summary": "x", "description": "y" * 50, "labels": ["docket-ready"],
        "assignee": {"displayName": "Tamil"}, "priority": {"name": "Low"},
        "status": {"name": "In Progress"},
        "fixVersions": [{"name": "R2025.10"}],
        "customfield_10300": "AC here",
    }, "renderedFields": {}}), ac_field_ids=["customfield_10300"])
    ok.append(("display-name dicts flattened", t["assignee"] == "Tamil" and t["priority"] == "Low"))
    ok.append(("release read from fixVersions", t["release"] == "R2025.10"))
    ok.append(("custom field VALUES never leak", "customfield_10300" in t["custom_field_keys"]
               and "AC here" not in json.dumps(t["custom_field_keys"])))
    checks = {c["check"]: c for c in preflight(t, trigger_label="docket-ready")}
    ok.append(("good ticket -> all deterministic gates pass",
               all(c["result"] == "pass" for c in checks.values())))
    ok.append(("ticket text puts AC first",
               to_ticket_text(t).index("Acceptance Criteria") < to_ticket_text(t).index("Description")))

    w = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name.ljust(w)}")
    failed = [n for n, p in ok if not p]
    print(f"\n  {len(ok) - len(failed)}/{len(ok)} passed" + (f"  FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch a Jira ticket for Docket")
    ap.add_argument("issue", nargs="?", help="issue key, e.g. PROJ-110")
    ap.add_argument("--json", action="store_true", help="raw JSON out")
    ap.add_argument("--ac-field-ids", help="comma-separated customfield IDs to check first")
    ap.add_argument("--no-verify-ssl", action="store_true")
    ap.add_argument("--label", default="docket-ready", help="trigger label to require")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return _self_test()
    if not a.issue:
        ap.error("issue key required")

    try:
        client = from_env(verify_ssl=not a.no_verify_ssl)
        ticket = fetch(a.issue, client, parse_ac_field_ids(a.ac_field_ids))
    except JiraError as e:
        print(json.dumps({"status": "failed", "reason": str(e)}))
        return 2

    if a.json:
        print(json.dumps({"status": "ok", "ticket": ticket,
                          "preflight": preflight(ticket, a.label)}, indent=2))
        return 0

    print(to_ticket_text(ticket))
    print("\n=== deterministic gates (no model, no tokens) ===")
    for c in preflight(ticket, a.label):
        print(f"  [{c['result'].upper()}] {c['check']}: {c['detail']}")
        if c["question"]:
            print(f"         -> {c['question']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
