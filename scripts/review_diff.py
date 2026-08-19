#!/usr/bin/env python3
"""
review_diff - point Docket's blind reviewer at code a HUMAN wrote.

Everything else that reaches reviewer.review_text_diff (the pipeline's
blind_review gate) goes through the checkpointer's pristine-vs-HEAD shadow
diff, built while loop.py runs a ticket end to end. This script has no
ticket, no checkpointer, no ledger - just a git repo and a diff you point it
at: your working tree against a ref, your staged changes, or your last
commit. Same reviewer, same evidence-verified findings, same three-state
outcome - run from a terminal, or from the "Docket: Review My Diff" command.

Transport: TWO, and which one you get is the whole point.

  --stdio   the VS Code path. Exactly the JSON-lines model bridge loop.py
            speaks: we write {"id","method":"chat"} to stdout, the extension
            answers over stdin using vscode.lm (GitHub Copilot models), and
            the finished review leaves as one {"method":"done"} notification.
            No `claude` binary, no provider credential, no PATH lookup - the
            CLI bridge is not even imported on this path.
  default   the terminal path. headless_gateway.ClaudeCli (see that module's
            docstring for why `claude -p` is the model access a headless
            machine actually has) - a stateless one-shot completion, same as
            every other headless run. Imported lazily, here only.

stdout is the WIRE under --stdio: nothing human-readable may be printed to it
(everything that would have been goes to stderr, or rides the done payload).

Security: security.scan() is a pure, deterministic function (no model, no
ledger) over changed files, scoped to the diff's own added lines exactly the
way the security_snyk gate scopes it (security.added_lines). That much is
cleanly separable and runs here. The full security_snyk gate ALSO triages
the scanner's findings through a model call and a fail-closed dismiss-with-
reason policy (security.run_security) - that half needs a ledger to record
the triage decision against, so it stays pipeline-only; this script reports
the raw scanner findings and nothing more.

Output: one JSON object on stdout -
    {"verdict", "outcome", "reason", "findings": [...], "summary",
     "security": {"scanned": [...], "findings": [...]}}
Exit 2 with a JSON {"error": "..."} on an empty diff or a git failure -
never a Python traceback for something this predictable.

Usage:
    python review_diff.py --repo ../data_project --against main
    python review_diff.py --repo ../data_project --staged
    python review_diff.py --repo ../data_project --last-commit --out review.md
    python review_diff.py --stdio --repo ../data_project --staged   <- VS Code
    python review_diff.py --self-test
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
for _p in (_here, _here.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import reviewer
import security
import transport as transport_mod

# headless_gateway (the `claude -p` bridge) is deliberately NOT imported here.
# It is imported inside _headless_transport() only, so the VS Code --stdio
# path never loads it - a module that is never imported cannot read
# DOCKET_HEADLESS_CLAUDE or look up a `claude` binary. The self-test pins
# that with a fresh-interpreter import probe.

DEFAULT_TICKET_ID = "review-diff"


# ---------------------------------------------------------------- git plumbing

def build_diff(repo, mode, ref=None, timeout=30):
    """mode in ('against', 'staged', 'last-commit'). Returns (diff_text,
    error) - exactly one of the two is set. An empty diff and a git failure
    are both reported here, honestly, rather than surfacing as a Python
    traceback or a silent empty review.
    """
    if mode == "against":
        cmd = ["git", "-C", str(repo), "diff", ref or "main"]
    elif mode == "staged":
        cmd = ["git", "-C", str(repo), "diff", "--cached"]
    elif mode == "last-commit":
        cmd = ["git", "-C", str(repo), "show", ref or "HEAD"]
    else:
        raise ValueError("unknown diff mode: {!r}".format(mode))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return None, "git failed: {}".format(e)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-300:]
        return None, "git failed: {}".format(tail or "exit {}".format(proc.returncode))
    diff = proc.stdout
    if not diff.strip():
        return None, "empty diff - nothing to review"
    return diff, None


def last_commit_message(repo, timeout=15):
    """The default ticket text: what the developer already said this change
    is about, in their own words. Best-effort - a repo with no commits yet
    (or a git that cannot answer) gets an empty string, never a crash."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%B"],
            capture_output=True, text=True, timeout=timeout)
        if proc.returncode == 0:
            return proc.stdout.strip()
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------- core

def build_output(res, security_result=None):
    """res is review_text_diff's return dict. Shapes the CLI's JSON
    contract - only the fields a caller (human or the VS Code extension)
    needs, none of the internal '_prompt'/'_reply'/'_agent' bookkeeping keys
    review_text_diff carries for a pipeline caller's benefit."""
    review = res.get("review") or {}
    findings = []
    for f in review.get("findings") or []:
        findings.append({
            "severity": f.get("severity"),
            "file": f.get("file"),
            "issue": f.get("issue"),
            "evidence": f.get("evidence"),
            "suggestion": f.get("suggestion"),
        })
    out = {
        "verdict": review.get("verdict"),
        "outcome": res.get("outcome"),
        "reason": res.get("reason"),
        "findings": findings,
        "summary": review.get("summary"),
    }
    if security_result is not None:
        out["security"] = security_result
    return out


def review_repo(repo, mode, ref, context, tx, cfg, workbench,
                out_path=None, run_security_scan=True,
                ticket_id=DEFAULT_TICKET_ID, say=None):
    """The whole flow, minus argv parsing - what main() calls, and what the
    self-test calls directly with a MockTransport so no real model is ever
    needed to prove this works.

    Transport-agnostic BY CONSTRUCTION: `tx` is whatever the caller built
    (StdioTransport over the VS Code gateway, ClaudeCli headless, or a
    MockTransport in a test). Nothing in here knows or can find out.

    `say` is the progress channel - the loop's own convention. Left None it
    is a no-op, which is what a terminal run wants; --stdio passes
    tx.progress so the extension's output channel narrates the review.

    Returns (output_dict, exit_code). exit_code is 2 (and output_dict is
    just {"error": ...}) on an empty diff or a git failure; 0 otherwise.
    """
    if say is None:
        def say(*_a, **_k):
            return None

    diff, err = build_diff(repo, mode, ref)
    if err:
        return {"error": err}, 2

    ticket_text = context if context else last_commit_message(repo)

    # Cap the diff sent to the model - same MAX_DIFF logic run_reviewer uses,
    # so an oversized working-tree diff gets a truncated-but-flagged review
    # instead of ClaudeCli's oversized-prompt preflight error. The security
    # scan below runs on the FULL diff regardless (it is deterministic, no
    # model, no prompt-size limit).
    model_diff = diff
    MAX_DIFF = 60_000
    try:
        import governor
        MAX_DIFF = governor.payload_budget(cfg or {}, "judge", MAX_DIFF)
    except Exception:
        pass
    if len(model_diff) > MAX_DIFF:
        model_diff = (model_diff[:MAX_DIFF] +
                     "\n... DIFF TRUNCATED at {} of {} chars - review what is "
                     "shown and flag the truncation in your concerns ..."
                     .format(MAX_DIFF, len(model_diff)))

    res = reviewer.review_text_diff(
        tx, cfg or {}, model_diff, ticket_text or "(no ticket text given)", [],
        workbench, say=say, ticket_id=ticket_id)

    security_result = None
    if run_security_scan:
        try:
            say("security scan (deterministic, added lines only)...")
            added = security.added_lines(diff)
            changed = list(added.keys())
            findings = security.scan(repo, changed, cfg or {}, added=added)
            security_result = {"scanned": changed, "findings": findings}
        except Exception as e:
            security_result = {"error": "security scan failed: {}".format(e)}

    out = build_output(res, security_result)

    if out_path:
        md = reviewer.render_review(
            res.get("review") or {}, res.get("outcome") or "unknown", ticket_id)
        Path(out_path).write_text(md, encoding="utf-8")

    return out, 0


# ---------------------------------------------------------------- transports

def _headless_transport(models_json=None, claude_bin=None):
    """The terminal path's model access: `claude -p` one-shots.

    The import lives INSIDE this function on purpose. It is the only thing
    in this script that can reach a `claude` binary or read
    DOCKET_HEADLESS_CLAUDE, and the VS Code path must be provably unable to
    do either - "never called" is an assertion about behaviour, "never
    imported" is an assertion about the module graph, and the second is the
    one that survives someone adding a helper later.
    """
    import headless_gateway
    return headless_gateway.ClaudeCli(
        headless_gateway.resolve_models(models_json), claude_bin=claude_bin)


def _stdio_transport():
    """The VS Code path's model access: the SAME JSON-lines gateway wire
    loop.py speaks, answered by extension/src/gateway.js through vscode.lm.
    No binary, no credential, no PATH lookup."""
    return transport_mod.build("stdio")


def emit_stdio_result(tx, out):
    """Hand the finished review to the gateway as loop.py's other --stdio
    entry points do: one {"method": "done"} notification carrying the whole
    payload, error included.

    The error rides IN the payload and the process still exits 0. That is
    not exit-code laundering, it is the protocol: gateway.runLoop() DISCARDS
    the done payload on a nonzero close and rejects with a bare
    "exited N", so an empty diff reported through the exit code reaches the
    user as a number with the reason thrown away. The terminal path keeps
    exit 2 for exactly the same condition, because there the exit code IS
    the channel.
    """
    if hasattr(tx, "_send"):
        tx._send({"method": "done", "params": out})


# ---------------------------------------------------------------- self-test

def _self_test():
    import io
    import tempfile

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    # ASCII cleanliness of this very file (standing repo rule).
    src = Path(__file__).read_text(encoding="utf-8")
    ok("review_diff.py is pure ASCII", all(ord(c) < 128 for c in src))

    from transport import MockTransport

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()

        def git(*a):
            subprocess.run(["git", "-C", str(repo)] + list(a),
                           check=True, capture_output=True)

        git("init", "-q")
        git("config", "user.email", "t@t.com")
        git("config", "user.name", "t")
        (repo / "a.py").write_text("def f():\n    return 0\n", encoding="utf-8")
        git("add", "a.py")
        git("commit", "-q", "-m", "base")
        base_sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True).stdout.strip()

        (repo / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        git("add", "a.py")
        git("commit", "-q", "-m", "change return to 1")

        # --- each diff-source mode builds the right diff ---

        diff_lc, err_lc = build_diff(str(repo), "last-commit")
        ok("last-commit: no error, shows the commit's own change",
           err_lc is None and "return 1" in diff_lc and "return 0" in diff_lc)

        diff_against, err_against = build_diff(str(repo), "against", base_sha)
        ok("against REF: working tree vs ref, same content when tree is clean",
           err_against is None and "return 1" in diff_against)

        diff_staged_empty, err_staged_empty = build_diff(str(repo), "staged")
        ok("staged: nothing staged yet -> empty-diff refusal",
           diff_staged_empty is None and "empty diff" in (err_staged_empty or ""))

        out_empty, code_empty = review_repo(
            str(repo), "staged", None, None, object(), {}, str(_here.parent))
        ok("review_repo refuses an empty diff before ever touching the "
           "transport (a bare object() would blow up on .chat)",
           code_empty == 2 and "empty diff" in out_empty.get("error", ""))

        # a NEW uncommitted, staged-only change
        (repo / "a.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        git("add", "a.py")

        diff_staged, err_staged = build_diff(str(repo), "staged")
        ok("staged: picks up exactly the staged change",
           err_staged is None and "return 2" in diff_staged)

        diff_against2, err_against2 = build_diff(str(repo), "against", base_sha)
        ok("against REF: now reflects the staged change too (working tree, "
           "not history)",
           err_against2 is None and "return 2" in diff_against2
           and "return 1" not in diff_against2)

        diff_lc2, err_lc2 = build_diff(str(repo), "last-commit")
        ok("last-commit: unaffected by uncommitted/staged changes",
           err_lc2 is None and "return 1" in diff_lc2 and "return 2" not in diff_lc2)

        # --- full JSON shape, evidence-verified findings, security block ---

        mt = MockTransport([json.dumps({
            "verdict": "request_changes", "summary": "found an issue",
            "findings": [{"severity": "major", "file": "a.py",
                         "issue": "returns the wrong value",
                         "evidence": "def f():\n-    return 1\n+    return 2",
                         "suggestion": "return 3 instead"}]})])
        out, code = review_repo(str(repo), "staged", None, "test ticket text",
                                mt, {}, str(_here.parent))
        ok("full run exits 0", code == 0)
        ok("output carries the required top-level keys",
           set(out) >= {"verdict", "outcome", "reason", "findings", "summary"})
        ok("verdict/outcome come from the SAME decide() the pipeline uses",
           out["verdict"] == "request_changes" and out["outcome"] == "fail")
        ok("finding evidence verified against the diff (a failed verification "
           "would have demoted it to 'concern' and flipped outcome to 'unknown')",
           len(out["findings"]) == 1
           and out["findings"][0]["severity"] == "major"
           and out["findings"][0]["evidence"])
        ok("security block present (deterministic scan, no findings on clean code)",
           out.get("security") == {"scanned": ["a.py"], "findings": []})

        # default ticket text = last commit message when --context is omitted
        mt2 = MockTransport([json.dumps({"verdict": "approve", "summary": "ok",
                                         "findings": []})])
        review_repo(str(repo), "last-commit", None, None, mt2, {},
                   str(_here.parent))
        ok("no --context -> falls back to the last commit message",
           "change return to 1" in mt2.calls[0]["user"])

        # --out writes the same markdown render_review() produces
        out_path = Path(td) / "peer-review.md"
        mt3 = MockTransport([json.dumps({"verdict": "approve", "summary": "clean",
                                         "findings": []})])
        review_repo(str(repo), "staged", None, "t", mt3, {},
                   str(_here.parent), out_path=str(out_path))
        ok("--out writes a peer-review markdown file",
           out_path.exists() and out_path.read_text().startswith("# Peer review"))

        # ------------------------------------------------------------------
        # Task 3: the VS Code path must never touch the claude CLI.
        #
        # docket.reviewMyDiff used to shell this script bare, and this script
        # reached the model through headless_gateway.ClaudeCli - so a VS Code
        # command needed a `claude` binary on PATH and a provider credential
        # behind it. The extension now spawns `review_diff.py --stdio` and
        # answers its chat requests over the SAME JSON-lines gateway wire
        # loop.py uses, i.e. vscode.lm / GitHub Copilot models.
        #
        # The booby trap below makes CONSTRUCTING the CLI bridge (or asking
        # it to resolve a role map) a hard failure, so "no claude binary is
        # consulted" is asserted, not assumed. Patching the attributes on the
        # real module catches a module-level import and a lazy in-function
        # import identically.
        # ------------------------------------------------------------------

        import headless_gateway as _hg

        class _CliBoobyTrap(Exception):
            pass

        def _trap(*_a, **_k):
            raise _CliBoobyTrap(
                "the VS Code path reached headless_gateway - docket."
                "reviewMyDiff must not need a claude binary")

        _real_cli, _real_resolve = _hg.ClaudeCli, _hg.resolve_models
        _old_stdin, _old_stdout = sys.stdin, sys.stdout

        wire_reply = json.dumps({
            "verdict": "request_changes", "summary": "wire review",
            "findings": [{"severity": "major", "file": "a.py",
                          "issue": "returns the wrong value",
                          "evidence": "def f():\n-    return 1\n+    return 2",
                          "suggestion": "return 3 instead"}]})
        wire_in = io.StringIO(
            json.dumps({"id": 1, "result": {"text": wire_reply}}) + "\n")
        wire_out = io.StringIO()
        wire_code, wire_err = None, None
        try:
            _hg.ClaudeCli, _hg.resolve_models = _trap, _trap
            sys.stdin, sys.stdout = wire_in, wire_out
            try:
                main(["--stdio", "--repo", str(repo), "--staged",
                      "--context", "test ticket text"])
            except SystemExit as e:
                wire_code = e.code
            except BaseException as e:      # booby trap, or anything else
                wire_err = e
        finally:
            sys.stdin, sys.stdout = _old_stdin, _old_stdout
            _hg.ClaudeCli, _hg.resolve_models = _real_cli, _real_resolve

        wire_msgs = []
        for _line in wire_out.getvalue().split("\n"):
            if not _line.strip():
                continue
            try:
                wire_msgs.append(json.loads(_line))
            except ValueError:
                wire_msgs.append({"_unparseable": _line})
        wire_chats = [m for m in wire_msgs if m.get("method") == "chat"]
        wire_dones = [m for m in wire_msgs if m.get("method") == "done"]
        wire_done = (wire_dones[0].get("params") or {}) if wire_dones else {}

        ok("--stdio drives the WHOLE review over the gateway wire (one chat "
           "request out, one done payload back) with the claude CLI bridge "
           "booby-trapped - the VS Code path needs no claude binary",
           wire_err is None and wire_code == 0
           and len(wire_chats) == 1 and len(wire_dones) == 1
           and wire_done.get("verdict") == "request_changes"
           and wire_done.get("outcome") == "fail"
           and len(wire_done.get("findings") or []) == 1
           and (wire_done.get("security") or {}).get("scanned") == ["a.py"])
        ok("--stdio keeps stdout the WIRE: every line is a protocol object, "
           "no human-readable JSON dump mixed in to corrupt it",
           bool(wire_msgs) and all("_unparseable" not in m for m in wire_msgs))

        # An empty diff over the wire: the reason travels IN the done payload
        # and the process still exits 0. A nonzero exit would reach the
        # extension through gateway.runLoop as a bare "exited N" with the
        # reason thrown away (runLoop discards `done` on a nonzero close).
        git("commit", "-q", "-m", "return 2")
        wire_in2 = io.StringIO("")
        wire_out2 = io.StringIO()
        wire_code2, wire_err2 = None, None
        try:
            _hg.ClaudeCli, _hg.resolve_models = _trap, _trap
            sys.stdin, sys.stdout = wire_in2, wire_out2
            try:
                main(["--stdio", "--repo", str(repo), "--staged"])
            except SystemExit as e:
                wire_code2 = e.code
            except BaseException as e:
                wire_err2 = e
        finally:
            sys.stdin, sys.stdout = _old_stdin, _old_stdout
            _hg.ClaudeCli, _hg.resolve_models = _real_cli, _real_resolve
        wire_dones2 = [json.loads(l) for l in wire_out2.getvalue().split("\n")
                       if l.strip() and '"done"' in l]
        ok("--stdio reports an empty diff IN the done payload and exits 0 - "
           "the extension gets the reason, not a bare exit code",
           wire_err2 is None and wire_code2 == 0 and len(wire_dones2) == 1
           and "empty diff" in ((wire_dones2[0].get("params") or {})
                                .get("error") or ""))

        # --stdio is the vscode.lm wire; the two headless-CLI knobs cannot
        # mean anything on it. Refused loudly rather than silently ignored.
        # The stderr assertion is what keeps this honest: before --stdio
        # existed, argparse rejected it as an "unrecognized argument", which
        # is exit 2 for entirely the wrong reason.
        _old_stderr = sys.stderr
        for _bad in (["--claude", "/nowhere/claude"], ["--models", "{}"]):
            _rej, _rej_err = None, io.StringIO()
            try:
                _hg.ClaudeCli, _hg.resolve_models = _trap, _trap
                sys.stdin, sys.stdout = io.StringIO(""), io.StringIO()
                sys.stderr = _rej_err
                try:
                    main(["--stdio", "--repo", str(repo), "--staged"] + _bad)
                except SystemExit as e:
                    _rej = e.code
                except BaseException:
                    _rej = "raised"
            finally:
                sys.stdin, sys.stdout = _old_stdin, _old_stdout
                sys.stderr = _old_stderr
                _hg.ClaudeCli, _hg.resolve_models = _real_cli, _real_resolve
            _rej_text = _rej_err.getvalue()
            ok("--stdio refuses {} by NAME instead of silently ignoring it"
               .format(_bad[0]),
               _rej == 2 and _bad[0] in _rej_text and "--stdio" in _rej_text
               and "unrecognized" not in _rej_text)

        # The brief's literal shape: the whole review driven by
        # transport.MockTransport, CLI bridge still booby-trapped. This pins
        # the CORE (review_repo) as transport-agnostic, independently of the
        # argv wiring the --stdio checks above cover.
        try:
            _hg.ClaudeCli, _hg.resolve_models = _trap, _trap
            mt4 = MockTransport([json.dumps(
                {"verdict": "approve", "summary": "ok", "findings": []})])
            out4, code4 = review_repo(str(repo), "last-commit", None, "t",
                                      mt4, {}, str(_here.parent))
        finally:
            _hg.ClaudeCli, _hg.resolve_models = _real_cli, _real_resolve
        ok("review_repo drives a full review through MockTransport with the "
           "claude CLI bridge booby-trapped",
           code4 == 0 and out4.get("verdict") == "approve"
           and len(mt4.calls) == 1)

    # Import-time proof, in a fresh interpreter: pulling in review_diff must
    # not drag in headless_gateway at all. A module that is never imported
    # cannot read DOCKET_HEADLESS_CLAUDE or look up a `claude` binary, so
    # this is the strongest form of "not consulted on the extension path".
    _probe = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, {sc!r}); sys.path.insert(0, {wb!r}); "
         "import review_diff; "
         "print('headless_gateway' in sys.modules)".format(
             sc=str(_here), wb=str(_here.parent))],
        capture_output=True, text=True)
    ok("importing review_diff does NOT import headless_gateway - the claude "
       "CLI bridge is loaded only by the terminal path that asks for it",
       _probe.returncode == 0 and _probe.stdout.strip() == "False")

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Review a git diff with Docket's blind reviewer - no "
                    "ticket, no ledger, just your working tree/staged "
                    "changes/last commit.")
    ap.add_argument("--repo", help="path to the git repo to review")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--against", metavar="REF",
                       help="review the working tree against this git ref")
    group.add_argument("--staged", action="store_true",
                       help="review staged changes only")
    group.add_argument("--last-commit", action="store_true",
                       help="review the last commit")
    ap.add_argument("--context", default=None,
                    help="ticket text for the reviewer; default is the "
                         "relevant commit's message")
    ap.add_argument("--out", default=None,
                    help="write the review as markdown to this path")
    ap.add_argument("--json", action="store_true",
                    help="findings JSON to stdout (this is the default "
                         "output regardless of this flag; kept for callers "
                         "that want to be explicit)")
    ap.add_argument("--models", default=None,
                    help='headless only: JSON role map override, e.g. '
                         '{"judge": "opus"}')
    ap.add_argument("--claude", default=None,
                    help="headless only: claude binary (default: claude on "
                         "PATH; env DOCKET_HEADLESS_CLAUDE also works)")
    ap.add_argument("--stdio", action="store_true",
                    help="VS Code spawned us: take the model over the "
                         "JSON-lines gateway wire (vscode.lm) instead of the "
                         "claude CLI, and return the review as a "
                         "{'method': 'done'} notification")
    ap.add_argument("--no-security", action="store_true",
                    help="skip the deterministic security scan pass")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        sys.exit(0 if _self_test() else 1)

    # --stdio is the vscode.lm wire. The two headless-CLI knobs cannot mean
    # anything on it, and silently ignoring a flag the caller passed is a
    # lie about what ran - refuse by name (argparse usage + exit 2).
    if args.stdio:
        for flag, value in (("--claude", args.claude), ("--models", args.models)):
            if value is not None:
                ap.error(
                    "{} configures the headless claude CLI and has no meaning "
                    "with --stdio, where the model comes from VS Code "
                    "(vscode.lm). Drop one of the two.".format(flag))

    if not args.repo:
        print(json.dumps({"error": "--repo is required"}))
        sys.exit(2)
    if not (args.against or args.staged or args.last_commit):
        print(json.dumps({
            "error": "one of --against REF, --staged, --last-commit is required"}))
        sys.exit(2)

    mode = "against" if args.against else ("staged" if args.staged else "last-commit")
    ref = args.against

    workbench = _here.parent
    cfg = {}
    try:
        cfg = json.loads((workbench / "config.json").read_text(encoding="utf-8"))
    except Exception:
        cfg = {}

    tx = _stdio_transport() if args.stdio else _headless_transport(
        args.models, args.claude)

    out, code = review_repo(
        args.repo, mode, ref, args.context, tx, cfg, str(workbench),
        out_path=args.out, run_security_scan=not args.no_security,
        say=(tx.progress if args.stdio else None))

    if args.stdio:
        # stdout is the WIRE here - the review leaves as a done notification,
        # never as a printed JSON blob (that would corrupt the protocol).
        if args.out and "error" not in out:
            tx.progress("review written to {}".format(args.out))
        emit_stdio_result(tx, out)
        sys.exit(0)

    print(json.dumps(out, indent=2))
    if args.out and "error" not in out:
        print("review written to {}".format(args.out), file=sys.stderr)
    sys.exit(code)


if __name__ == "__main__":
    main()
