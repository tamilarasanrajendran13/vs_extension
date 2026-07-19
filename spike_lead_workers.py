#!/usr/bin/env python3
"""
spike_lead_workers - a THROWAWAY proof that the lead/worker idea works on the
rig we already have. Not the real implementation; a spike to de-risk #2 before
building it.

What it proves, with MockTransport only (no VS Code, no real model):
  1. a lead can PARTITION tasks into independent groups from their file sets
     (deterministic - no model),
  2. it can SPAWN one sub-agent per group, each calling the model through the
     SAME transport every agent already uses,
  3. it can COLLECT their results and MERGE, and
  4. a bad partition (two workers on one file) is DETECTED, not silently merged.

The whole point: the spawning pattern reuses tx.chat exactly as the twelve agents
do, so the concurrency cap (1 today on the serialized vscode.lm gateway, N later)
changes nothing about correctness - the mock replays replies in order either way.

Run:  python spike_lead_workers.py
"""

from __future__ import annotations

import concurrent.futures as futures


# --------------------------------------------------------------- the transport
# The same shape as the real transport: tx.chat(model, system, user) -> {text,...}.
# Here it just replays scripted replies, keyed by worker, so no model is called.

class MockTransport:
    def __init__(self, replies):
        self.replies = dict(replies)     # worker_id -> reply text
        self.calls = []                  # for assertions: what was asked, in order

    def chat(self, model, system, user, key=None):
        self.calls.append({"model": model, "key": key, "user": user})
        return {"text": self.replies.get(key, "{}"), "model": model,
                "tokens_in": 5, "tokens_out": 9}


# --------------------------------------------------------------- deterministic
# The load-bearing, model-free part: which tasks can run at once.

def partition(tasks):
    """Group tasks so that GROUPS are mutually file-disjoint - no file appears in
    two groups - which is what makes them safe to run as parallel workers. Tasks
    that share a file must therefore land in the SAME group (that worker runs them
    in sequence). This is connected-components over 'shares a file'.

    tasks: [{"id","file","what"}]  ->  [[task,...], ...]  (file-disjoint groups)
    """
    parent = {t["id"]: t["id"] for t in tasks}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    owner = {}                     # file -> a task that touches it
    for t in tasks:
        f = t["file"]
        if f in owner:
            union(t["id"], owner[f])   # shares a file -> same component
        else:
            owner[f] = t["id"]

    comps = {}
    for t in tasks:
        comps.setdefault(find(t["id"]), []).append(t)
    return list(comps.values())


# --------------------------------------------------------------- the sub-agent
# A worker is just a normal agent call through the transport - the thing every
# one of the twelve agents already does.

def run_worker(tx, worker_id, group):
    """One sub-agent: implements its group of tasks. Returns which files it
    'wrote'. In the real system this is a full developer run scoped to a slice;
    here it just calls the model once and reports its files.
    """
    files = [t["file"] for t in group]
    prompt = "Implement tasks {} touching {}".format(
        [t["id"] for t in group], files)
    reply = tx.chat("worker", "You are a sub-developer.", prompt, key=worker_id)
    # the worker reports the files it touched (the mock reply drives this)
    return {"worker": worker_id, "tasks": [t["id"] for t in group],
            "files": files, "reply": reply["text"]}


# --------------------------------------------------------------- the lead
# Partitions, spawns, collects, merges. Never calls the model itself.

def run_lead(tx, tasks, max_concurrency=1):
    """Partition -> spawn a worker per group -> collect -> merge, detecting any
    file collision across workers. max_concurrency is the only knob that changes
    with the transport: 1 today (serialized vscode.lm), N if it ever parallelises.
    """
    groups = partition(tasks)
    results = []

    # Spawn one worker per group. With cap=1 this is sequential; with cap>1 the
    # pool runs them at once. Correctness is identical either way - the merge
    # check below does not care how they were scheduled.
    if max_concurrency <= 1:
        for i, g in enumerate(groups):
            results.append(run_worker(tx, "w{}".format(i), g))
    else:
        with futures.ThreadPoolExecutor(max_workers=max_concurrency) as pool:
            futs = {pool.submit(run_worker, tx, "w{}".format(i), g): i
                    for i, g in enumerate(groups)}
            for fut in futures.as_completed(futs):
                results.append(fut.result())
        results.sort(key=lambda r: r["worker"])

    # MERGE with collision detection: no file may be claimed by two workers. If
    # the partition was right this is impossible; the check is the safety net.
    seen = {}
    collisions = []
    for r in results:
        for f in r["files"]:
            if f in seen and seen[f] != r["worker"]:
                collisions.append((f, seen[f], r["worker"]))
            seen[f] = r["worker"]

    return {"groups": len(groups), "workers": len(results),
            "results": results, "merged_files": sorted(seen),
            "collisions": collisions, "ok": not collisions}


# ==================================================================== the spike

def main():
    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))
        print("  [{}] {}".format("ok " if cond else "XX", name))

    # A plan: A and B are independent (different files); C shares file1 with A,
    # so C must NOT ride in the same group as A.
    tasks = [
        {"id": "task-01", "file": "src/source.py", "what": "add source"},
        {"id": "task-02", "file": "src/registry.py", "what": "register it"},
        {"id": "task-03", "file": "src/source.py", "what": "extend source"},
    ]

    print("\nPartitioning:")
    groups = partition(tasks)
    ok("tasks sharing a file land in ONE worker (task-01 + task-03 on source.py)",
       any({t["id"] for t in g} == {"task-01", "task-03"} for g in groups))
    ok("file-disjoint task becomes its own parallel worker (task-02 on registry.py)",
       any({t["id"] for t in g} == {"task-02"} for g in groups))
    ok("groups are mutually file-disjoint (parallel-safe)",
       len(set().union(*[{t["file"] for t in g} for g in groups])) ==
       sum(len({t["file"] for t in g}) for g in groups))
    ok("nothing is lost or duplicated",
       sorted(t["id"] for g in groups for t in g) == ["task-01", "task-02", "task-03"])

    # Each worker's scripted reply (keyed by worker id). No model is called.
    replies = {"w0": "done: source tasks", "w1": "done: registry"}

    print("\nLead spawns workers (cap=1, serialized - today's gateway):")
    tx = MockTransport(replies)
    out = run_lead(tx, tasks, max_concurrency=1)
    ok("lead spawned one worker per group", out["workers"] == out["groups"])
    ok("every worker went through the SAME transport",
       len(tx.calls) == out["workers"] and all(c["model"] == "worker" for c in tx.calls))
    ok("no file collisions across workers", out["ok"] and out["collisions"] == [])
    ok("merge covers every touched file",
       out["merged_files"] == ["src/registry.py", "src/source.py"])

    print("\nSame lead, cap=3 (parallel - a future gateway). Result must be identical:")
    tx2 = MockTransport(replies)
    out2 = run_lead(tx2, tasks, max_concurrency=3)
    ok("parallel run spawns the same workers", out2["workers"] == out["workers"])
    ok("parallel run reaches the SAME merged files", out2["merged_files"] == out["merged_files"])
    ok("parallel run still calls the model once per worker", len(tx2.calls) == out2["workers"])

    print("\nSafety net: a BAD partition (two workers on one file) is caught:")
    bad = {"groups": 2, "workers": 2, "results": [
        {"worker": "w0", "tasks": ["t1"], "files": ["src/shared.py"], "reply": ""},
        {"worker": "w1", "tasks": ["t2"], "files": ["src/shared.py"], "reply": ""}]}
    # reuse the merge logic by feeding hand-built colliding results
    seen, collisions = {}, []
    for r in bad["results"]:
        for f in r["files"]:
            if f in seen and seen[f] != r["worker"]:
                collisions.append(f)
            seen[f] = r["worker"]
    ok("collision detected, not silently merged", collisions == ["src/shared.py"])

    passed = sum(1 for _, c in checks if c)
    print("\n{}/{} checks passed".format(passed, len(checks)))
    print("\nSpike result: a lead can partition, spawn N sub-agents through the same"
          "\ntransport, and merge safely - with the concurrency cap as the only knob"
          "\nthat changes between serialized (today) and parallel (later).")
    return passed == len(checks)


if __name__ == "__main__":
    import sys
    sys.exit(0 if main() else 1)
