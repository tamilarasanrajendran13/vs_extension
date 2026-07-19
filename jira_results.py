#!/usr/bin/env python3
"""
jira_results - post the unit-test results back to the ticket, on completion.

The developer already BUILT the comment (evidence/jira-comment.txt) during the
run. This is only the SEND, and it is deliberately conservative:

  - OFF by default. Writing to a real ticket other people read is opt-in:
    cfg['jira']['post_results'] must be true.
  - Only when the implementation completed (impl gate passed). A "here are your
    results" comment on a run that never finished is worse than silence.
  - Only when we actually have a Jira client - i.e. the ticket was fetched from
    Jira (--fetch). A --ticket-text run has nowhere to post.
  - Never fatal. Jira being slow or down must not sink a run whose work is done
    and already in the ledger.

Reuses the same client the spec agent posts questions through: it is stashed on
the ticket as ticket['_client'] by fetch_ticket.

Self-test (no network):  python scripts/jira_results.py --self-test
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def results_comment(run_id, ticket_id, result, workbench, release):
    """The comment body: the developer's built summary if present, plus a
    one-line pipeline gate status and a run marker so a re-run can find it.
    """
    parts = []
    built = (Path(workbench) / "development" / (release or "unreleased") / ticket_id
             / "evidence" / "jira-comment.txt")
    if built.exists():
        text = built.read_text(encoding="utf-8").strip()
        if text:
            parts.append(text)
    if not parts:
        impl = result.get("impl") or {}
        u = impl.get("unit") or {}
        parts.append("Docket run {} - unit tests: {} passed, {} failed of {}".format(
            run_id, u.get("passed", 0), u.get("failed", 0), u.get("total", 0)))

    gates = []
    for key in ("review", "security", "qa", "mutation"):
        v = result.get(key)
        if v and v.get("outcome"):
            gates.append("{}={}".format(key, v["outcome"]))
    if gates:
        parts.append("Pipeline gates: " + ", ".join(gates))

    parts.append("docket:result:{}".format(run_id))
    return "\n\n".join(parts)


def post_results(cfg, ticket, run_id, ticket_id, result, workbench, release, say):
    """Send the results comment, if configured and the run completed. Returns
    True only when a comment was actually posted.
    """
    if not (cfg.get("jira") or {}).get("post_results", False):
        return False  # opt-in only
    impl = (result or {}).get("impl") or {}
    if impl.get("outcome") != "pass":
        return False  # only on implementation completion
    client = (ticket or {}).get("_client")
    if not client:
        return False  # nothing to post to (not a --fetch run)

    body = results_comment(run_id, ticket_id, result, workbench, release)
    try:
        if client.add_comment(ticket_id, body):
            say("  posted unit-test results to {} as a comment.".format(ticket_id))
            return True
        say("  could not post results to {} (permission?). They are in the ledger.".format(ticket_id))
    except Exception as e:
        say("  could not post results to {}: {}".format(ticket_id, e))
    return False


# ==================================================================== self-test

class _FakeClient:
    def __init__(self, ok=True, raises=False):
        self.ok = ok
        self.raises = raises
        self.posted = []

    def add_comment(self, key, body):
        if self.raises:
            raise RuntimeError("jira down")
        self.posted.append((key, body))
        return self.ok


def _self_test():
    import tempfile

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    passing = {"impl": {"outcome": "pass", "unit": {"passed": 12, "failed": 0, "total": 12}},
               "review": {"outcome": "pass"}, "security": {"outcome": "pass"},
               "qa": {"outcome": "pass"}, "mutation": {"outcome": "pass"}}

    with tempfile.TemporaryDirectory() as td:
        wb = Path(td)

        # OFF by default: nothing posts even on a clean run
        c = _FakeClient()
        posted = post_results({}, {"_client": c}, "R1", "OT-1", passing, str(wb), None, lambda *_: None)
        ok("off by default -> no post", posted is False and not c.posted)

        # ON + implementation complete -> posts
        c = _FakeClient()
        posted = post_results({"jira": {"post_results": True}}, {"_client": c},
                              "R1", "OT-1", passing, str(wb), None, lambda *_: None)
        ok("on + impl passed -> posts", posted is True and len(c.posted) == 1)
        body = c.posted[0][1]
        ok("comment carries the run marker", "docket:result:R1" in body)
        ok("comment carries a gate summary", "qa=pass" in body and "mutation=pass" in body)
        ok("comment carries the unit numbers", "12 passed" in body)

        # ON but implementation did not complete -> no post
        c = _FakeClient()
        failed = dict(passing, impl={"outcome": "fail"})
        posted = post_results({"jira": {"post_results": True}}, {"_client": c},
                              "R2", "OT-2", failed, str(wb), None, lambda *_: None)
        ok("impl not passed -> no post", posted is False and not c.posted)

        # ON but no client (a --ticket-text run) -> no post, no crash
        posted = post_results({"jira": {"post_results": True}}, None, "R3", "OT-3",
                              passing, str(wb), None, lambda *_: None)
        ok("no jira client -> no post", posted is False)

        # prefers the developer's built comment when present
        dev = wb / "development" / "R2025.10" / "OT-9" / "evidence"
        dev.mkdir(parents=True)
        (dev / "jira-comment.txt").write_text("Custom developer summary line", encoding="utf-8")
        c = _FakeClient()
        post_results({"jira": {"post_results": True}}, {"_client": c}, "R9", "OT-9",
                     passing, str(wb), "R2025.10", lambda *_: None)
        ok("uses the developer's built comment", "Custom developer summary line" in c.posted[0][1])

        # never fatal: a client that raises does not propagate
        c = _FakeClient(raises=True)
        posted = post_results({"jira": {"post_results": True}}, {"_client": c},
                              "R4", "OT-4", passing, str(wb), None, lambda *_: None)
        ok("jira down -> returns False, does not raise", posted is False)

    passed = sum(1 for _, cnd in checks if cnd)
    for name, cnd in checks:
        print("  [{}] {}".format("ok " if cnd else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket Jira results post-back")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
