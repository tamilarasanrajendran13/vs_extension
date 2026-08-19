#!/usr/bin/env python3
"""
knowledge_view - the Docket Knowledge projection. Zero model calls.

One deterministic JSON (schema knowledge.view.v1) feeding every host of the
Knowledge view: the VS Code webview tab, the dashboard tab, and anything
else that wants to render "what does Docket know and what is waiting on a
human". The webview does math on NOTHING - if a number matters, it is
computed here and cited here.

Sections (mirroring reference/knowledge-view-mockup.html):
  overview   counts for the strip; pending items are the headline
  inbox      proposed learnings + unratified context files (reviewed: false)
  decisions  approved/discarded history with reasons - the audit trail
  craft      per-agent ratified lessons parsed from memory/<project>/*.md
  history    knowledge.recall() VERBATIM for recent tickets
  repo       patterns/repo_map freshness + hub files from read_stats.json
  map        every file in the project tree + who touched it (file_touch),
             including GONE files (touched once, no longer in the tree)
  graph      radial layout for the repo graph (Python-computed positions -
             the webview only draws SVG; no D3, no CDN) and the typed
             relations edges from the ledger's edges table

Honesty rules carried over: absent sources render as absent (missing
read_stats -> no hubs key content, never zeros); every touch cites its run;
an unreadable ledger raises rather than inventing an empty view.

CLI (from the docket/ folder):
    python3 scripts/knowledge_view.py --json --project data_project \
        --project-path ../data_project
    python3 scripts/knowledge_view.py --self-test
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import ledger      # noqa: E402
import knowledge   # noqa: E402

SCHEMA = "knowledge.view.v1"

# Same exclusions the context drafter uses - the map must show the tree a
# developer thinks of, not venv noise.
SKIP_DIRS = {".git", "venv", ".venv", "node_modules", "__pycache__", ".idea",
             ".vscode", "target", "build", "dist", ".pytest_cache",
             ".mypy_cache", ".tox", ".eggs", "site-packages", "reports",
             ".claude"}
MAP_EXTS = {".py", ".yaml", ".yml", ".json", ".csv", ".xml", ".md", ".toml",
            ".cfg", ".ini", ".txt"}

DRAFT_MARKER = "reviewed: false"
LESSON_HEADER = "## Learned from tickets"


# ------------------------------------------------------------- ledger reads

def learnings_rows(con):
    return [dict(r) for r in con.execute(
        "SELECT learning_id, created_at, run_id, artifact_path, "
        "proposed_diff, rationale, status, decided_by, decided_at, "
        "discard_reason FROM learnings ORDER BY learning_id")]


def file_touches(con, project):
    """path -> {ticket, run_id, ts, why} for the LATEST touch per path."""
    rows = con.execute(
        "SELECT e.target, e.payload_json, e.ts, r.ticket_id, r.run_id "
        "FROM events e JOIN runs r ON r.run_id = e.run_id "
        "WHERE e.event_type = 'file_touch' AND r.project = ? "
        "ORDER BY e.event_id", (project,)).fetchall()
    out = {}
    counts = {}
    for r in rows:
        p = (r["target"] or "").replace("\\", "/")
        if not p:
            continue
        why = ""
        try:
            why = str(json.loads(r["payload_json"]).get("why") or "")
        except Exception:
            pass
        counts[p] = counts.get(p, 0) + 1
        out[p] = {"ticket": r["ticket_id"], "run_id": r["run_id"],
                  "ts": str(r["ts"])[:10], "why": why[:200],
                  "touches": counts[p]}
    return out


def relation_edges(con, project, limit=200):
    return [dict(r) for r in con.execute(
        "SELECT src_kind, src_id, dst_kind, dst_id, edge_type, weight, "
        "ts, run_id FROM edges ORDER BY edge_id DESC LIMIT ?",
        (limit,))]


# ------------------------------------------------------------ file parsing

def context_status(workbench, project):
    """One context file's state: absent | draft (reviewed: false) | ratified.
    A draft also carries its open questions so the inbox can show them."""
    f = Path(workbench) / "context" / f"{project}.md"
    if not f.exists():
        return {"state": "absent", "path": f"context/{project}.md",
                "questions": []}
    text = f.read_text(encoding="utf-8", errors="replace")
    if DRAFT_MARKER not in text:
        return {"state": "ratified", "path": f"context/{project}.md",
                "questions": []}
    qs = []
    m = re.search(r"## Questions for you.*?\n(.*?)(?=\n## |\Z)", text, re.S)
    if m:
        qs = [q.strip() for q in re.findall(r"^\s*\d+\.\s*(.+?)(?=^\s*\d+\.|\Z)",
                                            m.group(1), re.S | re.M)]
        qs = [re.sub(r"\s+", " ", q)[:240] for q in qs if q.strip()]
    return {"state": "draft", "path": f"context/{project}.md", "questions": qs}


def agent_lessons(workbench, project):
    """[{agent, path, lessons: [...], raw_ok}] from memory/<project>/*.md.
    Lessons are the bullet lines under '## Learned from tickets'; a file
    whose shape surprises degrades to lessons=[] with raw_ok=False so the
    view can say 'unparsed' instead of pretending empty."""
    mdir = Path(workbench) / "memory" / project
    out = []
    if not mdir.is_dir():
        return out
    for f in sorted(mdir.glob("*.md")):
        text = f.read_text(encoding="utf-8", errors="replace")
        lessons, ok = [], True
        if LESSON_HEADER in text:
            tail = text.split(LESSON_HEADER, 1)[1]
            tail = tail.split("\n## ", 1)[0]
            cur = None
            for line in tail.splitlines():
                if line.lstrip().startswith("- "):
                    if cur:
                        lessons.append(cur)
                    cur = line.lstrip()[2:].strip()
                elif cur is not None and line.strip():
                    cur += " " + line.strip()
            if cur:
                lessons.append(cur)
        elif text.strip():
            ok = False
        out.append({"agent": f.stem, "path": f"memory/{project}/{f.name}",
                    "lessons": [l[:300] for l in lessons], "raw_ok": ok})
    return out


def repo_freshness(workbench, project, now=None):
    """Age + basic stats of the derived caches. Absent files -> absent keys."""
    now = now if now is not None else time.time()
    cdir = Path(workbench) / "cache" / project
    out = {}
    pm = cdir / "repo_map.json"
    if pm.exists():
        try:
            d = json.loads(pm.read_text(encoding="utf-8"))
            out["repo_map"] = {
                "age_hours": round((now - pm.stat().st_mtime) / 3600, 1),
                "tree_hash": str(d.get("tree_hash") or "")[:12],
                "modules": len(d.get("modules") or [])}
        except Exception:
            out["repo_map"] = {"unreadable": True}
    pp = cdir / "patterns.json"
    if pp.exists():
        out["patterns"] = {
            "age_hours": round((now - pp.stat().st_mtime) / 3600, 1),
            "bytes": pp.stat().st_size}
    hubs = []
    sp = cdir / "read_stats.json"
    if sp.exists():
        try:
            stats = json.loads(sp.read_text(encoding="utf-8")) or {}
            hubs = sorted(((p, int(c)) for p, c in stats.items()),
                          key=lambda x: (-x[1], x[0]))[:10]
        except Exception:
            pass
        out["read_stats"] = {"files_journaled": len(hubs),
                             "hubs": [{"path": p, "consults": c}
                                      for p, c in hubs]}
    return out


# --------------------------------------------------------------- repo map

def walk_tree(project_path):
    """Relative paths of every mapped file, sorted. Deterministic."""
    root = Path(project_path)
    out = []
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in MAP_EXTS:
            continue
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        out.append(str(rel).replace("\\", "/"))
    return out


def build_map(tree_files, touches):
    """Directory-grouped file entries. GONE = touched but not in the tree."""
    dirs = {}
    for p in tree_files:
        d = p.rsplit("/", 1)[0] + "/" if "/" in p else "./"
        t = touches.get(p)
        dirs.setdefault(d, []).append({
            "name": p.rsplit("/", 1)[-1], "path": p,
            "touch": t if t else None})
    for p, t in sorted(touches.items()):
        if p in tree_files:
            continue
        d = p.rsplit("/", 1)[0] + "/" if "/" in p else "./"
        dirs.setdefault(d, []).append({
            "name": p.rsplit("/", 1)[-1], "path": p, "touch": t,
            "gone": True})
    out = []
    for d in sorted(dirs):
        files = dirs[d]
        out.append({"dir": d, "files": files,
                    "touched": sum(1 for f in files if f.get("touch"))})
    return out


def relations_layout(edges, x_left=150, x_right=560, y0=44, dy=26, cap=40):
    """Bipartite positions for the typed-edges view: src nodes (tickets,
    learnings, ...) in a left column, dst nodes (files, ...) in a right
    column, one link per DISTINCT (src, dst, type) with its touch count.
    Deterministic: nodes sort by (kind, id); links keep the LATEST run_id.
    Links beyond `cap` (by count, then id) are dropped and COUNTED - the
    view says what it left out instead of silently truncating."""
    agg = {}
    for e in edges or []:
        k = (e["src_kind"], e["src_id"], e["dst_kind"], e["dst_id"],
             e["edge_type"])
        a = agg.setdefault(k, {"count": 0, "run_id": None, "ts": ""})
        a["count"] += 1
        ts = str(e.get("ts") or "")
        if ts >= a["ts"]:
            a["ts"], a["run_id"] = ts, e.get("run_id")
    links = sorted(agg.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
    dropped = max(0, len(links) - cap)
    links = links[:cap]
    lefts = sorted({(k[0], k[1]) for k, _ in links})
    rights = sorted({(k[2], k[3]) for k, _ in links})
    lpos = {n: (x_left, y0 + i * dy) for i, n in enumerate(lefts)}
    rpos = {n: (x_right, y0 + i * dy) for i, n in enumerate(rights)}
    out_links = []
    for (sk, si, dk, di, et), a in links:
        sx, sy = lpos[(sk, si)]
        dx_, dy_ = rpos[(dk, di)]
        out_links.append({"src": si, "src_kind": sk, "dst": di,
                          "dst_kind": dk, "type": et, "count": a["count"],
                          "run_id": a["run_id"],
                          "sx": sx, "sy": sy, "dx": dx_, "dy": dy_})
    height = y0 + dy * max(len(lefts), len(rights), 1) + 16
    return {"width": 980, "height": height,
            "left": [{"kind": k, "id": i, "x": lpos[(k, i)][0],
                      "y": lpos[(k, i)][1]} for k, i in lefts],
            "right": [{"kind": k, "id": i, "x": rpos[(k, i)][0],
                       "y": rpos[(k, i)][1]} for k, i in rights],
            "links": out_links, "dropped": dropped}


def radial_layout(map_dirs, cx=500, cy=500, r_dir=165, r_file=300, gap=1.6):
    """Node positions for the repo graph. Sector allocation: every file owns
    one slot on the outer ring, directories are separated by `gap` slots, so
    adjacent fans cannot overlap by construction. Name-ordered -> stable."""
    total = sum(len(d["files"]) for d in map_dirs) + gap * len(map_dirs)
    if total <= 0:
        return {"root": {"x": cx, "y": cy}, "dirs": []}
    unit = 2 * math.pi / total
    cursor = -math.pi / 2
    dirs = []
    for d in map_dirs:
        n = len(d["files"])
        a0 = cursor + (gap / 2) * unit
        mid = a0 + (n / 2) * unit
        cursor += (n + gap) * unit
        entry = {"dir": d["dir"],
                 "x": round(cx + r_dir * math.cos(mid)),
                 "y": round(cy + r_dir * math.sin(mid)),
                 "files": []}
        for j, f in enumerate(d["files"]):
            fa = a0 + (j + 0.5) * unit
            deg = math.degrees(fa) % 360
            entry["files"].append({
                "path": f["path"],
                "x": round(cx + r_file * math.cos(fa)),
                "y": round(cy + r_file * math.sin(fa)),
                "angle": round(deg, 1),
                "flip": 90 < deg < 270})
        dirs.append(entry)
    return {"root": {"x": cx, "y": cy}, "dirs": dirs}


# --------------------------------------------------------------- assembly

def build(project, project_path, workbench=None, db=None, recall_tickets=3):
    workbench = Path(workbench) if workbench else HERE.parent
    db = Path(db) if db else workbench / "ledger.db"
    with ledger.connect(db) as con:
        rows = learnings_rows(con)
        touches = file_touches(con, project)
        edges = relation_edges(con, project)
        tickets = [r["ticket_id"] for r in con.execute(
            "SELECT ticket_id, MAX(started_at) m FROM runs WHERE project=? "
            "GROUP BY ticket_id ORDER BY m DESC LIMIT ?",
            (project, recall_tickets))]
        counts = {k: v for k, v in con.execute(
            "SELECT status, COUNT(*) FROM findings GROUP BY status")}
        escaped = [dict(r) for r in con.execute(
            "SELECT defect_id, bug_ticket_id, origin_ticket, "
            "should_have_caught FROM escaped_defects "
            "ORDER BY defect_id DESC LIMIT 10")]

    proposed = [r for r in rows if r["status"] == "proposed"]
    decided = [r for r in rows if r["status"] in ("approved", "discarded")]
    ctx = context_status(workbench, project)
    craft = agent_lessons(workbench, project)
    repo = repo_freshness(workbench, project)
    tree = walk_tree(project_path) if project_path else []
    map_dirs = build_map(tree, touches)

    history = []
    for t in tickets:
        text = knowledge.recall(
            project, "_KNOWLEDGE_VIEW_", db=db,
            stats_path=workbench / "cache" / project / "read_stats.json")
        history.append({"ticket": t, "recall": text})
        break   # recall is project-scoped; one block, labeled by tickets list
    pending = len(proposed) + (1 if ctx["state"] == "draft" else 0)

    return {
        "schema": SCHEMA,
        "project": project,
        "computed_at": None,   # stamped by the caller/host, not here (replay)
        "overview": {
            "pending": pending,
            "context_state": ctx["state"],
            "approved": sum(1 for r in decided if r["status"] == "approved"),
            "discarded": sum(1 for r in decided if r["status"] == "discarded"),
            "agent_lessons": sum(len(a["lessons"]) for a in craft),
            # hub = >=3 consults, the same bar knowledge.top_reads applies
            # before a file reaches recall; below-threshold journal entries
            # still render in the repo panel, they just are not hubs yet.
            "hub_files": sum(1 for h in
                             (repo.get("read_stats") or {}).get("hubs") or []
                             if h["consults"] >= 3),
            "escaped_defects": len(escaped),
            "confirmed_findings": counts.get("CONFIRMED"),
            "files_total": len(tree),
            "files_touched": sum(d["touched"] for d in map_dirs),
        },
        "inbox": {
            "learnings": [{k: r[k] for k in
                           ("learning_id", "created_at", "run_id",
                            "artifact_path", "proposed_diff", "rationale")}
                          for r in proposed],
            "context": ctx if ctx["state"] != "ratified" else None,
        },
        "decisions": [{k: r[k] for k in
                       ("learning_id", "status", "decided_at",
                        "artifact_path", "proposed_diff", "discard_reason")}
                      for r in sorted(decided,
                                      key=lambda r: -(r["learning_id"]))],
        "craft": craft,
        "history": {"tickets": tickets, "blocks": history},
        "repo": repo,
        "map": map_dirs,
        "graph": {
            "layout": radial_layout(map_dirs),
            "relations": edges,
            "relations_layout": relations_layout(edges),
        },
    }


# --------------------------------------------------------------- self-test

def _self_test():
    import tempfile
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    td = Path(tempfile.mkdtemp())
    wb = td / "wb"
    (wb / "context").mkdir(parents=True)
    (wb / "memory" / "proj").mkdir(parents=True)
    (wb / "cache" / "proj").mkdir(parents=True)
    proj = td / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "a.py").write_text("x = 1\n")
    (proj / "src" / "b.py").write_text("y = 2\n")
    (proj / "top.md").write_text("hi\n")

    db = wb / "ledger.db"
    ledger.init(db)
    run = ledger.start_run("T-1", project="proj", db=db)
    eid = ledger.log(run, "T-1", "lead", "file_touch", target="src/a.py",
                     payload={"why": "extend the reader"}, db=db)
    ledger.log(run, "T-1", "lead", "file_touch", target="src/zz_gone.py",
               payload={"why": "was rolled back"}, db=db)
    with ledger.connect(db) as con:
        con.execute("INSERT INTO learnings (run_id, cited_event_id, "
                    "artifact_path, proposed_diff, rationale, status) "
                    "VALUES (?,?,?,?,?,'proposed')",
                    (run, eid, "context/proj.md", "+ a fact", "because"))
        con.execute("INSERT INTO learnings (run_id, cited_event_id, "
                    "artifact_path, proposed_diff, rationale, status, "
                    "decided_at, discard_reason) "
                    "VALUES (?,?,?,?,?,'discarded','2026-07-31',"
                    "'duplicate')",
                    (run, eid, "memory/proj/reviewer.md", "+ x", "because"))

    (wb / "context" / "proj.md").write_text(
        "reviewed: false\n\n# proj\n\n## Questions for you (answer)\n\n"
        "1. Is this permanent?\n2. Second question here?\n")
    (wb / "memory" / "proj" / "reviewer.md").write_text(
        "# reviewer\n\n## Learned from tickets\n- Reject tautologies.\n"
        "- Check empty arrays\n  explicitly.\n")
    (wb / "cache" / "proj" / "read_stats.json").write_text(
        json.dumps({"src/a.py": 4, "src/b.py": 1}))

    v = build("proj", proj, workbench=wb, db=db)
    check("schema stamped", v["schema"] == SCHEMA)
    check("pending counts proposed learning + draft context",
          v["overview"]["pending"] == 2)
    check("context draft carries both questions",
          v["inbox"]["context"]["state"] == "draft"
          and len(v["inbox"]["context"]["questions"]) == 2)
    check("inbox learning cites its run",
          v["inbox"]["learnings"][0]["run_id"] == run)
    check("decisions carry the discard reason",
          v["decisions"][0]["discard_reason"] == "duplicate")
    check("craft parses multi-line bullets into one lesson",
          v["craft"][0]["lessons"] == ["Reject tautologies.",
                                       "Check empty arrays explicitly."])
    check("hubs read from the journal, ranked",
          v["repo"]["read_stats"]["hubs"][0] == {"path": "src/a.py",
                                                 "consults": 4})
    files = {f["path"]: f for d in v["map"] for f in d["files"]}
    check("map covers the whole tree", {"src/a.py", "src/b.py",
                                        "top.md"} <= set(files))
    check("touched file carries ticket + why-then",
          files["src/a.py"]["touch"]["ticket"] == "T-1"
          and "extend the reader" in files["src/a.py"]["touch"]["why"])
    check("untouched file is first-class with touch=None",
          files["src/b.py"]["touch"] is None)
    check("gone file present, flagged, still cited",
          files["src/zz_gone.py"].get("gone") is True
          and files["src/zz_gone.py"]["touch"]["run_id"] == run)
    lay = v["graph"]["layout"]
    pts = [(f["x"], f["y"]) for d in lay["dirs"] for f in d["files"]]
    check("layout emits one position per mapped file",
          len(pts) == len(files))
    check("layout positions are distinct (no overlap by construction)",
          len(set(pts)) == len(pts))
    v2 = build("proj", proj, workbench=wb, db=db)
    check("layout is deterministic across builds",
          v2["graph"]["layout"] == lay)
    check("recall block present and verbatim-shaped",
          v["history"]["blocks"]
          and "PROJECT MEMORY" in v["history"]["blocks"][0]["recall"])
    # relations layout: bipartite, deduped, honest about truncation
    rl = relations_layout([
        {"src_kind": "ticket", "src_id": "T-1", "dst_kind": "file",
         "dst_id": "src/a.py", "edge_type": "touched", "ts": "2026-07-30",
         "run_id": "r-old"},
        {"src_kind": "ticket", "src_id": "T-1", "dst_kind": "file",
         "dst_id": "src/a.py", "edge_type": "touched", "ts": "2026-07-31",
         "run_id": "r-new"},
        {"src_kind": "learning", "src_id": "L-24", "dst_kind": "file",
         "dst_id": "src/a.py", "edge_type": "learned_from",
         "ts": "2026-07-31", "run_id": "r-new"},
    ])
    check("relations: duplicate touches collapse to one counted link",
          len(rl["links"]) == 2
          and next(l for l in rl["links"]
                   if l["type"] == "touched")["count"] == 2)
    check("relations: the collapsed link keeps the LATEST run",
          all(l["run_id"] == "r-new" for l in rl["links"]))
    check("relations: two left nodes, one right node, distinct rows",
          len(rl["left"]) == 2 and len(rl["right"]) == 1
          and len({(n["x"], n["y"]) for n in rl["left"]}) == 2)
    check("relations: nothing dropped -> dropped 0, cap drops counted",
          rl["dropped"] == 0
          and relations_layout([
              {"src_kind": "t", "src_id": "T%d" % i, "dst_kind": "f",
               "dst_id": "f", "edge_type": "touched", "ts": "", "run_id": ""}
              for i in range(45)], cap=40)["dropped"] == 5)
    check("relations layout rides the projection",
          v["graph"]["relations_layout"]["links"] is not None)
    check("confirmed findings absent (None), never zero-invented",
          v["overview"]["confirmed_findings"] is None)
    check("whole projection is JSON-serializable ASCII",
          all(ord(c) < 128 for c in json.dumps(v)))

    # ratified context leaves the inbox
    (wb / "context" / "proj.md").write_text("# proj\nratified.\n")
    v3 = build("proj", proj, workbench=wb, db=db)
    check("ratified context leaves the inbox",
          v3["inbox"]["context"] is None
          and v3["overview"]["context_state"] == "ratified")
    # absent everything degrades, never crashes
    v4 = build("nope", td / "missing", workbench=wb, db=db)
    check("unknown project/absent tree degrade to empty map",
          v4["map"] == [] and v4["overview"]["files_total"] == 0)

    passed = sum(1 for _, c in ok if c)
    for name, cond in ok:
        print("  [{}] {}".format("PASS" if cond else "FAIL", name))
    print("  {}/{} passed".format(passed, len(ok)))
    return 0 if passed == len(ok) else 1


def main():
    ap = argparse.ArgumentParser(description="Docket Knowledge projection")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--project", default=None)
    ap.add_argument("--project-path", default=None)
    ap.add_argument("--workbench", default=None)
    ap.add_argument("--db", default=None)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(_self_test())
    if not a.project:
        sys.exit("--project required (or --self-test)")
    v = build(a.project, a.project_path, workbench=a.workbench, db=a.db)
    try:
        print(json.dumps(v, indent=1))
    except BrokenPipeError:
        # `... | head` closed the pipe early - normal usage, not an error.
        import os
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)


if __name__ == "__main__":
    main()
