#!/usr/bin/env python3
"""
partitioner - split a plan's tasks into independent slices, deterministically.

The load-bearing, model-free piece of the lead/worker architecture. No transport,
no ledger, no agents - pure functions over task file sets, so it is provable with
plain assertions.

The rule the spike settled (and got wrong the first time): tasks that share a file
go to the SAME slice - one worker runs them in sequence - and only file-DISJOINT
slices become parallel workers. That is connected-components over "shares a file",
not greedy disjoint grouping.

  tasks_from_plan(plan)          -> [{"id","action","file","what"}, ...]
  partition_by_files(tasks)      -> [slice, ...]   (file-disjoint slices)
  apply_dependencies(slices, e)  -> [slice, ...]   (merge lead-flagged logical deps)
  verify_disjoint(slices)        -> [collision,...] (the merge safety net; [] = safe)
  summary(slices)                -> a one-line description for the log

Self-test:  python scripts/partitioner.py --self-test
"""

from __future__ import annotations

import argparse
import sys


def tasks_from_plan(plan):
    """The planner's steps as tasks with stable positional ids - the same shape
    the developer uses, so a slice's ids line up with checkpoints.
    """
    out = []
    for i, st in enumerate(plan.get("steps") or [], 1):
        out.append({
            "id": "task-{:02d}".format(i),
            "action": st.get("action") or "modify",
            "file": (st.get("file") or "").replace("\\", "/").strip(),
            "what": st.get("what") or "",
        })
    return out


# ---------------------------------------------------------------- union-find

class _UF:
    def __init__(self, items):
        self.parent = {x: x for x in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


# ---------------------------------------------------------------- partition

def partition_by_files(tasks):
    """Group tasks so slices are mutually file-disjoint. Tasks sharing a file land
    in one slice (connected component). A task with no file touches nothing and is
    its own independent slice. Slice order and within-slice order follow the input
    order, so the result is deterministic.
    """
    ids = [t["id"] for t in tasks]
    uf = _UF(ids)
    owner = {}  # file -> first task id that touches it
    for t in tasks:
        f = t["file"]
        if not f:
            continue  # no file -> collides with nothing -> stays its own component
        if f in owner:
            uf.union(t["id"], owner[f])
        else:
            owner[f] = t["id"]

    # preserve input order: a component is keyed by the root of its FIRST member
    order = []
    comps = {}
    for t in tasks:
        root = uf.find(t["id"])
        if root not in comps:
            comps[root] = []
            order.append(root)
        comps[root].append(t)
    return [comps[r] for r in order]


def apply_dependencies(slices, edges):
    """Merge slices the lead flagged as logically dependent (a task in one calls
    something a task in another defines, even across different files). Each edge is
    {"from_group": i, "to_group": j} referencing slice indices. Merging only ever
    reduces parallelism - it unions two slices into one sequential worker - so it
    can never introduce a collision. Chains merge transitively.
    """
    n = len(slices)
    if not edges or n <= 1:
        return [list(s) for s in slices]
    uf = _UF(range(n))
    for e in edges:
        a, b = e.get("from_group"), e.get("to_group")
        if isinstance(a, int) and isinstance(b, int) and 0 <= a < n and 0 <= b < n:
            uf.union(a, b)

    order = []
    merged = {}
    for i in range(n):
        root = uf.find(i)
        if root not in merged:
            merged[root] = []
            order.append(root)
        merged[root].extend(slices[i])
    return [merged[r] for r in order]


def verify_disjoint(slices):
    """The safety net: no file may appear in two slices. Returns a list of
    collisions {"file", "slices": [i, j]}; empty means the slices are parallel-safe.
    A correct partition makes this impossible - the check exists to catch a bug,
    not to be relied on for correctness.
    """
    seen = {}  # file -> first slice index
    collisions = []
    for i, s in enumerate(slices):
        for f in {t["file"] for t in s if t["file"]}:
            if f in seen and seen[f] != i:
                collisions.append({"file": f, "slices": [seen[f], i]})
            else:
                seen[f] = i
    return collisions


def summary(slices):
    par = len(slices)
    seq = max((len(s) for s in slices), default=0)
    return ("{} independent slice(s); longest runs {} task(s) in sequence"
            .format(par, seq))


# ==================================================================== self-test

def _plan(*files):
    return {"steps": [{"action": "modify", "file": f, "what": "w"} for f in files]}


def _self_test():
    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    # tasks_from_plan
    tasks = tasks_from_plan(_plan("a.py", "b.py"))
    ok("tasks get positional ids", [t["id"] for t in tasks] == ["task-01", "task-02"])

    # partition_by_files
    t = tasks_from_plan(_plan("src/source.py", "src/registry.py", "src/source.py"))
    slices = partition_by_files(t)
    ok("shared-file tasks land in one slice (01 + 03)",
       any(sorted(x["id"] for x in s) == ["task-01", "task-03"] for s in slices))
    ok("disjoint task is its own slice (02)",
       any([x["id"] for x in s] == ["task-02"] for s in slices))
    ok("two slices total", len(slices) == 2)
    ok("nothing lost or duplicated",
       sorted(x["id"] for s in slices for x in s) == ["task-01", "task-02", "task-03"])
    ok("partition is parallel-safe", verify_disjoint(slices) == [])

    # all-independent
    ind = partition_by_files(tasks_from_plan(_plan("a.py", "b.py", "c.py")))
    ok("all-disjoint -> one slice each", len(ind) == 3)

    # all-shared
    shared = partition_by_files(tasks_from_plan(_plan("x.py", "x.py", "x.py")))
    ok("all-shared -> a single slice", len(shared) == 1 and len(shared[0]) == 3)

    # transitive: e touches f1 (with a,b), c and d touch f2 -> {a,b,e} and {c,d}
    ok("chained shares collapse correctly",
       len(partition_by_files([{"id": "a", "file": "f1"}, {"id": "b", "file": "f1"},
                               {"id": "c", "file": "f2"}, {"id": "d", "file": "f2"},
                               {"id": "e", "file": "f1"}])) == 2)

    # no-file task is independent
    nofile = partition_by_files([{"id": "a", "file": "x.py"}, {"id": "b", "file": ""}])
    ok("a task with no file is its own slice", len(nofile) == 2)

    # edge cases
    ok("empty plan -> no slices", partition_by_files([]) == [])
    ok("single task -> one slice", len(partition_by_files([{"id": "a", "file": "x"}])) == 1)

    # apply_dependencies
    s = partition_by_files(tasks_from_plan(_plan("a.py", "b.py", "c.py")))  # 3 slices
    ok("no edges -> unchanged", len(apply_dependencies(s, [])) == 3)
    merged = apply_dependencies(s, [{"from_group": 0, "to_group": 1}])
    ok("one edge merges two slices", len(merged) == 2)
    ok("merged slice holds both tasks' work",
       any(len(x) == 2 for x in merged))
    chain2 = apply_dependencies(s, [{"from_group": 0, "to_group": 1},
                                    {"from_group": 1, "to_group": 2}])
    ok("chained edges merge all three", len(chain2) == 1)
    ok("out-of-range edge is ignored safely",
       len(apply_dependencies(s, [{"from_group": 0, "to_group": 9}])) == 3)
    ok("merging never introduces a collision", verify_disjoint(merged) == [])

    # verify_disjoint catches a bad partition
    bad = [[{"id": "a", "file": "shared.py"}], [{"id": "b", "file": "shared.py"}]]
    cols = verify_disjoint(bad)
    ok("collision detected across slices",
       len(cols) == 1 and cols[0]["file"] == "shared.py" and cols[0]["slices"] == [0, 1])

    # summary
    ok("summary reports slice count",
       "2 independent slice(s)" in summary(partition_by_files(
           tasks_from_plan(_plan("a.py", "b.py", "a.py")))))

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket task partitioner")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
