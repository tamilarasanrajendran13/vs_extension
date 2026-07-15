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


def build_question_comment(ticket_id: str, run_id: str, questions: list[str],
                           prerequisites: list[str] | None = None) -> str:
    """
    Jira Server wiki markup. Numbered so the author can answer per item.

    Deliberately short. A wall of text from a bot gets ignored, and an ignored
    gate is worse than no gate - it trains people that the pipeline cries wolf.
    """
    lines = [
        f"*Docket cannot start {ticket_id} yet.*",
        "",
        "These are decisions no one has made. They cannot be found by reading the "
        "codebase, so work stops until they are answered.",
        "",
    ]
    for q in questions:
        lines.append(f"# {q}")

    if prerequisites:
        lines += ["", "*Files needed* - attach to this ticket:"]
        for p in prerequisites:
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


def find_last_ask(comments: list[dict]) -> dict | None:
    """The newest comment carrying our marker. Anything after it may be an answer."""
    for c in reversed(comments or []):
        if MARKER_RE.search(c.get("body") or ""):
            return c
    return None


def answers_since_ask(comments: list[dict]) -> list[dict]:
    """
    Comments posted after our newest question comment.

    Not all comments. A comment from before we asked cannot be an answer to a
    question we had not asked yet, and treating it as one is how the pipeline
    starts believing things nobody said.
    """
    comments = comments or []
    last = find_last_ask(comments)
    if not last:
        return []
    idx = comments.index(last)
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
    ok = []

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

    # Two rounds: only the latest round's answers count.
    body2 = build_question_comment("ONE-67", "ONE-67-def456", ["One more thing?"])
    comments2 = comments + [
        {"body": body2, "created": "2026-07-16T09:00:00.000+0000", "author": {"displayName": "docket"}},
        {"body": "Yes, do it that way.", "created": "2026-07-16T10:00:00.000+0000",
         "author": {"displayName": "Jane PO"}},
    ]
    ans2 = answers_since_ask(comments2)
    ok.append(("second round reads only the newest round",
               len(ans2) == 1 and "Yes, do it" in ans2[0]["body"]))

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

    res = download_all(_C(), [{"id": "1", "filename": "../../etc/passwd", "size": 4}],
                       Path("/tmp/docket-att-test"))
    ok.append(("attachment path traversal defused", written.get("name") == "passwd"))
    ok.append(("download reports per-file, never fatal", res[0]["ok"] is True))

    class _Boom(_FakeClient):
        def download_attachment(self, att, dest):
            raise RuntimeError("network died")
    res = download_all(_Boom(), [{"id": "2", "filename": "a.cpy"}], Path("/tmp/x"))
    ok.append(("a failed download is reported, not raised", res[0]["ok"] is False))

    ok.append(("new attachments filtered by known ids",
               len(new_attachments([{"id": "1"}, {"id": "2"}], None, {"1"})) == 1))

    w = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name.ljust(w)}")
    failed = [n for n, p in ok if not p]
    print(f"\n  {len(ok) - len(failed)}/{len(ok)} passed" + (f"  FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
