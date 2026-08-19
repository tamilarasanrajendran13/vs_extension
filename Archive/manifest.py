#!/usr/bin/env python3
"""
manifest.py - the versioned run manifest (reliability mission 4B /
ACT-038 foundation, 2026-08-05).

Sixteen DATACMP-3 runs could not answer 'what source, config, prompts,
and toolchain did run N execute?' - Docket source changed between almost
every pair of runs, three runs executed on uncommitted code no commit
captures, and 'reproducibility' was structurally unprovable. This module
captures, at run start, an immutable versioned record of every input the
framework controls:

  - docket source: HEAD sha of the repo containing docket/, plus a
    per-file sha256 of every tracked-but-modified file (dirty state is
    RECORDED, never hidden);
  - config hash: sha256 over the canonical JSON of the effective config
    (runtime underscore keys excluded - they are per-run wiring, and
    several are not JSON-serializable);
  - policy: the resolved profile and its required gates;
  - agents: name@version:prompt_sha for every roster agent;
  - ticket: sha256 of the exact ticket text handed to comprehension;
  - models: the role->model pins;
  - toolchain: python version + platform;
  - project: name, resolved path, HEAD sha, dirty file count;
  - overrides: any per-run human overrides (they are provenance);
  - invocation: fresh or resume.

LIMITATION, recorded here on purpose: the model transport does not
guarantee deterministic sampling, so this manifest supports DETERMINISTIC
REPLAY (captured responses) and input-identity comparison - it never
claims exact-output reproducibility.

The manifest is written to the ticket workspace
(evidence/manifest-<run8>.json - one file per run, never overwritten),
registered as a ledger artifact (sha256), and logged as a manifest
event. Best-effort by contract: a manifest failure must never kill a
run, but it says so out loud.

Self-test:  python manifest.py --self-test
Pure ASCII. Stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

SCHEMA = "docket.manifest.v2"

# Mission Task 2 (2026-08-05). The manifest is a REQUIRED artifact of
# the current reliability contract, not a best-effort nicety. Live run
# DATACMP-0-7744ae27 printed
#     [manifest] module unavailable (name 'workbench' is not defined)
# and continued: the run executed 26 model calls and 501k tokens whose
# inputs no artifact captured. A run that cannot be reproduced, replayed
# or disputed does not start.
MANIFEST_REQUIRED = True

# The workflow-level typed class for a manifest refusal (workflow.py
# FAILURE_CLASSES). Docket-owned, retryable once the cause is fixed.
MANIFEST_FAILURE_CLASS = "tooling_failure"


class ManifestUnavailable(RuntimeError):
    """The required run manifest could not be built or persisted. The
    caller MUST stop the run before the first model call - there is no
    degraded-but-continuing mode for a required contract artifact."""


def _sha(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _git(args, cwd, timeout=15, strip=True):
    """strip=False preserves LEADING whitespace, which `git status
    --porcelain` uses: an unstaged modification is ' M src/a.py'.
    Stripping shifted every field by one, so the FIRST dirty file was
    recorded as 'rc/a.py' with no sha256 - the reproducibility evidence
    for the most-edited file in the run was fiction (2026-08-05 audit)."""
    try:
        p = subprocess.run(["git"] + args, cwd=str(cwd),
                           capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        if p.returncode != 0:
            return None
        return p.stdout.strip() if strip else p.stdout.rstrip("\n")
    except Exception:
        return None


def _repo_state(root) -> dict:
    """HEAD sha + per-file sha256 of tracked-but-modified files. A repo
    state with dirty entries is honest: those bytes ran, no commit holds
    them."""
    if root is None:
        return {"head": None, "dirty": []}
    head = _git(["rev-parse", "HEAD"], root)
    if head is None:
        return {"head": None, "dirty": []}
    out = _git(["status", "--porcelain"], root, strip=False) or ""
    # Porcelain paths are relative to the WORKTREE ROOT, not to the
    # directory git ran in.
    top = _git(["rev-parse", "--show-toplevel"], root)
    base = Path(top) if top else Path(root)
    dirty = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        status, rel = line[:2], line[3:].strip().strip('"')
        if " -> " in rel:                      # a rename: hash the NEW path
            rel = rel.split(" -> ", 1)[1].strip().strip('"')
        if status.strip() in ("??",):
            entry = {"path": rel, "status": "untracked"}
        else:
            entry = {"path": rel, "status": status.strip() or "M"}
        f = base / rel
        if f.is_file():
            try:
                entry["sha256"] = _sha(f.read_bytes())
            except OSError:
                entry["sha256"] = None
        dirty.append(entry)
        if len(dirty) >= 200:
            dirty.append({"path": "(truncated at 200 entries)",
                          "status": "-"})
            break
    return {"head": head, "dirty": dirty}


def _config_hash(cfg: dict) -> str:
    """Canonical hash of the EFFECTIVE config. Runtime wiring keys
    (underscore-prefixed) are excluded: they are per-run plumbing, and
    several are unserializable objects."""
    def _clean(v):
        if isinstance(v, dict):
            return {k: _clean(x) for k, x in sorted(v.items())
                    if not str(k).startswith("_")}
        if isinstance(v, (list, tuple)):
            return [_clean(x) for x in v]
        if isinstance(v, (str, int, float, bool)) or v is None:
            return v
        return str(v)
    return _sha(json.dumps(_clean(cfg or {}), sort_keys=True))


def _agents(workbench) -> list:
    """name@version:prompt_sha for every roster agent, computed from the
    agent files themselves - never from a cache."""
    agents_dir = Path(workbench) / "agents"
    if not agents_dir.is_dir():
        agents_dir = HERE / "agents"
    out = []
    for p in sorted(agents_dir.glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
            version = "0"
            for line in text.splitlines()[:15]:
                if line.strip().startswith("version:"):
                    version = line.split(":", 1)[1].strip()
                    break
            out.append("{}@{}:{}".format(p.stem, version,
                                         _sha(text)[:8]))
        except OSError:
            out.append("{}@unreadable".format(p.stem))
    return out


def _resolve_cap(cfg: dict, token_cap):
    """The effective recorded-token cap, as the metered authority
    resolves it. Passed in by the loop (which resolved it before the
    first call); resolved here for direct/self-test callers."""
    if isinstance(token_cap, dict):
        return {"value": token_cap.get("value"),
                "source": token_cap.get("source")}
    try:
        import model_authority
        return model_authority.resolve_cap(cfg)
    except Exception:
        return {"value": None, "source": "unresolved"}


def _invocation_args(cfg: dict):
    """[45] The loop process's own argv as a STRUCTURED, REDACTED array.

    The 2026-08-07 launch failure was undiagnosable from the manifest
    because invocation was one word - the exact argv was discarded.
    cfg["_invocation_args"] is set by loop.main from sys.argv; absent
    (in-process callers, old launches) records honest None. Redaction
    is the gateway's single authority; if it cannot be imported the
    args are WITHHELD rather than persisted unredacted."""
    args = (cfg or {}).get("_invocation_args")
    if not isinstance(args, list):
        return None
    try:
        from headless_gateway import _redact
    except Exception:
        return ["[args withheld - redaction unavailable]"]
    return [_redact(str(a))[:400] for a in args]


def _transport_caps(cfg: dict):
    """[T12] The transport capability document the loop probed at run
    start (docket.transport.capabilities.v1).

    The manifest already records WHAT ran; this records what the thing
    answering the model calls could actually DO - the transport identity,
    which model each role really resolved to versus what was pinned, and
    whether the provider reports a cache metric or a dollar cost at all.
    Those are inputs to every cost and token number downstream, and until
    now no run captured them.

    Absent (no transport spoke, an in-process caller) records honest None.
    Present, it is normalized to the FULL ten-field contract so a partial
    reply from an older gateway can never land here looking complete -
    and so an undeclared capability reads "unavailable" rather than
    False."""
    raw = (cfg or {}).get("_transport_capabilities")
    if not isinstance(raw, dict):
        return None
    try:
        import transport as _tx
        return _tx.normalize_capabilities(raw)
    except Exception:
        # Never drop the evidence on an import failure; recording the raw
        # reply beats recording nothing.
        return dict(raw)


def build(cfg: dict, ticket_id: str, ticket_text: str, project: str,
          project_path, workbench, intent: str = "fresh",
          workflow_id=None, token_cap=None, run_id=None) -> dict:
    """The manifest dict. Deterministic given the same inputs and disk
    state; never raises (every probe degrades to None/[])."""
    cfg = cfg or {}
    try:
        _sp = HERE / "scripts"
        if str(_sp) not in sys.path:
            sys.path.insert(0, str(_sp))
        import governor
        profile = ((cfg.get("policy") or {}).get("profile")
                   or governor.DEFAULT_PROFILE)
        required = governor.required_gates(cfg)
    except Exception:
        profile, required = None, []
    gates_cfg = {k: bool((v or {}).get("enabled", True))
                 for k, v in (cfg.get("gates") or {}).items()
                 if isinstance(v, dict)}
    return {
        "schema": SCHEMA,
        "ticket_id": ticket_id,
        "run_id": run_id,
        "workflow_id": workflow_id,
        "ticket_sha256": _sha(ticket_text or ""),
        "invocation": intent,
        # [45] The exact argv this loop process received (structured,
        # redacted; None when not launched as a process) and the
        # gateway's own DECLARED launch settings (shakedown max,
        # provider budget value/source - the [42.0] gateway-owned
        # values that never appear in loop-side state).
        "invocation_args": _invocation_args(cfg),
        "gateway_launch": (dict(cfg["_gateway_launch"])
                           if isinstance((cfg or {}).get(
                               "_gateway_launch"), dict) else None),
        # [T12] What the transport declared it can do (all ten fields, or
        # None when nothing was probed).
        "transport": _transport_caps(cfg),
        "token_cap": _resolve_cap(cfg, token_cap),
        "docket_source": _repo_state(_git(["rev-parse", "--show-toplevel"],
                                          HERE) or None),
        "config_sha256": _config_hash(cfg),
        "policy": {"profile": profile,
                   "required_gates": required,
                   "gates_enabled": gates_cfg},
        "overrides": cfg.get("_overrides") or {},
        "agents": _agents(workbench),
        "models": dict(cfg.get("models") or {}),
        "toolchain": {"python": platform.python_version(),
                      "platform": platform.platform()},
        "project": {
            "name": project,
            "path": str(project_path) if project_path else None,
            # The CANONICAL path is what actually executed. The live run
            # was launched with '--project-path ../dat' and ran a
            # different tree; recording only the given string would have
            # preserved the lie.
            "canonical_path": (str(Path(project_path).resolve())
                               if project_path else None),
            "explicit": bool(cfg.get("_project_path")),
            "head": _git(["rev-parse", "HEAD"], project_path)
            if project_path else None,
            "dirty_files": len((_git(["status", "--porcelain"],
                                     project_path) or "").splitlines())
            if project_path else None,
        },
        "determinism_note": ("transport does not guarantee deterministic "
                             "sampling; this manifest supports captured-"
                             "response replay and input identity, never "
                             "exact-output reproducibility"),
    }


def record_required(cfg: dict, run_id: str, ticket_id: str,
                    ticket_text: str, project: str, project_path,
                    workbench, release, db, say=None,
                    intent: str = "fresh", workflow_id=None,
                    token_cap=None) -> dict:
    """Build and persist the REQUIRED run manifest: workspace file +
    hashed ledger artifact + ledger event. Returns the manifest.

    Raises ManifestUnavailable on ANY failure - an invalid project
    path, an unwritable workspace, an unreachable ledger. There is no
    degraded mode: the caller stops the run before the first model
    call. This is the whole point of the mission-2 change; the previous
    best-effort contract let a NameError at the call site silently cost
    every run its provenance."""
    say = say or (lambda *_: None)

    # 1. the project path must be REAL when there is one. A manifest
    #    naming a path that does not exist records nothing anyone can
    #    reproduce from - that is a refusal.
    #    NO path at all is a different thing and stays legal: Docket
    #    already runs comprehension against a project it cannot find
    #    (load_patterns says "NO PATTERNS"), and the manifest records
    #    that truthfully rather than inventing a tree.
    pp = None
    if project_path is not None:
        pp = Path(project_path)
        if not pp.exists():
            raise ManifestUnavailable(
                "project path does not exist: {}".format(pp))
    else:
        say("  [manifest] no project path resolved - recorded as "
            "unresolved (nothing to reproduce a code change against)")

    # 2. build (never raises on its own probes, but a caller bug does)
    try:
        man = build(cfg, ticket_id, ticket_text, project, pp, workbench,
                    intent=intent, workflow_id=workflow_id,
                    token_cap=token_cap, run_id=run_id)
    except Exception as e:
        raise ManifestUnavailable(
            "manifest build failed: {}: {}".format(type(e).__name__,
                                                   str(e)[:200])) from e

    # 3. filesystem
    rel = "evidence/manifest-{}.json".format(str(run_id)[-8:])
    try:
        dev = (Path(workbench) / "development" / (release or "unreleased")
               / ticket_id / "evidence")
        dev.mkdir(parents=True, exist_ok=True)
        (dev.parent / rel).write_text(json.dumps(man, indent=2),
                                      encoding="utf-8")
    except OSError as e:
        raise ManifestUnavailable(
            "manifest could not be written: {}".format(str(e)[:200])) from e

    # 4. ledger event + hashed artifact
    try:
        import ledger
        ledger.log(run_id, ticket_id, "system", "message",
                   {"text": "run manifest recorded",
                    "manifest_sha256": _sha(json.dumps(man, sort_keys=True)),
                    "config_sha256": man["config_sha256"],
                    "docket_head": (man["docket_source"] or {}).get("head"),
                    "docket_dirty": len((man["docket_source"] or {})
                                        .get("dirty") or []),
                    "token_cap": (man.get("token_cap") or {}).get("value"),
                    "token_cap_source": (man.get("token_cap")
                                         or {}).get("source"),
                    "workflow_id": man.get("workflow_id"),
                    "project_path": (man.get("project")
                                     or {}).get("canonical_path"),
                    "profile": (man["policy"] or {}).get("profile")},
                   db=db)
        ledger.record_artifact(run_id, ticket_id, "evidence", rel,
                               workspace_path=str(dev.parent),
                               actor="system", db=db)
    except Exception as e:
        raise ManifestUnavailable(
            "manifest could not be recorded in the ledger: {}: {}".format(
                type(e).__name__, str(e)[:200])) from e
    say("  [manifest] recorded ({}; cap {})".format(
        rel, (man.get("token_cap") or {}).get("value")))
    return man


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    import tempfile
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        wb = td / "wb"
        (wb / "agents").mkdir(parents=True)
        (wb / "agents" / "developer.md").write_text(
            "---\nname: developer\nversion: 14\nmodel: worker\n---\n"
            + "x" * 250, encoding="utf-8")
        cfg = {"models": {"worker": "m1"}, "gates": {
            "security_snyk": {"enabled": False}},
            "_runtime_junk": object()}

        m = build(cfg, "T-1", "the ticket text", "proj", None, wb)
        check("schema stamped", m["schema"] == SCHEMA)
        check("ticket hashed", m["ticket_sha256"] == _sha("the ticket text"))
        check("agent stamp carries name@version:sha",
              len(m["agents"]) == 1
              and m["agents"][0].startswith("developer@14:")
              and len(m["agents"][0].split(":")[1]) == 8)
        check("unserializable runtime keys never break the config hash",
              len(m["config_sha256"]) == 64)
        check("policy resolved from config",
              m["policy"]["profile"] == "full-development"
              and "mutation" in m["policy"]["required_gates"]
              and m["policy"]["gates_enabled"] == {"security_snyk": False})
        check("determinism limitation recorded, never overclaimed",
              "never" in m["determinism_note"]
              and "exact-output" in m["determinism_note"])

        # [45] THE FORENSIC GAP: invocation was one word ("fresh"), so
        # the exact argv loop.py received was unrecoverable - the
        # 2026-08-07 launch failure could not be diagnosed from the
        # manifest. Now a STRUCTURED, REDACTED array plus the gateway's
        # own declared launch settings.
        check("[45] no argv provided -> honest None, never a "
              "fabricated list",
              m["invocation_args"] is None
              and m["gateway_launch"] is None)
        _inv_cfg = dict(cfg)
        _inv_cfg["_invocation_args"] = [
            "--stdio", "--ticket", "DATACMP-0", "--sessions", "on",
            "--jira-token", "ANTHROPIC_AUTH_TOKEN=hunter2secret"]
        _inv_cfg["_gateway_launch"] = {"shakedown_max": 75000,
                                       "session_budget_usd": 2.0,
                                       "budget_source": "cli"}
        m_inv = build(_inv_cfg, "T-1", "the ticket text", "proj", None,
                      wb)
        check("[45] the loop argv is persisted as a STRUCTURED array, "
              "not a reconstructed shell string",
              isinstance(m_inv["invocation_args"], list)
              and "--ticket" in m_inv["invocation_args"]
              and "DATACMP-0" in m_inv["invocation_args"]
              and "on" in m_inv["invocation_args"])
        check("[45] the existing credential redaction applies BEFORE "
              "persistence - a secret-shaped value never lands in the "
              "manifest",
              not any("hunter2secret" in str(a)
                      for a in m_inv["invocation_args"])
              and any("[redacted]" in str(a)
                      for a in m_inv["invocation_args"]))
        check("[45] the gateway's declared launch settings are "
              "recorded verbatim and separately",
              m_inv["gateway_launch"] == {"shakedown_max": 75000,
                                          "session_budget_usd": 2.0,
                                          "budget_source": "cli"})

        # [T12] The TRANSPORT CAPABILITY DOCUMENT. Sixteen DATACMP-3 runs
        # could not say what source and config executed; no run at all
        # could say what the thing answering its model calls could
        # actually DO - which model each role really got, whether the
        # provider reported a cache metric or a dollar cost. Those are
        # inputs to every downstream number, so they belong in the same
        # immutable record.
        import transport as _tx_m
        check("[T12] no transport spoke -> honest None, never a "
              "fabricated all-capable document",
              m["transport"] is None)
        _cap_cfg = dict(cfg)
        _cap_cfg["_transport_capabilities"] = {
            "schema": _tx_m.CAPABILITY_SCHEMA,
            "transport": {"name": "vscode-lm", "version": "0.0.1"},
            "provider": "copilot",
            "models": {"worker": {"requested": "gpt-4o",
                                  "effective": {"family": "gpt-4o"}}},
            "sessions": False, "token_counting": True,
            "cache_metrics": "unavailable", "cost_usd": "unavailable",
            "cancellation": True, "concurrent_requests": True,
            "tool_calls": False}
        m_cap = build(_cap_cfg, "T-1", "the ticket text", "proj", None, wb)
        check("[T12] the capability document is persisted with the run - "
              "identity, per-role model drift, and the declared "
              "no-sessions fact",
              m_cap["transport"]["transport"]["name"] == "vscode-lm"
              and m_cap["transport"]["provider"] == "copilot"
              and m_cap["transport"]["models"]["worker"]["requested"]
              == "gpt-4o"
              and m_cap["transport"]["sessions"] is False)
        check("[T12] a provider metric the transport cannot expose is "
              "recorded 'unavailable' - the Cost tab must never read "
              "an absent number as $0.00",
              m_cap["transport"]["cost_usd"] == "unavailable"
              and m_cap["transport"]["cache_metrics"] == "unavailable")
        # A PARTIAL document (an older gateway that answers only the
        # session bit) must still land as the full ten fields, with the
        # rest unavailable - not silently short.
        m_part = build(dict(cfg, _transport_capabilities={"sessions": True}),
                       "T-1", "t", "proj", None, wb)
        check("[T12] a partial reply is normalized to the full contract: "
              "every undeclared capability reads unavailable, never False",
              all(f in m_part["transport"]
                  for f in _tx_m.CAPABILITY_FIELDS)
              and m_part["transport"]["sessions"] is True
              and m_part["transport"]["cost_usd"] == "unavailable"
              and m_part["transport"]["tool_calls"] == "unavailable"
              and not any(m_part["transport"][f] is False
                          for f in _tx_m.CAPABILITY_FIELDS))
        check("[T12] the capability document does not disturb the config "
              "hash - it is evidence about the transport, not config",
              m_cap["config_sha256"] == m["config_sha256"])

        m2 = build(cfg, "T-1", "the ticket text", "proj", None, wb)
        check("same inputs -> identical manifest (minus source probes)",
              _sha(json.dumps(m2, sort_keys=True))
              == _sha(json.dumps(m, sort_keys=True)))
        m3 = build(dict(cfg, extra=1), "T-1", "the ticket text", "proj",
                   None, wb)
        check("a config change changes the config hash",
              m3["config_sha256"] != m["config_sha256"])
        m4 = build(cfg, "T-1", "different ticket text", "proj", None, wb)
        check("a ticket change changes the ticket hash",
              m4["ticket_sha256"] != m["ticket_sha256"])

        # Mission Task 2: the manifest is a REQUIRED contract artifact.
        check("the module declares the manifest REQUIRED",
              MANIFEST_REQUIRED is True
              and MANIFEST_FAILURE_CLASS == "tooling_failure")
        proj_dir = td / "projtree"
        proj_dir.mkdir()
        m5 = build(cfg, "T-1", "t", "proj", proj_dir, wb,
                   workflow_id="wf-1", token_cap={"value": 150000,
                                                  "source": "override"},
                   run_id="R-1")
        check("the manifest carries the effective token cap and source",
              m5["token_cap"] == {"value": 150000, "source": "override"})
        check("the manifest carries the workflow and run identity",
              m5["workflow_id"] == "wf-1" and m5["run_id"] == "R-1")
        check("the manifest records the CANONICAL project path",
              m5["project"]["canonical_path"]
              == str(proj_dir.resolve()))
        check("an unresolved cap is stated, never faked as 0",
              build(cfg, "T-1", "t", "proj", proj_dir, wb)["token_cap"]
              ["value"] is None)

        # record_required(): file + ledger artifact + event.
        import ledger as _l
        db = td / "led.db"
        _l.init(db)
        rid = _l.start_run("T-1", db=db)
        said = []
        man = record_required(cfg, rid, "T-1", "the ticket text", "proj",
                              proj_dir, wb, None, db, say=said.append,
                              workflow_id="wf-9",
                              token_cap={"value": 150000,
                                         "source": "override"})
        mp = (wb / "development" / "unreleased" / "T-1" / "evidence"
              / "manifest-{}.json".format(rid[-8:]))
        check("manifest written to the workspace", mp.is_file()
              and json.loads(mp.read_text())["run_id"] == rid)
        with _l.connect(db) as con:
            ev = con.execute(
                "SELECT payload_json FROM events WHERE run_id=? AND "
                "payload_json LIKE '%run manifest recorded%'",
                (rid,)).fetchone()
            art = con.execute(
                "SELECT sha256 FROM artifacts WHERE run_id=? AND "
                "rel_path LIKE 'evidence/manifest-%'", (rid,)).fetchone()
        check("manifest event carries the hashes",
              ev is not None
              and len(json.loads(ev["payload_json"])["manifest_sha256"])
              == 64)
        check("manifest event carries the cap and the workflow identity",
              json.loads(ev["payload_json"])["token_cap"] == 150000
              and json.loads(ev["payload_json"])["workflow_id"] == "wf-9")
        check("manifest registered as a hashed artifact",
              art is not None and art["sha256"] is not None)
        check("recording is announced, never silent",
              any("[manifest] recorded" in s for s in said))

        # --- the four required refusals (all typed, none degrading) ---
        def _refused(**kw):
            base = dict(cfg=cfg, run_id=rid, ticket_id="T-1",
                        ticket_text="t", project="proj",
                        project_path=proj_dir, workbench=wb,
                        release=None, db=db)
            base.update(kw)
            try:
                record_required(**base)
            except ManifestUnavailable as e:
                return str(e)
            return None

        _nosaid = []
        _noman = record_required(
            cfg, rid, "T-1", "t", "proj", None, wb, None, db,
            say=_nosaid.append)
        check("NO project path is recorded truthfully as unresolved, "
              "never invented",
              _noman["project"]["canonical_path"] is None
              and any("unresolved" in s for s in _nosaid))
        check("a nonexistent project path is REFUSED",
              "does not exist" in (_refused(
                  project_path=td / "no-such-tree") or ""))
        check("an unwritable workspace is REFUSED (typed, not swallowed)",
              "could not be written" in (_refused(
                  workbench=Path("/dev/null/nope")) or ""))
        check("an unreachable ledger is REFUSED (typed, not swallowed)",
              "ledger" in (_refused(db=td / "no" / "such.db") or ""))
        check("the refusal type is the declared one",
              issubclass(ManifestUnavailable, RuntimeError))

        # AUDIT 2026-08-05: `git status --porcelain` encodes state in the
        # first TWO columns, so an unstaged modification is ' M src/a.py'.
        # _git stripped the leading space, shifting every field: the
        # FIRST dirty file was recorded as 'rc/a.py' with no sha256. The
        # reproducibility evidence for the most-edited file in the run
        # was fiction, silently.
        import subprocess as _sp_a
        gr = td / "gitrepo"
        (gr / "src").mkdir(parents=True)
        (gr / "src" / "a.py").write_text("x = 1\n", encoding="ascii")
        (gr / "src" / "b.py").write_text("y = 1\n", encoding="ascii")
        _git_ok = True
        try:
            for args in (["init", "-q"],
                         ["-c", "user.email=t@e", "-c", "user.name=t",
                          "add", "."],
                         ["-c", "user.email=t@e", "-c", "user.name=t",
                          "commit", "-q", "-m", "one"]):
                _sp_a.run(["git"] + args, cwd=str(gr), check=True,
                          capture_output=True)
        except Exception:
            _git_ok = False
        if _git_ok:
            (gr / "src" / "a.py").write_text("x = 2\n", encoding="ascii")
            (gr / "src" / "b.py").write_text("y = 2\n", encoding="ascii")
            st = _repo_state(gr)
            paths = [d["path"] for d in st["dirty"]]
            check("AUDIT: the FIRST dirty file's path is recorded intact "
                  "(porcelain's leading status column is not stripped)",
                  "src/a.py" in paths and "rc/a.py" not in paths)
            check("AUDIT: EVERY dirty file is hashed - none silently "
                  "unhashed",
                  all(d.get("sha256") for d in st["dirty"])
                  and len(st["dirty"]) == 2)
            # ...and a project that is a SUBDIRECTORY of the repo still
            # resolves its dirty paths (porcelain is root-relative).
            sub = _repo_state(gr / "src")
            check("AUDIT: dirty paths resolve against the WORKTREE ROOT, "
                  "not the directory git ran in",
                  all(d.get("sha256") for d in sub["dirty"]))

    passed = sum(1 for _, c in ok if c)
    for name, cond in ok:
        print("  [{}] {}".format("PASS" if cond else "FAIL", name))
    print("\n  {}/{} passed".format(passed, len(ok)))
    return 0 if passed == len(ok) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Docket run manifest (4B)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
