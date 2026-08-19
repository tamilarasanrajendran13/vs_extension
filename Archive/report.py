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
import re
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
        f"docket report {REPORT_VERSION} - "
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

    # Extra tabs (Reference, Knowledge, Slices) are self-contained, script-free
    # .page sections injected before the colophon; the dashboard router lists
    # them automatically, so no app.js/app.css change is needed. They render
    # from the SAME payload the rest of the page does (B2) - extra_tabs.py no
    # longer opens the ledger behind payload_builder's back.
    # Never fatal: a report without the extra tabs beats no report.
    try:
        import extra_tabs
        html = extra_tabs.inject(html, payload)
    except Exception as e:
        print(f"note: extra tabs skipped ({e})", file=sys.stderr)

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

# A stub DOM, nine methods wide, that is enough to LOAD dashboard/app.js under
# node without a browser. app.js only touches `document` at load time to decide
# whether to wait for DOMContentLoaded, so the stub never has to be a real DOM -
# it just has to exist. What we then call are the renderer's PURE decision
# functions (findingsView / verdictView), which return what to say and touch no
# element at all. That is the point: the three findings states and the verdict
# fold get EXECUTED here, not grepped for.
#
# argv[2] = absolute path to app.js, argv[3] = one payload ticket row as JSON.
_PROBE_JS = '''"use strict";
var APP = process.argv[2];
var ROW = JSON.parse(process.argv[3]);
function stub() {
  return {
    style: {}, dataset: {},
    classList: { add: function () {}, remove: function () {},
                 toggle: function () {}, contains: function () { return false; } },
    appendChild: function () {}, setAttribute: function () {},
    addEventListener: function () {}
  };
}
global.window = { addEventListener: function () {} };
global.document = {
  readyState: "complete",
  addEventListener: function () {},
  querySelector: function () { return null; },
  querySelectorAll: function () { return []; },
  getElementById: function () { return null; },
  createElement: stub,
  createTextNode: stub,
  createElementNS: stub
};
require(APP);
var D = global.window.DocketDashboard || {};
var has_f = typeof D.findingsView === "function";
var has_v = typeof D.verdictView === "function";
var out = { has_findings_view: has_f, has_verdict_view: has_v };
if (has_f) {
  out.nul = D.findingsView(null);
  out.empty = D.findingsView({ by_status: {}, confirmed: 0, proposed: 0 });
  out.counts = D.findingsView(
    { by_status: { CONFIRMED: 2, PROPOSED: 1 }, confirmed: 2, proposed: 1 });
  out.taxonomy = D.findingsView({ by_status: {
    DOCKET_FOUND_IT: 1, TEST_GAP_FOUND: 2, SPEC_GAP_FOUND: 3,
    REGRESSION_RISK_FOUND: 4, HARNESS_FAILURE: 5, NO_FINDING: 6 } });
  // findings.verdict is the TAXONOMY column; findings.status is the
  // lifecycle. They are two vocabularies over the same rows and the page
  // has to keep them apart.
  out.verdicts = D.findingsView(
    { by_status: { CONFIRMED: 1 },
      by_verdict: { TEST_GAP_FOUND: 1, NO_FINDING: 2 } });
  out.live = D.findingsView(JSON.parse(process.argv[4]));
}
if (has_v) {
  out.verdict = D.verdictView(ROW);
  out.no_verdict = D.verdictView({ outcome: "merged", verdict: null });
  // Three verdicts that display_state deliberately folds into one word.
  // Whatever the chip reads, it must not read the same for all three.
  out.vocab = ["failed", "blocked", "halted"].map(function (s) {
    return D.verdictView({ outcome: "running", verdict: {
      headline: "headline for " + s, state: s, display_state: "halted" } });
  });
}
console.log(JSON.stringify(out));
'''


def _self_test() -> int:
    import tempfile
    from _demo_ledger import write_demo

    passed = failed = 0
    skipped: list[str] = []

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
    #
    # The fixture poke goes through _demo_ledger, which owns the fixtures.
    # This module renders payloads; payload_builder.py is the only dashboard
    # component that opens a database (B2) and that includes its tests.
    #
    # The escaping canary rides on runs.summary. If this ledger's CONTRACT does
    # not map summary (apply_contract found no summary column), there is no field
    # to carry the hostile string, so the summary-specific checks are n/a here -
    # the escaping still runs against the whole payload, just with no summary.
    import _demo_ledger
    _summary_col = (payload_builder.CONTRACT.get("runs", {})
                    .get("columns", {}).get("summary"))
    if _summary_col:
        _demo_ledger.set_run_field(
            db, _summary_col,
            "</script><script>alert(1)</script> hostile ticket")
    build_report(db, out)
    html2 = open(out, encoding="utf-8").read()
    check("script-tag injection neutralised", "<script>alert(1)" not in html2)
    if _summary_col:
        check("payload survives escaping", "\\u003c/script" in html2)
    else:
        check("payload survives escaping (summary unmapped: n/a)", True)
    check("still exactly two script tags", html2.count("<script>") == 2)

    # the payload the page gets back must be the payload we put in
    start = html2.index("window.DOCKET_PAYLOAD = ") + len("window.DOCKET_PAYLOAD = ")
    end = html2.index(";</script>", start)
    round_tripped = json.loads(html2[start:end])
    check("payload round-trips through the escaping",
          round_tripped["schema"] == payload_builder.SCHEMA_VERSION)
    if _summary_col:
        check("hostile summary survives intact, escaped not mangled",
              any("hostile ticket" in (t.get("summary") or "")
                  for t in round_tripped["tickets"]))
    else:
        check("hostile summary (summary unmapped: n/a)", True)

    # The hard emailability limit is WARN_BYTES (4MB). This self-test asserts a
    # tighter bound on the DEMO so growth is noticed early - but the demo now
    # carries conversations, gate scores, agent roster and architecture, all
    # legitimately. 1MB still leaves 4x headroom under the real limit.
    check("report is emailable (<1MB)", os.path.getsize(out) < 1_000_000)

    # the three injected tabs are present and still self-contained
    check("reference/knowledge/slices tabs injected",
          all(f'id="page-{p}"' in html for p in ("reference", "knowledge", "slices")))

    # gate-name drift pin, JS side: app.js must not lookup the old 9-stage
    # names ("test-spec", "develop", "review", "security", "qa", "context")
    # that vanished when gate_order moved to the real 7 ledger gate names.
    # Quoted-literal needles only - "qa" alone would false-positive on
    # "qa_e2e", and GATE_ABBR values like "DEV" are unrelated code lookups.
    _app_js = _read("app.js")
    # V4.4 note: the needle for test-spec is the LOOKUP shape its siblings
    # use, not the bare quoted string - the approved TOPOLOGY legitimately
    # names the AGENT "test-spec" (agents/test-spec.md), and this pin is
    # about gate-name lookups, not agent names.
    _stale_needles = ['X("test-spec")', 'X("develop")', 'X("review")',
                       'X("security")', 'X("qa")', '"context",']
    for _needle in _stale_needles:
        check(f"app.js has no stale gate-name literal {_needle!r}",
              _needle not in _app_js)

    # ----------------------------------------------------------------------
    # Task 30: doc/code drift detector for RUN_MONITOR_SPEC.md.
    #
    # The spec spent several sessions recording the Run Flow OUTPUT and
    # EVIDENCE bottom-panel tabs as not built, long after they shipped. A
    # stale "not built" line is worse than no line: it sends the next
    # session to rebuild code that already exists, or to re-defer a
    # requirement that is already met. Make the disagreement a test failure
    # instead of a reading mistake.
    #
    # This pin has TWO halves on purpose, so neither can pass vacuously:
    #   CODE half - run_flow.js must still declare both tabs and both
    #     renderers. Delete them and this fails, which is the honest
    #     signal that the doc is no longer wrong.
    #   DOC half  - while the code half holds, RUN_MONITOR_SPEC.md must
    #     still DESCRIBE both tabs (at least one paragraph naming both - the
    #     doc writes tab names in caps), and no such paragraph may carry a
    #     "not built"/"deferred" word.
    # The two halves together mean: whichever side moves, the pair is
    # re-checked. Behavioral proof that the tabs really render lives in
    # extension/scripts/preview_run_flow.js --check (RENDER_MUST rows keyed
    # "output"/"evidence") and preview_run_monitor.js --check (T1-T8, H10);
    # this is a drift pin, not a second copy of those assertions.
    #
    # Fix round 1 (reviewer finding F3): the "still describes them" half is
    # NOT decoration. Without it, emptying or truncating the spec left the
    # pin fully green - it could prove "no stale sentence" but never "the
    # spec still says anything", and silence is how the last three
    # revisions of this claim rotted. A pin that a `: > FILE` satisfies is
    # not a pin.
    #
    # A missing file is reported as a named failure, not a traceback: both
    # sides of a drift pin have to exist for the pin to mean anything, and
    # "the file is gone" is a finding, not an accident of the harness.
    _spec_path = os.path.join(HERE, "RUN_MONITOR_SPEC.md")
    _flow_path = os.path.join(HERE, "extension", "src", "run_flow.js")
    _spec_md = _flow_js = ""
    for _label, _p in (("RUN_MONITOR_SPEC.md", _spec_path),
                       ("extension/src/run_flow.js", _flow_path)):
        _ok = os.path.isfile(_p)
        check(f"{_label} exists - both halves of the doc/code drift pin "
              f"must be readable for the pin to mean anything", _ok)
        if _ok:
            _txt = open(_p, encoding="utf-8").read()
            if _label.endswith(".md"):
                _spec_md = _txt
            else:
                _flow_js = _txt
    for _needle in ('data-tab="output"', 'data-tab="evidence"',
                    "function renderOutput(", "function renderEvidence("):
        check(f"run_flow.js still ships the OUTPUT/EVIDENCE tab {_needle!r} "
              f"- the RUN_MONITOR_SPEC.md drift pin is about THIS code",
              _needle in _flow_js)
    _unbuilt_words = ("unimplemented", "unbuilt", "not built", "never built",
                      "not implemented", "never implemented", "defer")
    _tab_paras, _drift_paras = [], []
    for _para in _spec_md.split("\n\n"):
        _flat = " ".join(_para.split())
        if not (re.search(r"\bOUTPUT\b", _flat)
                and re.search(r"\bEVIDENCE\b", _flat)):
            continue
        _tab_paras.append(_flat)
        _hit = [w for w in _unbuilt_words if w in _flat.lower()]
        if _hit:
            _drift_paras.append((_hit, _flat[:160]))
    check("RUN_MONITOR_SPEC.md still DESCRIBES the OUTPUT and EVIDENCE tabs "
          "- an emptied or silent spec is drift too, not a clean pin",
          bool(_tab_paras))
    check("RUN_MONITOR_SPEC.md does not call the shipped Run Flow "
          "OUTPUT/EVIDENCE tabs unbuilt or deferred (doc/code drift)",
          not _drift_paras)
    for _hit, _snippet in _drift_paras:
        print(f"  DRIFT {_hit}: {_snippet}")

    # ----------------------------------------------------------------------
    # The finding taxonomy and THE run verdict have to be RENDERED, not just
    # computed. payload_builder already carries payload.findings (from the
    # findings table) and payload.tickets[].verdict (run_verdict.py's single
    # terminal projection). A payload with no renderer is a release blocker:
    # the taxonomy is the metric this product is judged on, and the collapsed
    # Runs row used to read runs.outcome ("running") while the authority said
    # the run was complete.
    #
    # Three states, three DIFFERENT sentences, because they are three
    # different facts:
    #   findings is null      no findings table in this ledger  -> unavailable
    #   by_status is {}       there is one, and it is empty     -> none recorded
    #   by_status has counts  real numbers, one row per status the ledger used
    # ----------------------------------------------------------------------
    import shutil
    import subprocess
    from pathlib import Path as _Path
    import ledger as _ledger
    import run_verdict as _rv

    _css = _read("app.css")
    TAXONOMY = ("DOCKET_FOUND_IT", "TEST_GAP_FOUND", "SPEC_GAP_FOUND",
                "REGRESSION_RISK_FOUND", "HARNESS_FAILURE", "NO_FINDING")
    ALL_GATES = ("comprehension", "frozen_tests", "unit_tests", "blind_review",
                 "security_snyk", "qa_e2e", "mutation")

    fdb = _Path(tmp) / "findings.db"
    _ledger.init(fdb)
    frun = _ledger.start_run("FIND-1", project="onetest", db=fdb)
    for _g in ALL_GATES:
        _ledger.gate(frun, "FIND-1", _g, "pass", actor="selftest", db=fdb)
    _ledger.end_run(frun, "merged", db=fdb)
    _ledger.record_finding(frun, "FIND-1", "surviving_mutant",
                           "a mutant in compare.py survived every test",
                           evidence={"mutant": "compare.py:41"},
                           status="CONFIRMED", verdict="TEST_GAP_FOUND",
                           db=fdb)
    _ledger.record_finding(frun, "FIND-1", "qa_failure", "AC2 is unmet",
                           evidence={"ac": "AC2"}, db=fdb)
    fout = os.path.join(tmp, "findings.html")
    build_report(str(fdb), fout)
    fhtml = open(fout, encoding="utf-8").read()
    _fs = fhtml.index("window.DOCKET_PAYLOAD = ") + len("window.DOCKET_PAYLOAD = ")
    fpay = json.loads(fhtml[_fs:fhtml.index(";</script>", _fs)])
    frow = fpay["tickets"][0]

    check("fixture: the payload carries findings.by_status",
          (fpay.get("findings") or {}).get("by_status")
          == {"CONFIRMED": 1, "PROPOSED": 1})
    # The lifecycle status says how far a finding got through triage. The
    # TAXONOMY verdict says what it is. Task 4 shipped a renderer that can
    # draw six taxonomy labels and a payload that carried none of them, so
    # the hero could only ever count triage states.
    check("fixture: the payload carries findings.by_verdict (the taxonomy "
          "column, not just the lifecycle status)",
          (fpay.get("findings") or {}).get("by_verdict")
          == {"TEST_GAP_FOUND": 1})
    check("fixture: the payload carries the folded run verdict",
          isinstance(frow.get("verdict"), dict)
          and bool(frow["verdict"].get("headline")))
    check("fixture: the demo ledger has no findings table (the null case)",
          payload_builder.build(db).get("findings") is None)

    # The verdict on the page is run_verdict's, field for field. Nothing
    # between the ledger and the reader gets a second opinion.
    _authority = _rv.run_verdict(frun, fdb)
    check("the payload's run verdict is run_verdict.run_verdict() verbatim",
          all(frow["verdict"].get(k) == _authority.get(k)
              for k in ("run_id", "headline", "state", "display_state",
                        "is_success", "is_terminal", "needs_human",
                        "resumable")))

    # ---- the renderer's contract, read off the shipped files
    check("the page has a findings surface in its markup",
          'class="findings"' in fhtml)
    check("the findings surface is never data-needs-hidden "
          "(a null findings table must SAY unavailable, not vanish)",
          'data-needs="findings"' not in fhtml)
    check("render() paints the findings surface",
          "function renderFindings" in _app_js and "renderFindings(" in _app_js)
    check("the run verdict has a dedicated surface, styled",
          "verdict-block" in _app_js and ".verdict-block" in _css)
    check("the collapsed Runs row reads the typed verdict, not the prose",
          "display_state" in _app_js)

    # Each taxonomy status must be tellable apart from the other five.
    for _t in TAXONOMY:
        check(f"taxonomy status {_t} is visually distinguishable",
              ".fs-" + _t.lower() in _css)

    # ...and the renderer must not KNOW the vocabulary. Statuses come from the
    # ledger; a renderer holding its own list renders nothing the day the
    # ledger grows a status, and a renderer that can NAME a verdict is one
    # edit away from deciding one (invariant 1).
    for _t in TAXONOMY:
        check(f"app.js hardcodes no {_t} literal - statuses come from the "
              f"ledger", _t not in _app_js)

    # ---- behaviour, not grep: run the renderer's decisions under node
    _node = shutil.which("node")
    if _node is None:
        skipped.append(
            "renderer behaviour under node (node is not on PATH). The static "
            "contract checks above ran; the three findings states and the "
            "verdict fold were NOT executed.")
    else:
        probe = os.path.join(tmp, "render_probe.js")
        with open(probe, "w", encoding="ascii") as fh:
            fh.write(_PROBE_JS)
        proc = subprocess.run(
            [_node, probe, os.path.join(BUNDLE, "app.js"), json.dumps(frow),
             json.dumps(fpay.get("findings"))],
            capture_output=True, text=True, timeout=120)
        try:
            view = json.loads((proc.stdout or "").strip().splitlines()[-1])
        except Exception:
            view = {}
            print("  node probe produced no JSON:",
                  ((proc.stdout or "") + (proc.stderr or ""))[:400])
        check("app.js exposes its findings/verdict decisions for test",
              bool(view.get("has_findings_view"))
              and bool(view.get("has_verdict_view")))
        nul = view.get("nul") or {}
        emp = view.get("empty") or {}
        cnt = view.get("counts") or {}
        tax = view.get("taxonomy") or {}
        check("findings null renders unavailable, and invents no zero",
              nul.get("state") == "unavailable"
              and "unavailable" in str(nul.get("message") or "").lower()
              and "0" not in str(nul.get("message") or "")
              and not nul.get("rows"))
        check("findings present but empty renders no-findings-recorded",
              emp.get("state") == "empty"
              and "no findings recorded" in str(emp.get("message") or "").lower())
        check("the two empty states are DIFFERENT sentences",
              bool(nul.get("message")) and bool(emp.get("message"))
              and nul.get("message") != emp.get("message"))
        check("real counts render one row per recorded status",
              [(r.get("status"), r.get("count")) for r in cnt.get("rows") or []]
              == [("CONFIRMED", 2), ("PROPOSED", 1)])
        check("every taxonomy status gets its own row and its own class",
              sorted(r.get("status") for r in tax.get("rows") or [])
              == sorted(TAXONOMY)
              and len({r.get("cls") for r in tax.get("rows") or []})
              == len(TAXONOMY))

        # ---- the taxonomy column, end to end. by_verdict is a SECOND
        # vocabulary over the same rows; folding it into by_status would
        # double-count every finding, so it renders as its own set of rows.
        vd = view.get("verdicts") or {}
        check("a taxonomy verdict renders as its own row, in its own class, "
              "kept apart from the lifecycle rows",
              [(r.get("status"), r.get("count"), r.get("cls"))
               for r in vd.get("verdicts") or []]
              == [("NO_FINDING", 2, "fs-no_finding"),
                  ("TEST_GAP_FOUND", 1, "fs-test_gap_found")]
              and [r.get("status") for r in vd.get("rows") or []]
              == ["CONFIRMED"])
        live = view.get("live") or {}
        check("the fixture ledger's real TEST_GAP_FOUND verdict travels "
              "ledger -> payload -> renderer without anyone naming it",
              [(r.get("status"), r.get("count"))
               for r in live.get("verdicts") or []]
              == [("TEST_GAP_FOUND", 1)])
        check("a payload with no by_verdict key renders no taxonomy rows and "
              "claims nothing", not (cnt.get("verdicts") or []))
        vv = view.get("verdict") or {}
        check("the rendered run verdict headline is the authority's, verbatim",
              vv.get("headline") == _authority.get("headline")
              and vv.get("display_state") == _authority.get("display_state"))
        nv = view.get("no_verdict") or {}
        check("a run with no recorded verdict says so and invents nothing",
              nv.get("state") == "unavailable" and nv.get("headline") is None
              and bool(nv.get("message")))

        # display_state exists to give the runs row a four-word vocabulary,
        # and it buys that by folding blocked, failed AND halted into
        # "halted" (run_verdict.display_state). Labelling a chip from it
        # paints a harness death as "awaiting human" - invariant 8 run
        # backwards, and it collapses three of the six representations the
        # Runs tab is required to keep apart. The label comes from run_state.
        vocab = view.get("vocab") or []
        byst = {v.get("run_state"): v for v in vocab if isinstance(v, dict)}
        check("a failed run reads FAILED, never 'awaiting human'",
              (byst.get("failed") or {}).get("status") == "failed"
              and (byst.get("failed") or {}).get("label") == "failed")
        check("a blocked run keeps its own word",
              (byst.get("blocked") or {}).get("status") == "blocked"
              and (byst.get("blocked") or {}).get("label") == "blocked")
        check("a halted run still reads as awaiting a human",
              (byst.get("halted") or {}).get("status") == "halted"
              and (byst.get("halted") or {}).get("label") == "awaiting human")
        check("failed / blocked / halted are three distinct chips, not one",
              len({(v.get("status"), v.get("label")) for v in vocab
                   if isinstance(v, dict)}) == 3)
        check("a row with no verdict still labels itself from its outcome",
              nv.get("status") == "merged" and nv.get("label") == "merged")

    # Task 11 (B12): a config-disabled scanner reaches the STATIC report as
    # itself. Built from its own tiny ledger rather than the shared demo
    # fixture (whose security rows are pre-M-4 unknown+reason rows, kept as
    # the historical shape they are): a `skipped` gate row must arrive in
    # the inlined payload as "skipped", carrying its why, and must never
    # arrive as pass, as never_reached, or as a 0 score.
    import ledger as _led_t11
    from pathlib import Path as _Path_t11
    _t11_db = os.path.join(tmp, "t11.db")
    _led_t11.init(_Path_t11(_t11_db))
    _t11_run = _led_t11.start_run("T11-1", project="onetest",
                                 db=_Path_t11(_t11_db))
    for _g, _o in (("comprehension", "pass"), ("frozen_tests", "pass"),
                   ("unit_tests", "pass"), ("blind_review", "pass"),
                   ("security_snyk", "skipped"), ("qa_e2e", "pass"),
                   ("mutation", "pass")):
        _led_t11.gate(_t11_run, "T11-1", _g, _o, actor="t",
                      unknown_reason=("disabled by config"
                                      if _o == "skipped" else None),
                      details=({"reason": "disabled by config"}
                               if _o == "skipped" else None),
                      db=_Path_t11(_t11_db))
    _t11_out = os.path.join(tmp, "t11.html")
    build_report(_t11_db, _t11_out)
    _t11_html = open(_t11_out, encoding="utf-8").read()
    _s = _t11_html.index("window.DOCKET_PAYLOAD = ") + len(
        "window.DOCKET_PAYLOAD = ")
    _t11_pay = json.loads(_t11_html[_s:_t11_html.index(";</script>", _s)])
    _t11_gate = next(g for t in _t11_pay["tickets"] for g in t["gates"]
                     if g["name"] == "security_snyk")
    check("a config-disabled scanner reaches the static report as SKIPPED "
          "- never pass, never never_reached",
          _t11_gate["result"] == "skipped")
    check("the skipped gate carries its why into the report, and no "
          "invented score",
          "disabled by config" in (_t11_gate.get("detail") or "")
          and _t11_gate.get("score") is None)
    check("the report styles skipped as its own mark (not unknown's, not "
          "pass's)", ".mark.skipped" in _css)

    # Task 11 fix round 1 (review I1): the OTHER half of the same fact - a
    # PRE-CONTRACT `unknown, disabled by config` row (all 37 of the live
    # ledger's security rows are this shape) is unmeasured, not a defect,
    # so the row it lands on may not be painted in the defect colour. Built
    # end to end: real ledger row -> payload_builder -> the inlined payload
    # the page renders from -> the stylesheet that colours the chip.
    _t11b_run = _led_t11.start_run("T11-2", project="onetest",
                                   db=_Path_t11(_t11_db))
    for _g, _o in (("comprehension", "pass"), ("frozen_tests", "pass"),
                   ("unit_tests", "pass"), ("blind_review", "pass"),
                   ("security_snyk", "unknown"), ("qa_e2e", "pass"),
                   ("mutation", "pass")):
        _led_t11.gate(_t11b_run, "T11-2", _g, _o, actor="t",
                      unknown_reason=("disabled by config"
                                      if _o == "unknown" else None),
                      db=_Path_t11(_t11_db))
    # The pre-contract on-disk shape: no evidence envelope on ANY row of
    # the run, which is what the live ledger actually holds (measured: 0 of
    # the 37 runs carrying an unknown security row has a single stamped
    # row). Fix round 2 / review N1: unstamping only the ONE row would not
    # be a legacy run at all - a stamped sibling proves the contract-era
    # write path ran, and run_verdict then reads the silence as a stamping
    # failure and keeps the row red. See run_verdict._run_is_contract_era.
    with _led_t11.connect(_Path_t11(_t11_db)) as _con_t11:
        for _gid, _dj in [(r["gate_id"], r["details_json"]) for r in
                          _con_t11.execute(
                              "SELECT gate_id, details_json FROM gates "
                              "WHERE run_id=?", (_t11b_run,)).fetchall()]:
            _d = json.loads(_dj or "{}")
            _d.pop("evidence", None)
            _d.pop("evidence_error", None)
            _con_t11.execute(
                "UPDATE gates SET details_json=? WHERE gate_id=?",
                (json.dumps(_d), _gid))
    build_report(_t11_db, _t11_out)
    _t11b_html = open(_t11_out, encoding="utf-8").read()
    _s2 = _t11b_html.index("window.DOCKET_PAYLOAD = ") + len(
        "window.DOCKET_PAYLOAD = ")
    _t11b_pay = json.loads(_t11b_html[_s2:_t11b_html.index(";</script>", _s2)])
    _t11b_t = next(t for t in _t11b_pay["tickets"] if t["issue"] == "T11-2")
    _t11b_state = (_t11b_t.get("verdict") or {}).get("state")

    def _verdict_css(state):
        _k = ".disposition .verdict." + state
        _i = _css.find(_k)
        return _css[_i:_css.find("}", _i)] if _i >= 0 else ""

    check("a pre-contract 'disabled by config' row never lands in the red "
          "band - it is unmeasured, not a defect",
          _t11b_state == "halted"
          and (_t11b_t["verdict"].get("is_success") is False)
          and "carmine" not in _verdict_css(_t11b_state)
          and "carmine" in _verdict_css("blocked"))
    check("and the row says so in the reader's own 'why' slot, reason "
          "included",
          "UNMEASURED" in (_t11b_t["verdict"].get("headline") or "")
          and "disabled by config" in _t11b_t["verdict"]["headline"])

    # every state the verdict vocabulary can produce is styled; none of them
    # inherits another's colour by accident.
    for _s in ("failed", "blocked", "halted", "complete", "delivered",
               "stopped", "running"):
        check(f"run state {_s} is styled distinctly on the Runs chip",
              ".disposition .verdict." + _s in _css)

    # ----------------------------------------------------------------------
    # V4.4: the desktop-approved SUBWAY map is the Architecture tab's ONE
    # visualization, rendered from the ONE TOPOLOGY authority carried in
    # app.js; the old network chart and the stale RBAC claim are GONE.
    # Static pins here; the EXECUTED contract (stations render, player
    # advances, selection highlights) lives in
    # extension/scripts/dashboard_host.js --check.
    check("V4.4: app.js ships the TOPOLOGY authority",
          "var TOPOLOGY" in _app_js)
    check("V4.4: the stale 'Governor (RBAC)' claim is gone from the "
          "renderer", "Governor (RBAC)" not in _app_js
          and "governor (RBAC)" not in _app_js)
    check("V4.4: the old network chart is gone - no buildArchSVG, no "
          "RBAC rows markup",
          "buildArchSVG" not in _app_js and "arch-rbac" not in _app_js)
    check("V4.4: the governor is described as the state machine and "
          "policy knobs, never an allow/ask/deny arbiter",
          "state machine" in _app_js and "policy knobs" in _app_js)
    check("V4.4: subway styles land in the stylesheet",
          ".an .st-m" in _css and ".aedge.lane-repair" in _css
          and ".arch-stepbar" in _css and ".conc-grp" in _css)
    check("V4.4: reduced motion is honored in the stylesheet",
          "prefers-reduced-motion" in _css)

    # Executed, not grepped: the topology counts, the scenario table and
    # the qa-convergence classification, read off the LOADED renderer.
    if _node is not None:
        _t_probe = os.path.join(tmp, "topology_probe.js")
        with open(_t_probe, "w", encoding="ascii") as fh:
            fh.write(
                '"use strict";\n'
                + _PROBE_JS.split("require(APP);")[0]
                + 'require(APP);\n'
                  'var D = global.window.DocketDashboard || {};\n'
                  'var T = D.topology || {};\n'
                  'var S = D.archScenarios || [];\n'
                  'function hits(f, t, sub) {\n'
                  '  return (T.edges || []).filter(function (e) {\n'
                  '    return e.from === f && e.to === t && (!sub ||\n'
                  '      (e.label || "").toLowerCase().indexOf(\n'
                  '        sub.toLowerCase()) >= 0);\n'
                  '  }).length;\n'
                  '}\n'
                  'var refsOk = S.length > 0 && S.every(function (sc) {\n'
                  '  return (sc.steps || []).every(function (st) {\n'
                  '    var refs = (st.e || []).slice();\n'
                  '    (st.par || []).forEach(function (br) {\n'
                  '      refs = refs.concat(br.e || []); });\n'
                  '    return refs.every(function (r) {\n'
                  '      return hits(r[0], r[1], r[2]) === 1; });\n'
                  '  });\n'
                  '});\n'
                  'var qac = ((T.nodes || []).filter(function (n) {\n'
                  '  return n.id === "qa_convergence"; })[0] || {});\n'
                  'console.log(JSON.stringify({\n'
                  '  nodes: (T.nodes || []).length,\n'
                  '  edges: (T.edges || []).length,\n'
                  '  conc: (T.concurrency || []).length,\n'
                  '  scenarios: S.length,\n'
                  '  refsOk: refsOk,\n'
                  '  qac_kind: qac.kind || null\n'
                  '}));\n')
        _t_proc = subprocess.run(
            [_node, _t_probe, os.path.join(BUNDLE, "app.js"), "{}", "null"],
            capture_output=True, text=True, timeout=120)
        try:
            _t_view = json.loads(
                (_t_proc.stdout or "").strip().splitlines()[-1])
        except Exception:
            _t_view = {}
            print("  topology probe produced no JSON:",
                  ((_t_proc.stdout or "") + (_t_proc.stderr or ""))[:400])
        check("V4.4: the approved topology loads - 44 nodes / 74 edges / "
              "7 concurrency groups",
              _t_view.get("nodes") == 44 and _t_view.get("edges") == 74
              and _t_view.get("conc") == 7)
        check("V4.4: the 16-scenario player ships and every reference "
              "resolves uniquely against TOPOLOGY",
              _t_view.get("scenarios") == 16
              and _t_view.get("refsOk") is True)
        check("V4.4: qa-convergence stays DETERMINISTIC in the shipped "
              "topology", _t_view.get("qac_kind") == "deterministic")

    # ----------------------------------------------------------------------
    # V4.4: the FINDINGS TAB - the approved investigation workspace, driven
    # by payload.kernel. Static pins here plus the EXECUTED model seam:
    # findingsTabModel computes the command-header metrics and repair splits
    # from kernel rows without ever naming a status - a ledger that grows a
    # status renders it with no edit.
    check("V4.4: the Findings tab page ships in the bundle",
          'id="page-findings"' in html)
    check("V4.4: findings workspace styles land in the stylesheet",
          ".fx-queue" in _css and ".fx-detail" in _css
          and ".fx-chain" in _css and ".rx-seg" in _css)
    check("V4.4: app.js exposes the findings tab model seam",
          "findingsTabModel" in _app_js)
    if _node is not None:
        _f_probe = os.path.join(tmp, "findings_probe.js")
        _KFIX = {
            "workflows": [
                {"workflow_id": "wf-A-1", "ticket_id": "A-1",
                 "created_at": "2026-08-01 10:00:00", "state": "BLOCKED"},
                {"workflow_id": "wf-A-2", "ticket_id": "A-1",
                 "created_at": "2026-08-02 10:00:00", "state": "READY"}],
            "transitions": [],
            "failures": [
                {"failure_id": 7, "workflow_id": "wf-A-1",
                 "source_stage": "qa", "failure_class": "test_failure",
                 "owner": "docket", "retryable": 1,
                 "evidence": "e", "at": "2026-08-01 11:00:00"}],
            "repairs": [
                {"attempt_id": 1, "workflow_id": "wf-A-1", "failure_id": 7,
                 "strategy": "qa-repair", "started_at": "x",
                 "resolved_at": "y", "converted": 1, "rechecks": "[]"},
                {"attempt_id": 2, "workflow_id": "wf-A-1", "failure_id": 7,
                 "strategy": "qa-repair", "started_at": "x",
                 "resolved_at": "y", "converted": 0, "rechecks": "[]"},
                {"attempt_id": 3, "workflow_id": "wf-A-1", "failure_id": 7,
                 "strategy": "dev-repair", "started_at": "x",
                 "resolved_at": None, "converted": None, "rechecks": None}],
            "findings": [
                {"finding_id": 1, "created_at": "2026-08-01 12:00:00",
                 "run_id": "r1", "ticket_id": "A-1", "kind": "qa_failure",
                 "status": "WEIRD_STATE", "verdict": None,
                 "summary": "s1", "evidence": "ev1", "supersedes": None},
                {"finding_id": 2, "created_at": "2026-08-02 12:00:00",
                 "run_id": "r2", "ticket_id": "A-1", "kind": "mutation",
                 "status": "CONFIRMED", "verdict": "TG_X",
                 "summary": "s2", "evidence": "ev2", "supersedes": 1}],
            "meta": {"populations": {}, "caps": {}},
        }
        with open(_f_probe, "w", encoding="ascii") as fh:
            fh.write(
                '"use strict";\n'
                + _PROBE_JS.split("require(APP);")[0]
                + 'require(APP);\n'
                  'var D = global.window.DocketDashboard || {};\n'
                  'var K = JSON.parse(process.argv[3]);\n'
                  'var out = {\n'
                  '  has: typeof D.findingsTabModel === "function" };\n'
                  'if (out.has) {\n'
                  '  out.nul = D.findingsTabModel(null);\n'
                  '  out.m = D.findingsTabModel(K);\n'
                  '}\n'
                  'console.log(JSON.stringify(out));\n')
        _f_proc = subprocess.run(
            [_node, _f_probe, os.path.join(BUNDLE, "app.js"),
             json.dumps(_KFIX)],
            capture_output=True, text=True, timeout=120)
        try:
            _f_view = json.loads(
                (_f_proc.stdout or "").strip().splitlines()[-1])
        except Exception:
            _f_view = {}
            print("  findings probe produced no JSON:",
                  ((_f_proc.stdout or "") + (_f_proc.stderr or ""))[:400])
        _m = _f_view.get("m") or {}
        check("V4.4: findingsTabModel exists and a null kernel is "
              "UNAVAILABLE, never zero",
              _f_view.get("has") is True
              and (_f_view.get("nul") or {}).get("state") == "unavailable")
        check("V4.4: header metrics computed from kernel rows - total, "
              "with/lacking a verdict",
              _m.get("total") == 2 and _m.get("with_verdict") == 1
              and _m.get("lacking_verdict") == 1)
        check("V4.4: statuses are tallied from the data - a status this "
              "code has never heard of is counted, not dropped",
              any(r.get("status") == "WEIRD_STATE" and r.get("count") == 1
                  for r in _m.get("by_status") or []))
        check("V4.4: repair economics split open / converted / "
              "did-not-convert from the recorded converted flag",
              _m.get("repairs", {}).get("converted") == 1
              and _m.get("repairs", {}).get("did_not") == 1
              and _m.get("repairs", {}).get("open") == 1)
        check("V4.4: the finding -> failure -> repair chain uses the "
              "RECORDED failure_id join and the derived ticket join is "
              "labeled derived",
              _m.get("chain_provenance", {}).get("workflow") == "derived"
              and _m.get("chain_provenance", {}).get("repair") == "recorded")

    # ----------------------------------------------------------------------
    # V4.4: the NOW-LINE and the four-authority liveness rule. A run may
    # render as ACTIVE only when the host process is alive, the host's
    # projection names the run, the payload records it running, and its
    # workflow is undecided. Every other combination is its own honest
    # sentence. The renderer only INTERSECTS authority outputs
    # (payload.liveness + window.DOCKET_HOST); it invents no lifecycle
    # vocabulary of its own.
    check("V4.4 now-line: the masthead carries the now-line host",
          'id="nowline"' in html)
    check("V4.4 now-line: app.js exports the nowLineModel seam",
          "nowLineModel" in _app_js)
    check("V4.4 now-line: styles ship", ".nowline" in _css)
    if _node is not None:
        _l_probe = os.path.join(tmp, "live_probe.js")
        with open(_l_probe, "w", encoding="ascii") as fh:
            fh.write(
                '"use strict";\n'
                + _PROBE_JS.split("require(APP);")[0]
                + 'require(APP);\n'
                  'var D = global.window.DocketDashboard || {};\n'
                  'var out = { has: typeof D.nowLineModel === "function" };\n'
                  'if (out.has) {\n'
                  '  var P = { scope: { project: "p" }, liveness: {\n'
                  '    recorded_running: [{ run_id: "r1", ticket_id: "T-1",\n'
                  '      workflow_state: "IMPLEMENTING",\n'
                  '      workflow_decided: false }],\n'
                  '    decided_states: ["BLOCKED", "CANCELLED", "READY",\n'
                  '                     "COMPLETED"] } };\n'
                  '  var PD = JSON.parse(JSON.stringify(P));\n'
                  '  PD.liveness.recorded_running[0].workflow_decided = true;\n'
                  '  PD.liveness.recorded_running[0].workflow_state = "READY";\n'
                  '  var P0 = { scope: {}, liveness: { recorded_running: [],\n'
                  '    decided_states: [] } };\n'
                  '  out.active = D.nowLineModel(P,\n'
                  '    { live: true, run: { run_id: "r1", state: "running" } });\n'
                  '  out.stale = D.nowLineModel(P, { live: false, run: null });\n'
                  '  out.unver = D.nowLineModel(P, null);\n'
                  '  out.decided = D.nowLineModel(PD,\n'
                  '    { live: true, run: { run_id: "r1", state: "running" } });\n'
                  '  out.starting = D.nowLineModel(P,\n'
                  '    { live: true, run: null });\n'
                  '  out.foreign = D.nowLineModel(P,\n'
                  '    { live: true, run: { run_id: "zz", state: "running" } });\n'
                  '  out.idle = D.nowLineModel(P0,\n'
                  '    { live: false, run: null });\n'
                  '}\n'
                  'console.log(JSON.stringify(out));\n')
        _l_proc = subprocess.run(
            [_node, _l_probe, os.path.join(BUNDLE, "app.js"), "{}", "null"],
            capture_output=True, text=True, timeout=120)
        try:
            _l_view = json.loads(
                (_l_proc.stdout or "").strip().splitlines()[-1])
        except Exception:
            _l_view = {}
            print("  now-line probe produced no JSON:",
                  ((_l_proc.stdout or "") + (_l_proc.stderr or ""))[:400])
        _lv = {k: (_l_view.get(k) or {}) for k in
               ("active", "stale", "unver", "decided", "starting",
                "foreign", "idle")}
        check("V4.4 now-line: ACTIVE requires ALL authorities to agree, "
              "and names the run and ticket",
              _l_view.get("has") is True
              and _lv["active"].get("state") == "active"
              and "r1" in str(_lv["active"].get("sentence"))
              and "T-1" in str(_lv["active"].get("sentence")))
        check("V4.4 now-line: a recorded-running row with a dead process "
              "renders history, with the truth stated",
              _lv["stale"].get("state") != "active"
              and "no live process" in str(_lv["stale"].get("sentence")))
        check("V4.4 now-line: a host that cannot verify a process never "
              "claims ACTIVE and says so",
              _lv["unver"].get("state") != "active"
              and "cannot verify" in str(_lv["unver"].get("sentence")))
        check("V4.4 now-line: a DECIDED workflow blocks the active claim "
              "and the decided state is named",
              _lv["decided"].get("state") != "active"
              and "READY" in str(_lv["decided"].get("sentence")))
        check("V4.4 now-line: process alive without a named run is "
              "'starting', never active",
              _lv["starting"].get("state") != "active"
              and "alive" in str(_lv["starting"].get("sentence")))
        check("V4.4 now-line: a live run the payload does not record is "
              "never presented from the ledger's mouth",
              _lv["foreign"].get("state") != "active"
              and "does not record" in str(_lv["foreign"].get("sentence")))
        check("V4.4 now-line: nothing recorded and nothing alive is idle",
              _lv["idle"].get("state") == "idle")
        check("V4.4 now-line: the seven situations are seven DIFFERENT "
              "sentences",
              len({str(v.get("sentence")) for v in _lv.values()}) == 7)

    # ----------------------------------------------------------------------
    # V4.4: NEEDS YOU. Derived from the workflow authority (latest workflow
    # per ticket, created_at then workflow_id tie-break - the approved V4.2
    # identity rule) plus the folded run verdicts. One row per ticket, the
    # workflow authority wins, older BLOCKED workflows are excluded ONLY
    # when a strictly newer workflow exists, and a comprehension halt may
    # claim questions only when question evidence is actually retained.
    check("V4.4 needs-you: the Overview carries the section host",
          'class="needs-you"' in html)
    check("V4.4 needs-you: the folded verdict line host ships",
          'class="verdict-line"' in html)
    check("V4.4 needs-you: app.js exports the needsYouModel seam",
          "needsYouModel" in _app_js)
    if _node is not None:
        _n_probe = os.path.join(tmp, "needsyou_probe.js")
        _NFIX = {
            "tickets": [
                {"issue": "T-A",
                 "verdict": {"state": "complete", "needs_human": False}},
                {"issue": "T-B",
                 "verdict": {"state": "blocked", "needs_human": True,
                             "headline": "BLOCKED - qa did not converge"}},
                {"issue": "T-C",
                 "verdict": {"state": "halted", "needs_human": True,
                             "headline": "HALTED - comprehension needs "
                                         "answers",
                             "at": "comprehension"},
                 "artifacts": []},
            ],
            "kernel": {
                "workflows": [
                    {"workflow_id": "wf-A-1", "ticket_id": "T-A",
                     "created_at": "2026-08-01 09:00:00",
                     "state": "BLOCKED"},
                    {"workflow_id": "wf-A-2", "ticket_id": "T-A",
                     "created_at": "2026-08-02 09:00:00", "state": "READY"},
                    {"workflow_id": "wf-B-1", "ticket_id": "T-B",
                     "created_at": "2026-08-03 09:00:00",
                     "state": "BLOCKED"},
                ],
                "transitions": [
                    {"workflow_id": "wf-A-1", "from_state": "VALIDATING",
                     "to_state": "BLOCKED", "reason": "old reason",
                     "at": "2026-08-01 10:00:00"},
                    {"workflow_id": "wf-B-1", "from_state": "VALIDATING",
                     "to_state": "BLOCKED",
                     "reason": "qa repair exhausted",
                     "at": "2026-08-03 10:00:00"},
                ],
            },
        }
        with open(_n_probe, "w", encoding="ascii") as fh:
            fh.write(
                '"use strict";\n'
                + _PROBE_JS.split("require(APP);")[0]
                + 'require(APP);\n'
                  'var D = global.window.DocketDashboard || {};\n'
                  'var P = JSON.parse(process.argv[3]);\n'
                  'var out = { has: typeof D.needsYouModel === "function" };\n'
                  'if (out.has) {\n'
                  '  out.m = D.needsYouModel(P);\n'
                  '  var PQ = JSON.parse(process.argv[3]);\n'
                  '  PQ.tickets[2].artifacts = [\n'
                  '    { rel_path: "context/questions.json" }];\n'
                  '  out.mq = D.needsYouModel(PQ);\n'
                  '  var PN = JSON.parse(process.argv[3]);\n'
                  '  PN.kernel = null;\n'
                  '  out.mn = D.needsYouModel(PN);\n'
                  '}\n'
                  'console.log(JSON.stringify(out));\n')
        _n_proc = subprocess.run(
            [_node, _n_probe, os.path.join(BUNDLE, "app.js"),
             json.dumps(_NFIX)],
            capture_output=True, text=True, timeout=120)
        try:
            _n_view = json.loads(
                (_n_proc.stdout or "").strip().splitlines()[-1])
        except Exception:
            _n_view = {}
            print("  needs-you probe produced no JSON:",
                  ((_n_proc.stdout or "") + (_n_proc.stderr or ""))[:400])
        _nm = _n_view.get("m") or {}
        _nrows = _nm.get("rows") or []
        check("V4.4 needs-you: one row per ticket, newest first, workflow "
              "authority first - BLOCKED, READY, then the verdict halt",
              _n_view.get("has") is True
              and [r.get("ticket_id") for r in _nrows]
              == ["T-B", "T-A", "T-C"]
              and [r.get("kind") for r in _nrows]
              == ["blocked", "ready", "halt"])
        check("V4.4 needs-you: the BLOCKED row carries the recorded "
              "transition reason and its workflow id",
              _nrows and _nrows[0].get("workflow_id") == "wf-B-1"
              and "qa repair exhausted" in str(_nrows[0].get("headline")))
        check("V4.4 needs-you: the READY row names its workflow and asks "
              "for Ship",
              len(_nrows) > 1 and _nrows[1].get("workflow_id") == "wf-A-2"
              and any("Ship" in str(a.get("label"))
                      for a in (_nrows[1].get("actions") or [])))
        check("V4.4 needs-you: an older BLOCKED workflow is excluded and "
              "COUNTED as superseded - never silently dropped",
              _nm.get("superseded_blocked") == 1
              and all(r.get("workflow_id") != "wf-A-1" for r in _nrows))
        check("V4.4 needs-you: a comprehension halt without retained "
              "question evidence SAYS so",
              len(_nrows) > 2
              and (_nrows[2].get("questions") or {}).get("present") is False
              and "not retained"
              in str((_nrows[2].get("questions") or {}).get("note")))
        _mq_rows = (_n_view.get("mq") or {}).get("rows") or []
        check("V4.4 needs-you: retained question evidence flips the claim",
              len(_mq_rows) > 2
              and (_mq_rows[2].get("questions") or {}).get("present")
              is True)
        _mn = _n_view.get("mn") or {}
        check("V4.4 needs-you: without workflow tables the verdict halts "
              "still surface and the basis admits the limit",
              [r.get("ticket_id") for r in (_mn.get("rows") or [])]
              == ["T-B", "T-C"]
              and "not recorded" in str(_mn.get("basis")))

    # ----------------------------------------------------------------------
    # V4.4: RUNS - the all-attempts lens and its filters. Search reaches
    # ticket id, run id AND workflow id; the workflow-state / stopped-stage
    # / started-month filters exist with option lists DERIVED from the data;
    # a ticket matches when ANY of its attempts does.
    check("V4.4 runs: the toolbar host ships in the bundle",
          'class="runs-toolbar"' in html)
    check("V4.4 runs: the all-attempts host ships in the bundle",
          'class="runs-attempts"' in html)
    check("V4.4 runs: app.js exports the runsFilterModel seam and the "
          "openAttempt action",
          "runsFilterModel" in _app_js and "openAttempt" in _app_js)
    if _node is not None:
        _r_probe = os.path.join(tmp, "runsfilter_probe.js")
        _RFIX = {
            "tickets": [
                {"issue": "T-1", "run": "T-1-bbb", "runs": [
                    {"run": "T-1-aaa", "issue": "T-1",
                     "started": "2026-07-01 10:00:00", "outcome": "merged",
                     "stopped_at": None,
                     "verdict": {"workflow_id": "wf-1",
                                 "workflow_state": "READY",
                                 "state": "delivered"}},
                    {"run": "T-1-bbb", "issue": "T-1",
                     "started": "2026-08-01 10:00:00", "outcome": "failed",
                     "stopped_at": "blind_review",
                     "verdict": {"workflow_id": "wf-2",
                                 "workflow_state": "BLOCKED",
                                 "state": "blocked"}},
                ]},
                {"issue": "T-2", "run": "T-2-ccc", "runs": [
                    {"run": "T-2-ccc", "issue": "T-2",
                     "started": "2026-08-05 09:00:00",
                     "outcome": "running", "stopped_at": None,
                     "verdict": {"workflow_id": "wf-3",
                                 "workflow_state": "IMPLEMENTING",
                                 "state": "running"}},
                ]},
            ],
        }
        with open(_r_probe, "w", encoding="ascii") as fh:
            fh.write(
                '"use strict";\n'
                + _PROBE_JS.split("require(APP);")[0]
                + 'require(APP);\n'
                  'var D = global.window.DocketDashboard || {};\n'
                  'var P = JSON.parse(process.argv[3]);\n'
                  'function ids(m) {\n'
                  '  return (m.attempts || []).map(function (r) {\n'
                  '    return r.run; });\n'
                  '}\n'
                  'var out = { has: typeof D.runsFilterModel === '
                  '"function" };\n'
                  'if (out.has) {\n'
                  '  out.all = D.runsFilterModel(P, {});\n'
                  '  out.byRun = ids(D.runsFilterModel(P, '
                  '{ q: "T-1-bbb" }));\n'
                  '  out.byWfId = ids(D.runsFilterModel(P, '
                  '{ q: "wf-3" }));\n'
                  '  out.byWfState = ids(D.runsFilterModel(P, '
                  '{ wf: "BLOCKED" }));\n'
                  '  out.byStage = ids(D.runsFilterModel(P, '
                  '{ stage: "blind_review" }));\n'
                  '  out.byDate = ids(D.runsFilterModel(P, '
                  '{ date: "2026-08" }));\n'
                  '  out.tSearch = D.runsFilterModel(P, '
                  '{ q: "T-1-bbb" }).tickets;\n'
                  '}\n'
                  'console.log(JSON.stringify(out));\n')
        _r_proc = subprocess.run(
            [_node, _r_probe, os.path.join(BUNDLE, "app.js"),
             json.dumps(_RFIX)],
            capture_output=True, text=True, timeout=120)
        try:
            _r_view = json.loads(
                (_r_proc.stdout or "").strip().splitlines()[-1])
        except Exception:
            _r_view = {}
            print("  runs-filter probe produced no JSON:",
                  ((_r_proc.stdout or "") + (_r_proc.stderr or ""))[:400])
        _ra = _r_view.get("all") or {}
        check("V4.4 runs: unfiltered, every attempt is listed newest "
              "first and every ticket is in",
              _r_view.get("has") is True
              and [r.get("run") for r in _ra.get("attempts") or []]
              == ["T-2-ccc", "T-1-bbb", "T-1-aaa"]
              and sorted(_ra.get("tickets") or []) == ["T-1", "T-2"])
        check("V4.4 runs: search reaches run ids and workflow ids",
              _r_view.get("byRun") == ["T-1-bbb"]
              and _r_view.get("byWfId") == ["T-2-ccc"])
        check("V4.4 runs: the workflow-state, stopped-stage and "
              "started-month filters each select by their own axis",
              _r_view.get("byWfState") == ["T-1-bbb"]
              and _r_view.get("byStage") == ["T-1-bbb"]
              and _r_view.get("byDate") == ["T-2-ccc", "T-1-bbb"])
        check("V4.4 runs: option lists are DERIVED from the data, never "
              "hardcoded",
              (_ra.get("options") or {}).get("wf")
              == ["BLOCKED", "IMPLEMENTING", "READY"]
              and (_ra.get("options") or {}).get("stage")
              == ["blind_review"]
              and (_ra.get("options") or {}).get("date")
              == ["2026-07", "2026-08"])
        check("V4.4 runs: a ticket matches when ANY of its attempts does",
              _r_view.get("tSearch") == ["T-1"])

    # ----------------------------------------------------------------------
    # V4.4: GATES - the full-column table and its honesty notes. Every
    # recorded state gets its own cell (Failed, Halted, Unknown, Skipped,
    # Never reached beside Caught), and the two rules a reader needs to
    # trust the numbers - latest-durable-row-wins and measured-zero-vs-
    # dash - are stated on the tab itself, not left to documentation.
    check("V4.4 gates: the table header carries every recorded state - "
          "Failed, Halted, Unknown, Skipped and Never reached beside "
          "Caught",
          all((">" + w + "</th>") in html for w in
              ("Failed", "Caught", "Halted", "Unknown", "Skipped",
               "Never reached")))
    check("V4.4 gates: the latest-durable-row-wins rule is stated with "
          "the table",
          "latest durable row" in html)
    check("V4.4 gates: the measured-zero-vs-dash rule is stated with "
          "the table",
          "measured zero" in html.lower())

    # ----------------------------------------------------------------------
    # V4.4: USAGE & COST workbench. The bundle ships the coverage-bar,
    # token-flow, breakdown and per-call hosts; app.js exports the pure
    # callExplorerModel seam whose six filter axes, search and option
    # lists are exercised here on a synthetic payload.
    check("V4.4 cost: the coverage-bar, token-flow, breakdowns and "
          "per-call hosts ship in the bundle",
          'id="acct-coverage"' in html and 'id="tokflow"' in html
          and 'class="u-breaks"' in html and 'class="percall-bar"' in html
          and 'class="percall-host"' in html)
    check("V4.4 cost: app.js exports the callExplorerModel seam",
          "callExplorerModel" in _app_js)
    if _node is not None:
        _c_probe = os.path.join(tmp, "callexplorer_probe.js")
        _CFIX = {
            "accounting": {
                "calls": 9, "per_call_truncated": 6,
                "per_call": [
                    {"actor": "developer", "model": "m1", "run": "T-1-aaa",
                     "issue": "T-1", "at": "2026-08-01 10:00:00",
                     "tokens_in": 100, "tokens_out": 10,
                     "tokens_cached": None, "cost_usd": None,
                     "failed": False},
                    {"actor": "qa", "model": "m2", "run": "T-1-bbb",
                     "issue": "T-1", "at": "2026-08-02 10:00:00",
                     "tokens_in": 200, "tokens_out": 20,
                     "tokens_cached": 50, "cost_usd": 0.5, "failed": True},
                    {"actor": "planner", "model": "m1", "run": "T-2-ccc",
                     "issue": "T-2", "at": "2026-08-03 10:00:00",
                     "tokens_in": 300, "tokens_out": 30,
                     "tokens_cached": 0, "cost_usd": 0.1, "failed": False},
                ],
            },
            "agents": [
                {"role": "developer", "stage": "unit_tests"},
                {"role": "qa", "stage": "qa_e2e"},
                {"role": "planner", "stage": "plan"},
            ],
        }
        with open(_c_probe, "w", encoding="ascii") as fh:
            fh.write(
                '"use strict";\n'
                + _PROBE_JS.split("require(APP);")[0]
                + 'require(APP);\n'
                  'var D = global.window.DocketDashboard || {};\n'
                  'var P = JSON.parse(process.argv[3]);\n'
                  'function ids(m) {\n'
                  '  return (m.rows || []).map(function (c) {\n'
                  '    return c.run; });\n'
                  '}\n'
                  'var out = { has: typeof D.callExplorerModel === '
                  '"function" };\n'
                  'if (out.has) {\n'
                  '  var all = D.callExplorerModel(P, {});\n'
                  '  out.all = { runs: ids(all), retained: all.retained,\n'
                  '    total: all.total, truncated: all.truncated,\n'
                  '    options: all.options };\n'
                  '  out.byActor = ids(D.callExplorerModel(P, '
                  '{ actor: "developer" }));\n'
                  '  out.byStage = ids(D.callExplorerModel(P, '
                  '{ stage: "qa" }));\n'
                  '  out.byModel = ids(D.callExplorerModel(P, '
                  '{ model: "m1" }));\n'
                  '  out.byOk = ids(D.callExplorerModel(P, '
                  '{ ok: "failed" }));\n'
                  '  out.byUnpriced = ids(D.callExplorerModel(P, '
                  '{ priced: "unpriced" }));\n'
                  '  out.byCacheRep = ids(D.callExplorerModel(P, '
                  '{ cache: "reported" }));\n'
                  '  out.byCacheUn = ids(D.callExplorerModel(P, '
                  '{ cache: "unavailable" }));\n'
                  '  out.byQ = ids(D.callExplorerModel(P, '
                  '{ q: "t-2" }));\n'
                  '  out.bySelTicket = ids(D.callExplorerModel(P, '
                  '{ sel: { dim: "ticket", val: "T-1" } }));\n'
                  '  out.bySelStage = ids(D.callExplorerModel(P, '
                  '{ sel: { dim: "stage", val: "develop" } }));\n'
                  '}\n'
                  'console.log(JSON.stringify(out));\n')
        _c_proc = subprocess.run(
            [_node, _c_probe, os.path.join(BUNDLE, "app.js"),
             json.dumps(_CFIX)],
            capture_output=True, text=True, timeout=120)
        try:
            _c_view = json.loads(
                (_c_proc.stdout or "").strip().splitlines()[-1])
        except Exception:
            _c_view = {}
            print("  call-explorer probe produced no JSON:",
                  ((_c_proc.stdout or "") + (_c_proc.stderr or ""))[:400])
        _ca = _c_view.get("all") or {}
        check("V4.4 cost: unfiltered, every retained call is in and the "
              "population states retained, total and truncated honestly",
              _c_view.get("has") is True
              and sorted(_ca.get("runs") or [])
              == ["T-1-aaa", "T-1-bbb", "T-2-ccc"]
              and _ca.get("retained") == 3 and _ca.get("total") == 9
              and _ca.get("truncated") == 6)
        check("V4.4 cost: actor, stage, model and outcome filters each "
              "select by their own axis - stage is the actor's PIPELINE "
              "attribution",
              _c_view.get("byActor") == ["T-1-aaa"]
              and _c_view.get("byStage") == ["T-1-bbb"]
              and sorted(_c_view.get("byModel") or [])
              == ["T-1-aaa", "T-2-ccc"]
              and _c_view.get("byOk") == ["T-1-bbb"])
        check("V4.4 cost: priced and cache filters split on RECORDED vs "
              "absent - a reported zero cache split is reported, never "
              "unavailable",
              _c_view.get("byUnpriced") == ["T-1-aaa"]
              and sorted(_c_view.get("byCacheRep") or [])
              == ["T-1-bbb", "T-2-ccc"]
              and _c_view.get("byCacheUn") == ["T-1-aaa"])
        check("V4.4 cost: search reaches run and ticket ids, and a "
              "breakdown selection filters by its own dimension",
              _c_view.get("byQ") == ["T-2-ccc"]
              and sorted(_c_view.get("bySelTicket") or [])
              == ["T-1-aaa", "T-1-bbb"]
              and _c_view.get("bySelStage") == ["T-1-aaa"])
        check("V4.4 cost: filter option lists are DERIVED from the data, "
              "never hardcoded",
              (_ca.get("options") or {}).get("actor")
              == ["developer", "planner", "qa"]
              and (_ca.get("options") or {}).get("model") == ["m1", "m2"]
              and (_ca.get("options") or {}).get("stage")
              == ["develop", "plan", "qa"])

    # ----------------------------------------------------------------------
    # V4.4: pipeline economics + Prompts full table. stageEconModel folds
    # the actor aggregates onto the nine pipeline stages (context actors
    # onto Blast Radius, roster-less actors onto an unattributed row);
    # promptsModel filters the full-column prompt table by agent, stage
    # and model. Both are pure exported seams.
    check("V4.4 econ: app.js exports the stageEconModel seam",
          "stageEconModel" in _app_js)
    check("V4.4 prompts: the full-column header ships - Base, Delta, "
          "Tickets touched and Merge rate beside Calls",
          all((">" + w + "</th>") in html for w in
              ("Base", "Delta", "Tickets touched", "Merge rate"))
          and ">Runs</th>" not in html)
    check("V4.4 prompts: the ticket-keyed basis of the touched and "
          "merged counts is stated on the tab",
          "ticket-keyed" in html)
    check("V4.4 prompts: app.js exports the promptsModel seam",
          "promptsModel" in _app_js)
    if _node is not None:
        _e_probe = os.path.join(tmp, "stageecon_probe.js")
        _EFIX = {
            "agents": [
                {"role": "developer", "stage": "unit_tests", "calls": 2,
                 "failed_calls": 1, "tokens_in": 100, "tokens_out": 10,
                 "duration_ms": 5000, "cost_usd": 0.5},
                {"role": "lead", "stage": "context", "calls": 1,
                 "failed_calls": 0, "tokens_in": 50, "tokens_out": 5,
                 "duration_ms": None, "cost_usd": None},
                {"role": "mystery", "stage": None, "calls": None,
                 "failed_calls": None, "tokens_in": 7, "tokens_out": 1,
                 "duration_ms": None, "cost_usd": None},
            ],
            "prompt_versions": [
                {"version": "developer@3", "base": "developer@3",
                 "delta": None, "agent": "developer",
                 "stage": "unit_tests", "stages": ["unit_tests"],
                 "models": ["m1"], "calls": 1, "runs": 1, "merged": 1,
                 "merge_rate": 1.0, "tokens_in": 20, "cost_usd": 0.42},
                {"version": "v3", "base": "v3", "delta": None,
                 "agent": None, "stage": None, "stages": [],
                 "models": ["m1", "m2"], "calls": 9, "runs": 2,
                 "merged": 1, "merge_rate": 0.5, "tokens_in": 90,
                 "cost_usd": None},
            ],
        }
        with open(_e_probe, "w", encoding="ascii") as fh:
            fh.write(
                '"use strict";\n'
                + _PROBE_JS.split("require(APP);")[0]
                + 'require(APP);\n'
                  'var D = global.window.DocketDashboard || {};\n'
                  'var P = JSON.parse(process.argv[3]);\n'
                  'var out = { hasE: typeof D.stageEconModel === '
                  '"function", hasP: typeof D.promptsModel === '
                  '"function" };\n'
                  'if (out.hasE) {\n'
                  '  var e = D.stageEconModel(P);\n'
                  '  out.stages = (e.stages || []).length;\n'
                  '  function by(id) {\n'
                  '    return (e.stages || []).filter(function (s) {\n'
                  '      return s.id === id; })[0] || {};\n'
                  '  }\n'
                  '  out.dev = by("develop");\n'
                  '  out.blast = by("blast_radius");\n'
                  '  out.unatt = e.unattributed || {};\n'
                  '}\n'
                  'if (out.hasP) {\n'
                  '  var all = D.promptsModel(P, {});\n'
                  '  out.pAll = (all.rows || []).map(function (r) {\n'
                  '    return r.version; });\n'
                  '  out.pOpts = all.options;\n'
                  '  out.pByAgent = (D.promptsModel(P, '
                  '{ agent: "developer" }).rows || []).map(function (r) {'
                  ' return r.version; });\n'
                  '  out.pByModel = (D.promptsModel(P, '
                  '{ model: "m2" }).rows || []).map(function (r) {'
                  ' return r.version; });\n'
                  '}\n'
                  'console.log(JSON.stringify(out));\n')
        _e_proc = subprocess.run(
            [_node, _e_probe, os.path.join(BUNDLE, "app.js"),
             json.dumps(_EFIX)],
            capture_output=True, text=True, timeout=120)
        try:
            _e_view = json.loads(
                (_e_proc.stdout or "").strip().splitlines()[-1])
        except Exception:
            _e_view = {}
            print("  stage-econ probe produced no JSON:",
                  ((_e_proc.stdout or "") + (_e_proc.stderr or ""))[:400])
        _ed = _e_view.get("dev") or {}
        _eb = _e_view.get("blast") or {}
        _eu = _e_view.get("unatt") or {}
        check("V4.4 econ: every pipeline stage gets a row and the "
              "developer's aggregates land on Develop",
              _e_view.get("hasE") is True and _e_view.get("stages") == 9
              and _ed.get("tokens_in") == 100 and _ed.get("events") == 2
              and _ed.get("failed") == 1
              and "developer" in (_ed.get("actors") or []))
        check("V4.4 econ: a context-mapping actor folds onto Blast "
              "Radius and a roster-less actor lands on the unattributed "
              "row - nothing goes uncounted",
              _eb.get("tokens_in") == 50
              and "lead" in (_eb.get("actors") or [])
              and _eu.get("tokens_in") == 7
              and "mystery" in (_eu.get("actors") or []))
        check("V4.4 econ: a stage whose actors never counted events "
              "reports events as UNKNOWN, not zero",
              _eu.get("events") is None and _ed.get("events") == 2)
        check("V4.4 prompts: promptsModel filters by its axes with "
              "DERIVED options",
              sorted(_e_view.get("pAll") or [])
              == ["developer@3", "v3"]
              and _e_view.get("pByAgent") == ["developer@3"]
              and _e_view.get("pByModel") == ["v3"]
              and (_e_view.get("pOpts") or {}).get("agent")
              == ["developer"]
              and (_e_view.get("pOpts") or {}).get("model")
              == ["m1", "m2"])

    # ----------------------------------------------------------------------
    # V4.4: AGENTS roster completion. agentsModel is the pure seam: type
    # counts, the nine filters, search, and the sorts, over every actor
    # the payload returns.
    check("V4.4 agents: the roster toolbar and summary hosts ship in "
          "the bundle",
          'class="agents-bar' in html and 'class="agents-stats' in html)
    check("V4.4 agents: app.js exports the agentsModel seam",
          "agentsModel" in _app_js)
    if _node is not None:
        _a_probe = os.path.join(tmp, "agents_probe.js")
        _AFIX = {
            "agents": [
                {"role": "a-model", "type": "model", "does": "writes",
                 "title": "A", "calls": 3, "failed_calls": 0,
                 "tokens_in": 50, "tokens_out": 5},
                {"role": "b-det", "type": "deterministic", "does": "runs",
                 "title": "B", "calls": None, "failed_calls": None,
                 "tokens_in": None, "tokens_out": None},
                {"role": "c-human", "type": "human", "does": "decides",
                 "title": "C", "calls": 0, "failed_calls": 0,
                 "tokens_in": 0, "tokens_out": 0},
                {"role": "d-ghost", "type": "unclassified", "does": None,
                 "title": None, "calls": 2, "failed_calls": 1,
                 "tokens_in": 99, "tokens_out": 9},
            ],
        }
        with open(_a_probe, "w", encoding="ascii") as fh:
            fh.write(
                '"use strict";\n'
                + _PROBE_JS.split("require(APP);")[0]
                + 'require(APP);\n'
                  'var D = global.window.DocketDashboard || {};\n'
                  'var P = JSON.parse(process.argv[3]);\n'
                  'function roles(m) {\n'
                  '  return (m.rows || []).map(function (a) {\n'
                  '    return a.role; });\n'
                  '}\n'
                  'var out = { has: typeof D.agentsModel === '
                  '"function" };\n'
                  'if (out.has) {\n'
                  '  var all = D.agentsModel(P, {});\n'
                  '  out.all = roles(all);\n'
                  '  out.counts = all.counts;\n'
                  '  out.det = roles(D.agentsModel(P, '
                  '{ filter: "det" }));\n'
                  '  out.active = roles(D.agentsModel(P, '
                  '{ filter: "active" }));\n'
                  '  out.unused = roles(D.agentsModel(P, '
                  '{ filter: "unused" }));\n'
                  '  out.unclassified = roles(D.agentsModel(P, '
                  '{ filter: "unclassified" }));\n'
                  '  out.byQ = roles(D.agentsModel(P, '
                  '{ q: "ghost" }));\n'
                  '  out.byTin = roles(D.agentsModel(P, '
                  '{ sort: "tin" }));\n'
                  '  out.byName = roles(D.agentsModel(P, '
                  '{ sort: "name" }));\n'
                  '}\n'
                  'console.log(JSON.stringify(out));\n')
        _a_proc = subprocess.run(
            [_node, _a_probe, os.path.join(BUNDLE, "app.js"),
             json.dumps(_AFIX)],
            capture_output=True, text=True, timeout=120)
        try:
            _a_view = json.loads(
                (_a_proc.stdout or "").strip().splitlines()[-1])
        except Exception:
            _a_view = {}
            print("  agents probe produced no JSON:",
                  ((_a_proc.stdout or "") + (_a_proc.stderr or ""))[:400])
        _ac = _a_view.get("counts") or {}
        check("V4.4 agents: the summary counts fold type and activity "
              "honestly - a configured actor with zero or uncounted "
              "activity is 'configured, unused', never dropped",
              _a_view.get("has") is True
              and len(_a_view.get("all") or []) == 4
              and _ac.get("total") == 4 and _ac.get("activity") == 2
              and _ac.get("zero") == 2 and _ac.get("unclassified") == 1
              and _ac.get("model") == 1 and _ac.get("det") == 1
              and _ac.get("human") == 1 and _ac.get("hybrid") == 0
              and _ac.get("system") == 0)
        check("V4.4 agents: the type and activity filters each select "
              "by their own axis",
              _a_view.get("det") == ["b-det"]
              and sorted(_a_view.get("active") or [])
              == ["a-model", "d-ghost"]
              and sorted(_a_view.get("unused") or [])
              == ["b-det", "c-human"]
              and _a_view.get("unclassified") == ["d-ghost"])
        check("V4.4 agents: search reaches role and description, and "
              "the sorts order by their own key",
              _a_view.get("byQ") == ["d-ghost"]
              and (_a_view.get("byTin") or [None])[0] == "d-ghost"
              and _a_view.get("byName")
              == ["a-model", "b-det", "c-human", "d-ghost"])

    # ----------------------------------------------------------------------
    # V4.4: ARTIFACTS evidence browser. artifactsBrowserModel flattens
    # every attempt's artifact rows, filters by kind/ticket/search with
    # derived options, and states its population honestly: retained rows
    # against a total DERIVED from artifact_kinds, never a snapshot
    # literal.
    check("V4.4 artifacts: the browser bar and host ship in the bundle",
          'class="artbrowse-bar' in html and 'class="artbrowse-host' in html)
    check("V4.4 artifacts: app.js exports the artifactsBrowserModel seam",
          "artifactsBrowserModel" in _app_js)
    if _node is not None:
        _b_probe = os.path.join(tmp, "artbrowse_probe.js")
        _BFIX = {
            "artifact_kinds": [
                {"kind": "plan", "count": 5, "tickets": 2, "bytes": None},
                {"kind": "evidence", "count": 4, "tickets": 1,
                 "bytes": None},
            ],
            "tickets": [
                {"issue": "T-1", "runs": [
                    {"run": "T-1-aaa", "artifacts": [
                        {"issue": "T-1", "run": "T-1-aaa", "kind": "plan",
                         "rel_path": "plan/a.md", "actor": "planner",
                         "sha256": None, "bytes": 10,
                         "at": "2026-08-01 10:00:00",
                         "escapes_workspace": False},
                        {"issue": "T-1", "run": "T-1-aaa",
                         "kind": "evidence",
                         "rel_path": "evidence/log.txt", "actor": None,
                         "sha256": "ab" * 32, "bytes": None,
                         "at": "2026-08-01 11:00:00",
                         "escapes_workspace": False},
                    ]},
                ]},
                {"issue": "T-2", "runs": [
                    {"run": "T-2-bbb", "artifacts": [
                        {"issue": "T-2", "run": "T-2-bbb", "kind": "plan",
                         "rel_path": "../../etc/passwd", "actor": "dev",
                         "sha256": None, "bytes": None,
                         "at": "2026-08-02 10:00:00",
                         "escapes_workspace": True},
                    ]},
                ]},
            ],
        }
        with open(_b_probe, "w", encoding="ascii") as fh:
            fh.write(
                '"use strict";\n'
                + _PROBE_JS.split("require(APP);")[0]
                + 'require(APP);\n'
                  'var D = global.window.DocketDashboard || {};\n'
                  'var P = JSON.parse(process.argv[3]);\n'
                  'function paths(m) {\n'
                  '  return (m.rows || []).map(function (a) {\n'
                  '    return a.rel_path; });\n'
                  '}\n'
                  'var out = { has: typeof D.artifactsBrowserModel === '
                  '"function" };\n'
                  'if (out.has) {\n'
                  '  var all = D.artifactsBrowserModel(P, {});\n'
                  '  out.all = paths(all);\n'
                  '  out.retained = all.retained;\n'
                  '  out.total = all.total;\n'
                  '  out.options = all.options;\n'
                  '  out.byKind = paths(D.artifactsBrowserModel(P, '
                  '{ kind: "evidence" }));\n'
                  '  out.byTicket = paths(D.artifactsBrowserModel(P, '
                  '{ ticket: "T-2" }));\n'
                  '  out.byQ = paths(D.artifactsBrowserModel(P, '
                  '{ q: "t-1-aaa" }));\n'
                  '}\n'
                  'console.log(JSON.stringify(out));\n')
        _b_proc = subprocess.run(
            [_node, _b_probe, os.path.join(BUNDLE, "app.js"),
             json.dumps(_BFIX)],
            capture_output=True, text=True, timeout=120)
        try:
            _b_view = json.loads(
                (_b_proc.stdout or "").strip().splitlines()[-1])
        except Exception:
            _b_view = {}
            print("  artifacts probe produced no JSON:",
                  ((_b_proc.stdout or "") + (_b_proc.stderr or ""))[:400])
        check("V4.4 artifacts: every attempt's rows flatten in, and the "
              "population is retained-vs-DERIVED-total (artifact_kinds "
              "sum, larger than the retained sample)",
              _b_view.get("has") is True
              and len(_b_view.get("all") or []) == 3
              and _b_view.get("retained") == 3
              and _b_view.get("total") == 9)
        check("V4.4 artifacts: kind, ticket and search each filter by "
              "their own axis with DERIVED options",
              _b_view.get("byKind") == ["evidence/log.txt"]
              and _b_view.get("byTicket") == ["../../etc/passwd"]
              and _b_view.get("byQ")
              == ["plan/a.md", "evidence/log.txt"]
              and (_b_view.get("options") or {}).get("kind")
              == ["evidence", "plan"]
              and (_b_view.get("options") or {}).get("ticket")
              == ["T-1", "T-2"])

    # ----------------------------------------------------------------------
    # V4.4: LEDGER tab - measured database facts plus the three prose
    # truths a reader needs: the ledger is append-only, the dashboard is
    # NOT its only reader, and the inventory counts the WHOLE ledger
    # while the other tabs are project-scoped.
    check("V4.4 ledger: the database-facts host ships in the bundle",
          'id="db-facts"' in html)
    check("V4.4 ledger: the append-only, not-the-only-reader and "
          "whole-ledger-population statements ride with the tab",
          "append-only" in html
          and "not the only reader" in html.lower()
          and "whole ledger" in html.lower())

    # ----------------------------------------------------------------------
    # V4.4: REFERENCE corrections. The stale allow/ask/deny RBAC claim is
    # gone; the governor is described as the pipeline state machine plus
    # policy/budget knobs with blast_radius enforcing the boundary; the
    # state vocabularies and the compact pipeline figure render
    # client-side from TOPOLOGY (one authority, never a second copy);
    # CLI/config lines carry Copy controls with clipboard honesty.
    check("V4.4 reference: the stale allow/ask/deny governor claim is "
          "GONE and the state-machine + policy-knobs + blast-radius "
          "truth replaces it",
          "<b>allowed</b>" not in html
          and "state machine" in html.lower()
          and "blast" in html.lower()
          and "policy" in html.lower())
    check("V4.4 reference: the vocab and compact-topology hosts ship "
          "and app.js fills them from TOPOLOGY",
          'class="ref-vocab"' in html and 'class="ref-topology"' in html
          and "renderRefVocab" in _app_js
          and "renderRefTopology" in _app_js)
    check("V4.4 reference: CLI and config lines carry Copy controls and "
          "app.js wires the delegated data-copy handler",
          'data-copy="' in html and "data-copy" in _app_js
          and "copyText" in _app_js)

    # ----------------------------------------------------------------------
    # V4.4: CSS hygiene. --ground-2 was referenced in four places but
    # never defined, so those surfaces silently fell back to transparent
    # - a real visual defect. And the old network-chart's CSS must not
    # linger once the subway replaced it (mission rule: the old chart
    # must not linger).
    _css_path = os.path.join(BUNDLE, "app.css")
    with open(_css_path, encoding="utf-8") as _fh:
        _css = _fh.read()
    check("V4.4 css: --ground-2 is DEFINED where the palette lives, not "
          "just referenced through var()",
          re.search(r"^\s*--ground-2\s*:", _css, re.M) is not None)
    check("V4.4 css: the dead legacy network-chart block is gone - "
          "no .arch-svg, .arch-node, .arch-rbac or .rbac- selector "
          "survives in app.css",
          ".arch-svg" not in _css and ".arch-node" not in _css
          and ".arch-rbac" not in _css and ".rbac-" not in _css)

    print(f"report self-test: {passed}/{passed + failed}  ({os.path.getsize(out) // 1024}kb)")
    for s in skipped:
        print(f"  NOT RUN (environment, not a pass): {s}")
    return 0 if failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="ledger.db -> self-contained HTML")
    ap.add_argument("--db", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "ledger.db"))
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
    elif not os.path.exists(db) or os.path.getsize(db) == 0:
        print(f"no usable ledger at {db} (missing or empty). "
              "try --demo, or point --db at it.", file=sys.stderr)
        return 2

    out = build_report(db, a.out, a.release, a.project, a.max_events,
                       a.max_rows, tuple(a.exclude), a.hero)
    size = os.path.getsize(out)
    print(f"{out}  {size // 1024}kb  self-contained", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
