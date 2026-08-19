#!/usr/bin/env python3
"""
Docket - the clarification round-trip.

    escalate  ->  post the questions to Jira as a numbered comment
    author    ->  answers in a reply, attaches the sample copybook
    re-run    ->  read the answers back, feed them to the spec agent

Three decisions in here that are not obvious:

1. QUESTIONS GO TO JIRA, NOT AN OUTPUT CHANNEL.
   A question in a log is a question nobody answers. The ticket is where the
   author already is, where the notification goes, and where the answer belongs
   next to the thing it clarifies.

2. WE READ COMMENTS AFTER OUR MARKER, NOT ALL COMMENTS.
   Every question comment carries [docket:ask:<run_id>]. On re-run we take only
   what was said after the newest marker. Comments from six months ago about a
   different problem are not answers to today's questions, and feeding them to
   the spec agent as if they were is how a pipeline learns to be confidently
   wrong.

3. ANSWERS ARE MARKED AS AUTHOR-SUPPLIED, NOT MERGED INTO THE TICKET.
   The spec agent sees them under a CLARIFICATIONS heading, clearly separated
   from the ticket body. Provenance survives: six months from now, "who decided
   the connector was Spark-only?" is answerable.

Prerequisites - "is there a sample copybook?" - are not questions. Nobody
answers them; someone attaches a file. So attachments are downloaded to the
ticket workspace and reported as files, not as prose.
"""

from __future__ import annotations

import re
from pathlib import Path

MARKER = "docket:ask"
MARKER_RE = re.compile(r"\[docket:ask:([^\]]+)\]")


class ClarifyError(RuntimeError):
    """There is nothing to ask. Raised rather than posting an ask that
    accuses the ticket of being unclear while asking nothing."""


def numbered_questions(questions: list[str] | None) -> list[dict]:
    """
    The typed human-input shape: [{"id": "Q1", "text": ...}], 1-based.

    Three surfaces render the same blocking questions - the numbered Jira
    comment, the run's questions.json, and the human_input.required event a
    UI turns into a NEEDS INPUT card. They are renderings of ONE list, so the
    list is typed here and numbered here. "Answer Q2" then means the same
    thing to the author, to the ledger, and to the next round.

    A blank is not a question and never gets an id: an empty or whitespace
    string is what a harness failure looks like on its way to becoming a
    numbered ask, and the numbering must not shift around it either.
    """
    out: list[dict] = []
    for q in questions or []:
        text = str(q).strip()
        if text:
            out.append({"id": "Q{}".format(len(out) + 1), "text": text})
    return out


def build_question_comment(ticket_id: str, run_id: str, questions: list[str],
                           prerequisites: list[str] | None = None) -> str:
    """
    Jira Server wiki markup. Numbered so the author can answer per item.

    Deliberately short. A wall of text from a bot gets ignored, and an ignored
    gate is worse than no gate - it trains people that the pipeline cries wolf.

    Refuses to build an ask with no questions AND no files. A comment saying
    "Docket cannot start this yet - these are decisions no one has made"
    followed by an empty list is not an ambiguity report; it is a HARNESS
    failure (an unparseable spec reply, a dead model call, a gate that
    produced nothing) blaming the author for Docket's own breakage. An author
    blamed once for that stops answering, and the gate dies with them.
    """
    items = numbered_questions(questions)
    prereqs = [str(p).strip() for p in (prerequisites or []) if str(p).strip()]
    if not items and not prereqs:
        raise ClarifyError(
            "refusing to ask {} nothing: no answerable question and no file "
            "needed. A run that produced neither failed for a harness reason "
            "(unparseable reply, dead model call, gate produced nothing) - "
            "that is Docket's failure to fix, not the author's to "
            "answer.".format(ticket_id))

    lines = [
        f"*Docket cannot start {ticket_id} yet.*",
        "",
        "These are decisions no one has made. They cannot be found by reading the "
        "codebase, so work stops until they are answered.",
        "",
    ]
    for item in items:
        lines.append(f"# {item['text']}")

    if prereqs:
        lines += ["", "*Files needed* - attach to this ticket:"]
        for p in prereqs:
            lines.append(f"* {p}")

    lines += [
        "",
        "Reply in a comment below, numbered to match.",
        "",
        # The "why" is the whole value. A bare N/A is not a decision - it tells us
        # nothing, and the gate exists precisely so nobody has to guess. A reasoned
        # N/A tells us the QUESTION was wrong, which is worth more than the answer.
        "*If a question does not apply, reply* {{N/A}} *and say why in one line.* "
        "A bare {{N/A}} is not an answer - we will just ask again. Your reason tells "
        "us the question should never have been asked, and we will fix that "
        "permanently rather than ask you again next ticket.",
        "",
        "Re-run Docket once answered.",
        "",
        f"[{MARKER}:{run_id}]",
    ]
    return "\n".join(lines)


def find_first_ask(comments: list[dict]) -> dict | None:
    """
    The OLDEST comment carrying our marker. Everything after it is this
    conversation.
    """
    for c in comments or []:
        if MARKER_RE.search(c.get("body") or ""):
            return c
    return None


def find_last_ask(comments: list[dict]) -> dict | None:
    """The newest ask. Used to tell whether a round is still unanswered."""
    for c in reversed(comments or []):
        if MARKER_RE.search(c.get("body") or ""):
            return c
    return None


def unanswered_ask(comments: list[dict]) -> dict | None:
    """
    The newest ask, IF nobody has replied after it - else None.

    This is the re-post guard (B7). A re-run before the author answers
    regenerates the same blocking questions in new words; posting them again
    is the cry-wolf spam that kills the gate - an author who sees the bot ask
    twice stops answering. Any non-marker reply after the newest ask reopens
    posting: the new round may genuinely have new questions.
    """
    comments = comments or []
    last = find_last_ask(comments)
    if not last:
        return None
    idx = comments.index(last)
    for c in comments[idx + 1:]:
        if MARKER_RE.search(c.get("body") or ""):
            continue  # our own ask, not a reply
        if (c.get("body") or "").strip():
            return None  # the author replied - a new round may ask again
    return last


def answers_since_ask(comments: list[dict]) -> list[dict]:
    """
    Every answer since we FIRST asked, across all rounds.

    This used to take only comments after the NEWEST ask, and that was a real bug
    with a real cost. Round 1 asks "Spark-only or Polars?"; the author answers
    "Spark only". Round 2 asks something else and posts a new marker. Round 3 now
    reads only after that newer marker - so the Polars answer has fallen out of
    the window, and the agent asks it again, phrased differently because it is
    honestly re-deriving it from nothing.

    An author who answers a question and gets asked it again stops answering. That
    is the whole gate, dead.

    Comments from BEFORE our first ask are still excluded: chatter from six months
    ago about a different problem is not an answer to a question we had not asked
    yet, and feeding it in as one is how the pipeline starts believing things
    nobody said. But everything after the first ask is the conversation.
    """
    comments = comments or []
    first = find_first_ask(comments)
    if not first:
        return []
    idx = comments.index(first)
    out = []
    for c in comments[idx + 1:]:
        if MARKER_RE.search(c.get("body") or ""):
            continue  # our own asks, never our own answers
        body = (c.get("body") or "").strip()
        if body:
            out.append({
                "author": ((c.get("author") or {}).get("displayName")
                           or (c.get("author") or {}).get("name") or "unknown"),
                "created": c.get("created"),
                "body": body,
            })
    return out


def format_clarifications(answers: list[dict]) -> str:
    """
    What the spec agent sees. Attribution kept: these are statements by named
    humans, not facts from the ticket, and the difference matters when someone
    later asks who decided what.
    """
    if not answers:
        return ""
    parts = ["=== CLARIFICATIONS (answers from the ticket author, after Docket asked) ==="]
    for a in answers:
        parts.append(f"--- {a['author']} on {(a['created'] or '')[:10]} ---")
        parts.append(a["body"])
    return "\n".join(parts)


def new_attachments(attachments: list[dict], since_id: str | None,
                    known: set[str] | None = None) -> list[dict]:
    """Attachments we have not already pulled."""
    known = known or set()
    return [a for a in (attachments or []) if str(a.get("id")) not in known]


def download_all(client, attachments: list[dict], dest_dir: Path) -> list[dict]:
    """
    Pull attachments into the ticket workspace. A failure to fetch one file is
    reported, never fatal - a missing fixture is a prerequisite the gate will
    catch, not a reason to lose the whole run.
    """
    out = []
    for att in attachments or []:
        try:
            path = client.download_attachment(att, dest_dir)
            out.append({"id": str(att.get("id")), "filename": att.get("filename"),
                        "path": str(path), "bytes": att.get("size"), "ok": True})
        except Exception as e:
            out.append({"id": str(att.get("id")), "filename": att.get("filename"),
                        "error": str(e), "ok": False})
    return out


def _self_test() -> int:
    import sys
    import tempfile
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import transport

    ok = []
    # No scripted replies: any model call through this transport raises. It is
    # passed to nothing, because nothing in this module takes a transport -
    # the whole round-trip is string handling over Jira comments.
    tx = transport.MockTransport([])

    body = build_question_comment(
        "ONE-67", "ONE-67-abc123",
        ["Should the connector be Spark-only or Polars-compatible?",
         "What YAML keys does the test author supply for a mainframe source?"],
        prerequisites=["A sample copybook (.cpy) and matching EBCDIC data file"])
    ok.append(("comment is numbered for per-item replies", body.count("\n# ") == 2))
    ok.append(("comment carries a run-scoped marker", "[docket:ask:ONE-67-abc123]" in body))
    ok.append(("prerequisites asked as FILES, not questions",
               "attach to this ticket" in body and "sample copybook" in body))
    ok.append(("comment asks for N/A *with a reason*",
               "N/A" in body and "say why" in body))
    ok.append(("comment says a bare N/A will be re-asked", "we will just ask again" in body))
    ok.append(("comment stays short", len(body) < 1200))

    comments = [
        {"body": "old chatter about something else", "created": "2026-01-01T09:00:00.000+0000",
         "author": {"displayName": "Bob"}},
        {"body": body, "created": "2026-07-15T10:00:00.000+0000", "author": {"displayName": "docket"}},
        {"body": "1. Spark-only.\n2. source_type: mainframe, copybook: path",
         "created": "2026-07-15T11:00:00.000+0000", "author": {"displayName": "Jane PO"}},
    ]
    ans = answers_since_ask(comments)
    ok.append(("answers found after the marker", len(ans) == 1))
    ok.append(("pre-existing chatter is NOT read as an answer",
               not any("old chatter" in a["body"] for a in ans)))
    ok.append(("our own ask is not read back as an answer",
               not any(MARKER in a["body"] for a in ans)))
    ok.append(("attribution survives", ans[0]["author"] == "Jane PO"))

    ok.append(("no ask yet -> no answers", answers_since_ask([comments[0]]) == []))

    # THE regression, from a real run. Round 1 asked "Spark-only or Polars?" and
    # the author answered "Spark only". Round 3 then asked it AGAIN, phrased
    # differently - because we only read after the NEWEST marker, so round 1's
    # answer had fallen out of the window. An author who answers a question and
    # gets asked it again stops answering, and the gate is dead.
    body2 = build_question_comment("ONE-67", "ONE-67-def456", ["One more thing?"])
    comments2 = comments + [
        {"body": body2, "created": "2026-07-16T09:00:00.000+0000", "author": {"displayName": "docket"}},
        {"body": "Yes, do it that way.", "created": "2026-07-16T10:00:00.000+0000",
         "author": {"displayName": "Jane PO"}},
    ]
    ans2 = answers_since_ask(comments2)
    ok.append(("round 3 still sees round 1's answer", len(ans2) == 2))
    ok.append(("the FIRST answer survives later rounds - the actual bug",
               any("Spark-only" in a["body"] for a in ans2)))
    ok.append(("later answers are there too",
               any("Yes, do it" in a["body"] for a in ans2)))
    ok.append(("still excludes chatter from before we ever asked",
               not any("old chatter" in a["body"] for a in ans2)))
    ok.append(("our own asks never read back as answers",
               not any(MARKER in a["body"] for a in ans2)))

    body3 = build_question_comment("ONE-67", "ONE-67-ghi789", ["And another?"])
    comments3 = comments2 + [
        {"body": body3, "created": "2026-07-17T09:00:00.000+0000", "author": {"displayName": "docket"}},
        {"body": "Third round answer.", "created": "2026-07-17T10:00:00.000+0000",
         "author": {"displayName": "Jane PO"}},
    ]
    ans3 = answers_since_ask(comments3)
    ok.append(("three rounds: every answer survives", len(ans3) == 3))
    ok.append(("chronological order preserved for the agent",
               "Spark-only" in ans3[0]["body"] and "Third round" in ans3[2]["body"]))

    # B7: the re-post guard. An unanswered ask blocks re-posting; any reply
    # after the newest ask reopens it.
    ok.append(("unanswered ask found (no reply after it)",
               unanswered_ask(comments[:2]) is not None))
    ok.append(("answered ask -> None (a new round may post)",
               unanswered_ask(comments) is None))
    ok.append(("no ask at all -> None", unanswered_ask([comments[0]]) is None))
    ok.append(("empty comments -> None", unanswered_ask([]) is None))
    unans2 = comments2 + [
        {"body": build_question_comment("ONE-67", "ONE-67-r3", ["Round 3?"]),
         "created": "2026-07-17T09:00:00.000+0000", "author": {"displayName": "docket"}}]
    got = unanswered_ask(unans2)
    ok.append(("newest ask unanswered -> that ask returned, even after "
               "earlier answered rounds", got is not None and "ONE-67-r3" in got["body"]))
    ok.append(("a second ask right after an ask is not read as a reply",
               unanswered_ask([comments[1],
                               {"body": build_question_comment("X", "r2", ["q"])}])
               is not None))

    txt = format_clarifications(ans)
    ok.append(("clarifications are attributed, not anonymised", "Jane PO" in txt))
    ok.append(("clarifications are separated from the ticket body",
               txt.startswith("=== CLARIFICATIONS")))
    ok.append(("no answers -> empty, not a heading", format_clarifications([]) == ""))

    # Path traversal: a filename is a string from a human, not a safe path.
    class _FakeClient:
        def _request(self, m, p, h):
            return 200, "data"
    written = {}

    class _C(_FakeClient):
        def download_attachment(self, att, dest):
            import os
            name = os.path.basename(str(att.get("filename")))
            written["name"] = name
            return Path(dest) / name

    att_dir = Path(tempfile.mkdtemp(prefix="docket-clarify-"))
    res = download_all(_C(), [{"id": "1", "filename": "../../etc/passwd", "size": 4}],
                       att_dir)
    ok.append(("attachment path traversal defused", written.get("name") == "passwd"))
    ok.append(("download reports per-file, never fatal", res[0]["ok"] is True))

    class _Boom(_FakeClient):
        def download_attachment(self, att, dest):
            raise RuntimeError("network died")
    res_boom = download_all(_Boom(), [{"id": "2", "filename": "a.cpy"}], att_dir)
    ok.append(("a failed download is reported, not raised", res_boom[0]["ok"] is False))

    ok.append(("new attachments filtered by known ids",
               len(new_attachments([{"id": "1"}, {"id": "2"}], None, {"1"})) == 1))

    # ===== THE TYPED HUMAN-INPUT SHAPE =================================
    # A blocking question is not a log line. It is a typed item with a
    # stable id, so questions.json, the human_input.required event and the
    # numbered Jira comment are three renderings of ONE list and can never
    # drift apart or renumber between rounds.
    typed = numbered_questions(["Should the connector be Spark-only?",
                                "Which YAML keys does the author supply?"])
    ok.append(("a blocking question becomes a typed {id, text} item",
               typed == [{"id": "Q1", "text": "Should the connector be Spark-only?"},
                         {"id": "Q2", "text": "Which YAML keys does the author supply?"}]))
    ok.append(("ids are 1-based and contiguous - 'answer Q2' has to mean "
               "the same thing to the author and to the ledger",
               [t["id"] for t in typed] == ["Q1", "Q2"]))
    body_t = build_question_comment("ONE-67", "r1",
                                    [t["text"] for t in typed])
    ok.append(("the comment numbers exactly the typed items - one list, "
               "two renderings", body_t.count("\n# ") == len(typed)
               and all(t["text"] in body_t for t in typed)))
    ok.append(("a blank never gets an id, and the ones after it do not "
               "shift - a blank is not a question",
               numbered_questions(["a", "", "   ", "b"])
               == [{"id": "Q1", "text": "a"}, {"id": "Q2", "text": "b"}]))
    ok.append(("nothing to ask -> an empty typed list, not a phantom item",
               numbered_questions([]) == [] and numbered_questions(None) == []))

    # ===== A HARNESS FAILURE IS NOT A TICKET AMBIGUITY =================
    # When Docket itself breaks - the spec reply would not parse, the model
    # errored, the gate produced nothing - there is no question for anybody.
    # Posting "Docket cannot start ONE-67 yet. These are decisions no one has
    # made." with an empty numbered list blames the author for OUR failure,
    # and an author who is blamed once stops answering. The ask must refuse
    # to exist rather than say that.
    def _refused(fn):
        try:
            fn()
            return False, "not refused"
        except ClarifyError as e:
            return True, str(e)

    got, why = _refused(lambda: build_question_comment("ONE-67", "r2", []))
    ok.append(("an ask with no questions and no files is REFUSED - a "
               "generic Docket failure is not a ticket ambiguity", got))
    ok.append(("...and the refusal says whose failure it is",
               got and "harness" in why.lower() and "ONE-67" in why))
    got, _ = _refused(lambda: build_question_comment("ONE-67", "r2",
                                                     ["", "   "], []))
    ok.append(("blank strings are not questions either - an empty model "
               "reply cannot become a numbered ask", got))
    got, _ = _refused(lambda: build_question_comment("ONE-67", "r2", None))
    ok.append(("no question list at all is refused, not rendered", got))

    files_only = build_question_comment("ONE-67", "r3", [],
                                        prerequisites=["a sample copybook"])
    ok.append(("a files-only ask is still a real ask - nobody answers "
               "'is there a copybook?', they attach one",
               "sample copybook" in files_only
               and "[docket:ask:r3]" in files_only))

    ok.append(("a failed download travels as a typed record, never as a "
               "question the author must answer",
               set(res_boom[0]) == {"id", "filename", "error", "ok"}
               and "network died" in res_boom[0]["error"]))
    ok.append(("...and an ask built out of nothing but that failure is "
               "refused", _refused(lambda: build_question_comment(
                   "ONE-67", "r4",
                   [a.get("question", "") for a in res_boom]))[0]))

    # ===== TASK 20 / WORKSTREAM E SECTION 1 =============================
    # The comprehension gate decides; this module is the surface the
    # decision reaches a human through. Two bullets land here, and both
    # are only true if THIS module and loop.questions_from agree - a
    # typed state the gate derives and the ask refuses to render is a
    # gate that stops the run and says nothing.
    import loop as _t20_loop

    _t20_missing = _t20_loop.questions_from(
        {"intent": "make it better", "acceptance_criteria": [],
         "blocking_questions": [], "contradictions": []})
    _t20_typed_missing = numbered_questions(_t20_missing)
    ok.append(("T20-E1-b: MISSING acceptance criteria reach this surface "
               "as a real, typed, postable question - the ask the gate "
               "derives is one a human can actually answer",
               len(_t20_typed_missing) == 1
               and "acceptance criteria"
               in _t20_typed_missing[0]["text"].lower()
               and "acceptance criteria" in build_question_comment(
                   "ONE-99", "r-missing", _t20_missing).lower()))

    _t20_all = _t20_loop.questions_from(
        {"acceptance_criteria": [{"text": "it is quick",
                                  "testable": False,
                                  "why_not": "no measurable outcome"}],
         "blocking_questions": ["Which label modes exist?"],
         "contradictions": ["the summary and AC1 disagree on the default"]})
    _t20_typed_all = numbered_questions(_t20_all)
    _t20_body_all = build_question_comment("ONE-99", "r-all", _t20_all)
    ok.append(("T20-E1-b: all three ambiguity kinds - a blocking "
               "question, a contradiction and a criterion that cannot be "
               "tested - arrive as ONE typed list with contiguous ids, "
               "not three different shapes",
               [q["id"] for q in _t20_typed_all] == ["Q1", "Q2", "Q3"]
               and any(q["text"] == "Which label modes exist?"
                       for q in _t20_typed_all)
               and any(q["text"].startswith("Contradiction:")
                       for q in _t20_typed_all)
               and any(q["text"].startswith("Not testable:")
                       for q in _t20_typed_all)))
    ok.append(("T20-E1-b: ...and the numbered comment renders exactly "
               "those items, in that order - the Jira ask, questions.json "
               "and the NEEDS INPUT card are three renderings of one "
               "list, so 'answer Q2' means the same thing to everyone",
               _t20_body_all.count("\n# ") == len(_t20_typed_all)
               and [ln[2:] for ln in _t20_body_all.splitlines()
                    if ln.startswith("# ")]
               == [q["text"] for q in _t20_typed_all]))

    ok.append(("T20-E1-d: a generic Docket failure is never displayed as "
               "a ticket ambiguity - a gate that produced no question and "
               "no missing file cannot build an ask at all",
               _refused(lambda: build_question_comment("ONE-99", "r-none",
                                                       []))[0]
               and _refused(lambda: build_question_comment(
                   "ONE-99", "r-none", ["", "   "], []))[0]))
    ok.append(("T20-E1-d: ...and the refusal says whose failure it is, "
               "so the author is never blamed for an unparseable reply, "
               "a dead model call or a gate that produced nothing",
               "harness" in _refused(lambda: build_question_comment(
                   "ONE-99", "r-none", []))[1].lower()))

    ok.append(("no model was called: the clarification round-trip is "
               "string handling, not inference", tx.calls == []))

    import shutil
    shutil.rmtree(att_dir, ignore_errors=True)
    ok.append(("the temporary attachment dir is under $TMPDIR and removed",
               str(att_dir).startswith(tempfile.gettempdir())
               and not att_dir.exists()))

    w = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name.ljust(w)}")
    failed = [n for n, p in ok if not p]
    print(f"\n  {len(ok) - len(failed)}/{len(ok)} passed" + (f"  FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser(
        description="Docket clarification round-trip: blocking questions out "
                    "to Jira, answers back. A library module - its only "
                    "command is its own checks, and no model is ever called.")
    ap.add_argument("--self-test", action="store_true",
                    help="run this module's checks (the default action)")
    ap.parse_args()
    sys.exit(_self_test())
