#!/usr/bin/env python3
"""
Docket - report.

    ledger.db -> payload_builder -> one self-contained .html

No VS Code. No models. No network. No CDN. Pure Python and the stdlib, because
the point of this file is a thing you attach to an email and your VP opens on a
locked-down laptop, possibly on a plane, and it just renders.

Self-contained means self-contained. Everything - CSS, JS, payload - is inlined.
There is exactly one file at the end and it has no idea the internet exists.

Usage
-----
    python report.py --db ledger.db --out report.html
    python report.py --db ledger.db --release R2025.10 --out r10.html
    python report.py --demo --out demo.html      # synthetic ledger, no db needed
    python report.py --self-test                 # no db, no browser, seconds
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import payload_builder  # noqa: E402

BUNDLE = os.path.join(HERE, "dashboard")
REPORT_VERSION = "0.1"


def _read(name: str) -> str:
    with open(os.path.join(BUNDLE, name), encoding="utf-8") as f:
        return f.read()


def _safe_json(payload: dict) -> str:
    """
    JSON that can live inside a <script> tag.

    '</script>' anywhere in a string value - a ticket summary, a Snyk finding,
    a reviewer's note - closes the tag early and the page dies silently. Ticket
    text is arbitrary text from Jira; assume it is hostile.
    """
    text = json.dumps(payload, default=str, separators=(",", ":"))
    return (
        text.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render(payload: dict) -> str:
    html = _read("bundle.html")
    stamp = (
        f"docket report {REPORT_VERSION} · "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    # CSS and JS go in last and are never .format()ed - braces in the source
    # would blow up. Plain replace, in a fixed order.
    for token, value in (
        ("__DOCKET_PAYLOAD__", _safe_json(payload)),
        ("__DOCKET_STAMP__", stamp),
        ("__DOCKET_CSS__", _read("app.css")),
        ("__DOCKET_JS__", _read("app.js")),
    ):
        if token not in html:
            raise RuntimeError(f"bundle.html has lost its {token} placeholder")
        html = html.replace(token, value)
    return html


# Outlook rejects attachments over 20MB and most corporate gateways are stricter.
# A report too big to send is a report that does not exist.
WARN_BYTES = 4_000_000


def build_report(db: str, out: str, release=None, project=None, max_events=200,
                 max_rows=40, exclude=(), hero=payload_builder.DEFAULT_HERO) -> str:
    payload = payload_builder.build(db, release=release, project=project,
                                    event_limit=max_events, max_rows=max_rows,
                                    exclude=exclude, hero=hero)
    html = render(payload)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    size = os.path.getsize(out)
    if size > WARN_BYTES:
        big = sorted(
            ((sum(len(v) for v in t.get("related", {}).values()), t["issue"])
             for t in payload["tickets"]), reverse=True)[:3]
        print(
            f"WARNING: {out} is {size // 1_000_000}MB. That is too big to email, "
            f"which is the only thing this file is for.\n"
            f"  Heaviest runs: {', '.join(i for _, i in big)}\n"
            f"  Try --max-rows 10, --max-events 50, or --exclude <table>.",
            file=sys.stderr)
    return out


# --------------------------------------------------------------------------
# self-test
# --------------------------------------------------------------------------


def _self_test() -> int:
    import tempfile
    from _demo_ledger import write_demo

    passed = failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL  {name}")

    tmp = tempfile.mkdtemp()
    db = write_demo(os.path.join(tmp, "l.db"))
    out = os.path.join(tmp, "r.html")
    build_report(db, out)
    html = open(out, encoding="utf-8").read()

    check("report written", os.path.exists(out))
    check("no placeholders survive", "__DOCKET_" not in html)
    check("css inlined", "--carmine" in html)
    check("js inlined", "DocketDashboard" in html)
    check("payload inlined", "DOCKET_PAYLOAD" in html)

    # the whole promise of this file: it opens with nothing installed
    # The promise is "opens on a plane". Assert on real external references -
    # not on the word 'cdn', which appears in the comments boasting about their
    # absence. A test that greps prose tests prose.
    #
    # XML namespace URIs are the one legitimate http:// in the file. They are
    # identifiers, not addresses - createElementNS needs the SVG namespace and
    # nothing is ever fetched from w3.org. Strip them, then be strict.
    NAMESPACES = ("http://www.w3.org/2000/svg", "http://www.w3.org/1999/xhtml",
                  "http://www.w3.org/1999/xlink")
    net = html
    for ns in NAMESPACES:
        net = net.replace(ns, "")

    check("no external fetch", "fetch(" not in html)
    check("no <link> tags", "<link" not in html.lower())
    check("no src= attributes", " src=" not in html.lower())
    check("no @import", "@import" not in html)
    check("no absolute urls beyond xml namespaces",
          "http://" not in net and "https://" not in net)
    check("single file, no siblings", len(os.listdir(tmp)) == 2)

    # a ticket summary containing </script> must not decapitate the page
    import sqlite3
    con = sqlite3.connect(db)
    con.execute("UPDATE runs SET summary = ? WHERE rowid = 1",
                ("</script><script>alert(1)</script> hostile ticket",))
    con.commit()
    con.close()
    build_report(db, out)
    html2 = open(out, encoding="utf-8").read()
    check("script-tag injection neutralised", "<script>alert(1)" not in html2)
    check("payload survives escaping", "\\u003c/script" in html2)
    check("still exactly two script tags", html2.count("<script>") == 2)

    # the payload the page gets back must be the payload we put in
    start = html2.index("window.DOCKET_PAYLOAD = ") + len("window.DOCKET_PAYLOAD = ")
    end = html2.index(";</script>", start)
    round_tripped = json.loads(html2[start:end])
    check("payload round-trips through the escaping",
          round_tripped["schema"] == payload_builder.SCHEMA_VERSION)
    check("hostile summary survives intact, escaped not mangled",
          any("hostile ticket" in (t.get("summary") or "")
              for t in round_tripped["tickets"]))

    check("report is emailable (<600kb)", os.path.getsize(out) < 600_000)

    print(f"report self-test: {passed}/{passed + failed}  ({os.path.getsize(out) // 1024}kb)")
    return 0 if failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="ledger.db -> self-contained HTML")
    ap.add_argument("--db", default="ledger.db")
    ap.add_argument("--out", default="report.html")
    ap.add_argument("--release")
    ap.add_argument("--project")
    ap.add_argument("--max-events", type=int, default=200)
    ap.add_argument("--max-rows", type=int, default=40)
    ap.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                    help="skip a discovered table. Repeatable.")
    ap.add_argument("--hero", default=payload_builder.DEFAULT_HERO,
                    choices=sorted(payload_builder.HEROES),
                    help="which metric gets the big number on Overview "
                         f"(default: {payload_builder.DEFAULT_HERO})")
    ap.add_argument("--demo", action="store_true",
                    help="render a synthetic ledger - no ledger.db needed")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return _self_test()

    db = a.db
    if a.demo:
        import tempfile
        from _demo_ledger import write_demo
        db = write_demo(os.path.join(tempfile.mkdtemp(), "demo.db"))
        print("demo ledger (synthetic - not your data)", file=sys.stderr)
    elif not os.path.exists(db):
        print(f"no ledger at {db}. try --demo, or point --db at it.", file=sys.stderr)
        return 2

    out = build_report(db, a.out, a.release, a.project, a.max_events,
                       a.max_rows, tuple(a.exclude), a.hero)
    size = os.path.getsize(out)
    print(f"{out}  {size // 1024}kb  self-contained", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
