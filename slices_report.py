#!/usr/bin/env python3
"""
slices_report - the lead/worker view of the dashboard.

When a ticket runs through a lead (developer or QA), the lead records the slice
breakdown in its gate details: the unit_tests gate (actor lead-developer) carries
the dev slices and each worker's outcome and coaching rounds; the qa_e2e gate
(actor lead-qa) carries the shards. This reads those from ledger.db and renders a
per-ticket view - which worker/shard took what, how many coaching rounds it
needed, and whether it passed - so you can watch the parallel runs.

Standalone today; the same data drops into bundle.html as a tab once the
dashboard's build step is on hand.

  python scripts/slices_report.py --db ledger.db --out slices.html

Self-test:  python scripts/slices_report.py --self-test
"""

from __future__ import annotations

import argparse
import html
import json
import sqlite3
import sys
from pathlib import Path

LEAD_ACTORS = ("lead-developer", "lead-qa")


def load_lead_gates(db_path):
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM gates WHERE actor IN (?, ?)", LEAD_ACTORS)]
    except Exception:
        rows = []
    finally:
        con.close()
    return rows


def _details(row):
    try:
        return json.loads(row.get("details_json") or "{}")
    except Exception:
        return {}


def summarize(rows):
    """Group lead gates by ticket into {ticket: {dev:..., qa:...}}."""
    tickets = {}
    for r in rows:
        tid = r.get("ticket_id") or "?"
        actor = r.get("actor")
        d = _details(r)
        t = tickets.setdefault(tid, {})
        if actor == "lead-developer":
            t["dev"] = {"outcome": r.get("outcome"), "slices": d.get("slices"),
                        "passed": d.get("passed"), "failed": d.get("failed"),
                        "workers": d.get("workers") or []}
        elif actor == "lead-qa":
            t["qa"] = {"outcome": r.get("outcome"), "shards": d.get("shards"),
                       "passed": d.get("passed"), "failed": d.get("failed"),
                       "shards_out": d.get("shard_outcomes") or []}
    return tickets


_CSS = """
:root{
  --ink:#1b1a17; --rule:#d9d4c6; --panel-2:#f7f4ec; --paper:#fdfcf7;
  --muted:#6f6a5c; --carmine:#8c2f28; --ultra:#1f5c5a; --amber:#9a6a12;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  --serif:"Iowan Old Style",Georgia,"Times New Roman",serif;
}
*{box-sizing:border-box}
#page-slices{color:var(--ink);background:var(--paper);font-family:var(--serif);
  line-height:1.5;padding:32px;max-width:1000px;margin:0 auto}
#page-slices h1{font-size:26px;margin:0 0 2px;letter-spacing:-.01em}
#page-slices .sub{color:var(--muted);margin:0 0 22px;font-size:14px}
#page-slices .ticket{border-top:1px solid var(--ink);padding:16px 0 4px}
#page-slices .ticket h2{font-family:var(--mono);font-size:16px;margin:0 0 10px}
#page-slices .lane{margin:0 0 12px}
#page-slices .lane h3{font-family:var(--mono);font-size:12px;text-transform:uppercase;
  letter-spacing:.08em;color:var(--muted);margin:0 0 6px}
#page-slices .agg{font-family:var(--mono);font-size:11px;padding:1px 7px;border:1px solid var(--rule);
  border-radius:2px;margin-left:8px;text-transform:uppercase;letter-spacing:.04em}
#page-slices .pass{color:var(--ultra);border-color:var(--ultra)}
#page-slices .fail{color:var(--carmine);border-color:var(--carmine)}
#page-slices .unknown{color:var(--amber);border-color:var(--amber)}
#page-slices .single{color:var(--muted)}
#page-slices .cards{display:flex;flex-wrap:wrap;gap:8px}
#page-slices .card{border:1px solid var(--rule);border-left-width:3px;padding:8px 12px;
  min-width:150px;background:var(--panel-2)}
#page-slices .card.pass{border-left-color:var(--ultra)}
#page-slices .card.fail{border-left-color:var(--carmine)}
#page-slices .card.unknown{border-left-color:var(--amber)}
#page-slices .card .id{font-family:var(--mono);font-weight:600;font-size:13px}
#page-slices .card .meta{font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:3px}
#page-slices .coached{color:var(--amber)}
#page-slices .empty{color:var(--muted);font-style:italic;padding:26px 0}
"""


def _cls(outcome):
    return outcome if outcome in ("pass", "fail", "unknown") else "single"


def _worker_cards(items, id_key, extra_pass=None):
    out = ["<div class='cards'>"]
    for it in items:
        o = it.get("outcome")
        rounds = it.get("rounds")
        coached = ("<span class='coached'>coached x{}</span>".format(rounds - 1)
                   if isinstance(rounds, int) and rounds > 1 else
                   "{} round".format(rounds) if rounds else "")
        out.append("<div class='card {}'><div class='id'>{}</div>"
                   "<div class='meta'>{} {}</div></div>".format(
                       _cls(o), html.escape(str(it.get(id_key, "?"))),
                       html.escape(str(o)), coached))
    out.append("</div>")
    return "".join(out)


def render_html(tickets, standalone=True):
    parts = []
    if standalone:
        parts.append("<!doctype html><meta charset='utf-8'><title>Lead runs - Docket</title>")
    parts.append("<style>{}</style>".format(_CSS))
    parts.append("<section class='page' id='page-slices'>")
    parts.append("<h1>Lead runs</h1>")
    parts.append("<p class='sub'>Parallel slices and shards per ticket - which worker "
                 "took what, coaching rounds, and outcomes.</p>")

    if not tickets:
        parts.append("<p class='empty'>No lead runs yet. A ticket routes through a "
                     "lead when parallel_dev / parallel_qa is on and the work splits "
                     "into more than one slice.</p>")
    for tid in sorted(tickets):
        t = tickets[tid]
        parts.append("<div class='ticket'><h2>{}</h2>".format(html.escape(tid)))
        dev = t.get("dev")
        if dev:
            parts.append("<div class='lane'><h3>developer slices"
                         "<span class='agg {0}'>{1}</span></h3>".format(
                             _cls(dev["outcome"]), html.escape(str(dev["outcome"]))))
            parts.append(_worker_cards(dev["workers"], "worker"))
            parts.append("</div>")
        qa = t.get("qa")
        if qa:
            parts.append("<div class='lane'><h3>qa shards"
                         "<span class='agg {0}'>{1}</span></h3>".format(
                             _cls(qa["outcome"]), html.escape(str(qa["outcome"]))))
            parts.append(_worker_cards(qa["shards_out"], "shard"))
            parts.append("</div>")
        parts.append("</div>")

    parts.append("</section>")
    return "\n".join(parts)


def generate(db_path, out_path, standalone=True):
    tickets = summarize(load_lead_gates(db_path))
    Path(out_path).write_text(render_html(tickets, standalone=standalone), encoding="utf-8")
    return {"tickets": len(tickets)}


# ==================================================================== self-test

def _self_test():
    import tempfile

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "ledger.db"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE gates (ticket_id TEXT, gate_name TEXT, outcome TEXT, "
                    "actor TEXT, details_json TEXT)")
        con.execute(
            "INSERT INTO gates VALUES (?,?,?,?,?)",
            ("OT-1", "unit_tests", "pass", "lead-developer", json.dumps(
                {"slices": 2, "passed": 2, "failed": 0,
                 "workers": [{"worker": "w0", "outcome": "pass", "rounds": 1},
                             {"worker": "w1", "outcome": "pass", "rounds": 2}]})))
        con.execute(
            "INSERT INTO gates VALUES (?,?,?,?,?)",
            ("OT-1", "qa_e2e", "fail", "lead-qa", json.dumps(
                {"shards": 2, "passed": 1, "failed": 1,
                 "shard_outcomes": [{"shard": "s0", "outcome": "pass", "rounds": 1},
                                    {"shard": "s1", "outcome": "fail", "rounds": 3}]})))
        # a non-lead gate must be ignored
        con.execute("INSERT INTO gates VALUES (?,?,?,?,?)",
                    ("OT-1", "blind_review", "pass", "reviewer", "{}"))
        con.commit(); con.close()

        rows = load_lead_gates(db)
        ok("reads only lead gates", len(rows) == 2)

        tickets = summarize(rows)
        ok("groups by ticket", list(tickets) == ["OT-1"])
        ok("dev lane has two workers", len(tickets["OT-1"]["dev"]["workers"]) == 2)
        ok("qa lane recorded a failing shard", tickets["OT-1"]["qa"]["outcome"] == "fail")

        out = Path(td) / "slices.html"
        generate(db, out)
        h = out.read_text()
        ok("html renders the ticket", "OT-1" in h and "Lead runs" in h)
        ok("worker ids shown", "w0" in h and "w1" in h)
        ok("coaching surfaced (w1 took 2 rounds)", "coached x1" in h)
        ok("failing shard flagged", "s1" in h and "fail" in h)
        ok("non-lead gate excluded", "blind_review" not in h)

        # empty db renders gracefully
        empty = Path(td) / "empty.db"
        c = sqlite3.connect(str(empty))
        c.execute("CREATE TABLE gates (ticket_id TEXT, actor TEXT, details_json TEXT, outcome TEXT)")
        c.commit(); c.close()
        generate(empty, Path(td) / "empty.html")
        ok("empty -> graceful empty page",
           "No lead runs yet" in (Path(td) / "empty.html").read_text())

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket lead/worker slice dashboard")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--db", default="ledger.db")
    ap.add_argument("--out", default="slices.html")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    totals = generate(args.db, args.out)
    print("wrote {} - {} ticket(s) with lead runs".format(args.out, totals["tickets"]))


if __name__ == "__main__":
    main()
