#!/usr/bin/env python3
"""
member_chain.py - generic receiver/member-chain validation (live-readiness
mission Task 9, 2026-08-05).

WHY THIS EXISTS. Live run DATACMP-0-7744ae27 generated an acceptance
test that asserted a member the target class does not have. The freeze
gate caught it - at RUNTIME, after paying to write the suite - and asked
for a correction. The correction came back with the SAME invalid member.
So did the full regeneration after that. Three generations, ~109k output
tokens, one defect, no convergence, and the run blocked without ever
reaching development.

Two things were missing, and neither is specific to that ticket:

  1. STATIC validation. The invalid chain was decidable from the
     project's own source before a single test was executed: the class
     existed, its members were known, and the asserted one was not among
     them. Runtime is the backstop, not the first line.

  2. A CORRECTION that could converge. The model was told which member
     was wrong in prose and handed the whole suite back to rewrite. It
     needed the receiver type it got wrong, the members that DO exist,
     the nested relationship it confused (a top-level object's members
     vs its child object's), and only the ONE test to fix.

This module supplies both, from the project's AST. It knows nothing
about any particular class, member or project - the fixtures in its
self-test are deliberately generic, and the release contract refuses the
module if it names a live ticket's symbols.

Self-test:  python3 member_chain.py --self-test
Pure ASCII. Stdlib only. Zero model calls, zero network.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path

CHAIN_VALIDATOR_VERSION = 2

# Directories never worth parsing for an API surface.
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules",
             ".tox", "build", "dist", ".mypy_cache", ".pytest_cache"}


# ------------------------------------------------------------ surface

def _ann_name(node):
    """The class name an annotation names, if it names one plainly.
    Optional[X] / List[X] deliberately yield None - a container is not
    the member's receiver, and guessing there would produce false
    rejections."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value          # a string forward reference
    return None


def _call_class(node):
    """The class a call constructs, if the callee is a plain name."""
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
    return None


class _ClassInfo(dict):
    pass


DYNAMIC_HOOKS = ("__getattr__", "__getattribute__", "__slots__")


def _new_class(name):
    return {"name": name, "members": set(), "methods": set(),
            "member_types": {}, "returns": {}, "bases": [],
            "dynamic": False}


def api_surface(source_root) -> dict:
    """Every class the project defines, with:

        members       attribute names it exposes (fields, properties,
                      self-assignments in any method, dataclass fields,
                      annotations, and its own methods)
        methods       callables only
        member_types  member -> class name, when the source says so
                      (annotation, or an assignment constructing a known
                      class). This is what makes `a.b.c` decidable.
        returns       method -> class name it returns, from an
                      annotation or a plain `return ClassName(...)`.

    Inheritance is resolved after the walk, so a subclass exposes what it
    inherits and is never reported as missing a member it has."""
    classes: dict = {}
    funcs: dict = {}
    method_names: set = set()
    root = Path(source_root)
    files = ([root] if root.is_file()
             else [p for p in sorted(root.rglob("*.py"))
                   if not any(part in SKIP_DIRS or part.startswith(".")
                              for part in p.relative_to(root).parts)])
    for f in files:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="ignore"))
        except (SyntaxError, OSError, ValueError):
            continue
        # METHODS are collected first and REMEMBERED, because ast.walk
        # visits a class body after the module body: without this a
        # method silently overwrote a module-level function of the same
        # name in the flat `functions` map, and `c = load('p')` inferred
        # the wrong class (2026-08-05 audit).
        method_ids = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef,
                                         ast.AsyncFunctionDef)):
                        method_names.add(item.name)
                        method_ids.add(id(item))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                info = classes.setdefault(node.name, _new_class(node.name))
                info["bases"] = [b.id for b in node.bases
                                 if isinstance(b, ast.Name)]
                _walk_class(node, info)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Identity, not name: ast.walk yields methods too, and a
                # method silently overwrote a module-level function of
                # the same name in the flat `functions` map - so
                # `c = load('p')` inferred Session.load's return type and
                # rejected valid code (2026-08-05 audit).
                if id(node) in method_ids:
                    continue
                rt = _ann_name(node.returns) if node.returns else None
                if rt is None:
                    rt = _returned_class(node)
                if rt:
                    funcs[node.name] = rt
    # resolve inheritance (bounded, cycle-safe)
    for _ in range(4):
        for info in classes.values():
            for base in info["bases"]:
                b = classes.get(base)
                if not b:
                    continue
                info["members"] |= b["members"]
                info["methods"] |= b["methods"]
                info["dynamic"] = info.get("dynamic") or b.get("dynamic")
                for k, v in b["member_types"].items():
                    info["member_types"].setdefault(k, v)
                for k, v in b["returns"].items():
                    info["returns"].setdefault(k, v)
    # A class whose attribute access is DYNAMIC (__getattr__ /
    # __getattribute__) has no knowable member set, and a class with an
    # unresolved base may inherit anything. Neither can be member-checked
    # without inventing false rejections - and a false rejection costs a
    # regeneration, which is the failure this module exists to prevent.
    for info in classes.values():
        if any(b not in classes for b in info["bases"]):
            info["dynamic"] = True
    return {"classes": classes, "functions": funcs}


def _returned_class(fn):
    for n in ast.walk(fn):
        if isinstance(n, ast.Return) and n.value is not None:
            c = _call_class(n.value)
            if c:
                return c
    return None


def _walk_class(node, info):
    for item in node.body:
        if isinstance(item, ast.AnnAssign) and isinstance(item.target,
                                                          ast.Name):
            info["members"].add(item.target.id)
            t = _ann_name(item.annotation)
            if t:
                info["member_types"][item.target.id] = t
        elif isinstance(item, ast.Assign):
            for t in item.targets:
                if isinstance(t, ast.Name):
                    info["members"].add(t.id)
                    c = _call_class(item.value)
                    if c:
                        info["member_types"][t.id] = c
        elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            info["members"].add(item.name)
            info["methods"].add(item.name)
            if item.name in DYNAMIC_HOOKS:
                info["dynamic"] = True
            rt = _ann_name(item.returns) if item.returns else None
            if rt is None:
                rt = _returned_class(item)
            if rt:
                info["returns"][item.name] = rt
            # parameter annotations teach self-assignment types
            params = {}
            for a in list(item.args.args) + list(item.args.kwonlyargs):
                if a.annotation is not None:
                    t = _ann_name(a.annotation)
                    if t:
                        params[a.arg] = t
            for n in ast.walk(item):
                if isinstance(n, ast.AnnAssign) and _is_self_attr(n.target):
                    info["members"].add(n.target.attr)
                    t = _ann_name(n.annotation)
                    if t:
                        info["member_types"][n.target.attr] = t
                elif isinstance(n, ast.Assign):
                    for tgt in n.targets:
                        if _is_self_attr(tgt):
                            info["members"].add(tgt.attr)
                            c = _call_class(n.value)
                            if c:
                                info["member_types"][tgt.attr] = c
                            elif (isinstance(n.value, ast.Name)
                                  and n.value.id in params):
                                info["member_types"][tgt.attr] = \
                                    params[n.value.id]


def _is_self_attr(node):
    return (isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self")


# --------------------------------------------------------- validation

def _chain(node):
    """The dotted chain under an Attribute node, root-first, or None when
    the root is not a plain name (a subscript, a literal, a call on a
    call - all undecidable, and undecidable means silent)."""
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Call):
        c = _call_class(cur)
        if not c:
            return None
        parts.append("(" + c + ")")
        return list(reversed(parts))
    if not isinstance(cur, ast.Name):
        return None
    parts.append(cur.id)
    return list(reversed(parts))


def _subscript_chain(node):
    """A literal string-key chain, root-first.

    JSON reports are commonly asserted as ``payload['summary']['passed']``.
    The attribute-only validator could not see that this is the same object
    graph as ``result.summary.passed`` and froze a member on the wrong nested
    object.  Dynamic keys remain undecidable and therefore silent.
    """
    keys = []
    cur = node
    while isinstance(cur, ast.Subscript):
        sl = cur.slice
        if not (isinstance(sl, ast.Constant) and isinstance(sl.value, str)):
            return None
        keys.append(sl.value)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return None
    return [cur.id] + list(reversed(keys))


def _render_subscript_chain(parts):
    if not parts:
        return ""
    return parts[0] + "".join("[{!r}]".format(k) for k in parts[1:])


class _TypeEnv:
    """Local variable -> class name, learned from assignments in the
    order they appear. Deliberately flow-insensitive and conservative:
    an unknown type is never guessed, it is simply not checked."""

    def __init__(self, surface):
        self.surface = surface
        self.vars: dict = {}

    def learn(self, node):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target,
                                                          ast.Name):
            t = _ann_name(node.annotation)
            if t in self.surface["classes"]:
                self.vars[node.target.id] = t
            return
        if not isinstance(node, ast.Assign):
            return
        t = self.resolve_value(node.value)
        for tgt in node.targets:
            if isinstance(tgt, ast.Name):
                if t:
                    self.vars[tgt.id] = t
                else:
                    self.vars.pop(tgt.id, None)

    def resolve_value(self, value):
        c = _call_class(value)
        if c and c in self.surface["classes"]:
            return c
        if c and c in self.surface["functions"]:
            return self.surface["functions"][c]
        if isinstance(value, ast.Call) and isinstance(value.func,
                                                      ast.Attribute):
            recv = self.resolve_expr(value.func.value)
            if recv:
                info = self.surface["classes"].get(recv) or {}
                return (info.get("returns") or {}).get(value.func.attr)
        if isinstance(value, ast.Attribute):
            return self.resolve_expr(value)
        if isinstance(value, ast.Name):
            return self.vars.get(value.id)
        return None

    def resolve_expr(self, node):
        """The class an expression evaluates to, or None."""
        if isinstance(node, ast.Name):
            return self.vars.get(node.id)
        if isinstance(node, ast.Call):
            return self.resolve_value(node)
        if isinstance(node, ast.Attribute):
            base = self.resolve_expr(node.value)
            if not base:
                return None
            info = self.surface["classes"].get(base) or {}
            t = (info.get("member_types") or {}).get(node.attr)
            if t:
                return t
            return (info.get("returns") or {}).get(node.attr)
        return None


def validate_source(code: str, surface: dict, filename: str = "<test>",
                    ) -> list:
    """Every invalid attribute access this source makes on a class the
    project DEFINES. Each problem carries what a correction needs:

        chain          the exact expression that is wrong
        receiver       the inferred receiver class
        member         the member that does not exist
        valid_members  what the class really exposes
        nearest        the closest real member, when one is close
        borrowed_from  the class that DOES have that member, when
                       exactly one visible class does - the confusion
                       that produced the live failure was a member
                       borrowed from a neighbouring class
        line           where

    A receiver the source cannot decide is NEVER reported: a false
    rejection costs a regeneration, which is the failure this exists to
    prevent."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [{"kind": "syntax", "chain": "", "receiver": None,
                 "member": None, "valid_members": [], "nearest": None,
                 "borrowed_from": None, "line": e.lineno or 0,
                 "detail": "the candidate does not parse: {}".format(e.msg)}]
    classes = surface.get("classes") or {}
    problems = []
    seen = set()

    # SCOPES ARE SEPARATE and bindings are FLOW-SENSITIVE. The first
    # version built one environment per function by walking the whole
    # subtree first, which meant (a) two test functions reusing the name
    # `result` for different classes poisoned each other, and (b) a
    # rebinding later in a function retroactively invalidated the valid
    # access before it. Both produced FALSE REJECTIONS of correct tests -
    # the exact reject/correct/regenerate loop this module exists to
    # prevent (2026-08-05 adversarial audit).
    def _scope_bodies(node):
        """Every statement list that shares one local namespace."""
        if isinstance(node, ast.Module):
            return [node.body]
        return [node.body]

    def _walk_scope(stmts, env):
        """Statements in order: learn, then check what they read. A
        variable whose type becomes unknown is DROPPED, never kept."""
        for stmt in stmts:
            # nested scopes get their own environment
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                _walk_scope(stmt.body, _TypeEnv(surface))
                continue
            # check the reads this statement makes FIRST (with the types
            # known so far), then apply its own binding
            for node in ast.walk(stmt):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                    continue
                _check_attr(node, env)
                _check_subscript(node)
            env.learn(stmt)
            # branches: a body may rebind, and after a branch the type is
            # only knowable if every path agrees - so walk each branch in
            # its own copy and drop anything they disagree about.
            for field in ("body", "orelse", "finalbody"):
                inner = getattr(stmt, field, None)
                if isinstance(inner, list) and inner and not isinstance(
                        stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
                    branch = _TypeEnv(surface)
                    branch.vars = dict(env.vars)
                    _walk_scope(inner, branch)
                    for k, v in branch.vars.items():
                        if env.vars.get(k) != v:
                            env.vars.pop(k, None)
            for h in getattr(stmt, "handlers", []) or []:
                branch = _TypeEnv(surface)
                branch.vars = dict(env.vars)
                _walk_scope(h.body, branch)

    def _check_attr(node, env):
        if not isinstance(node, ast.Attribute):
            return
        recv = env.resolve_expr(node.value)
        if not recv or recv not in classes:
            return
        info = classes[recv]
        if info.get("dynamic"):
            return          # no knowable member set - never reject
        if node.attr in info["members"]:
            return
        chain = _chain(node)
        key = (recv, node.attr, node.lineno)
        if key in seen:
            return
        seen.add(key)
        problems.append({
                "kind": "member",
                "chain": ".".join(chain) if chain else node.attr,
                "receiver": recv,
                "member": node.attr,
                "valid_members": sorted(info["members"]),
                "nearest": _nearest(node.attr, info["members"]),
                "borrowed_from": _borrowed(node.attr, recv, classes),
                "relationship": _relationship(recv, classes),
                "line": node.lineno,
                "file": filename,
                "detail": ("{} has no member {!r}".format(recv, node.attr)),
            })

    def _check_subscript(node):
        """Validate a serialized dataclass/member chain conservatively.

        The root dictionary has no Python type after json.loads(), so infer
        it only when the first literal key belongs to a project class and
        leads through a declared member type.  If every possible owner
        yields the same missing nested member, the defect is decidable;
        otherwise stay silent rather than reject a valid arbitrary mapping.
        """
        if not isinstance(node, ast.Subscript):
            return
        chain = _subscript_chain(node)
        if not chain or len(chain) < 3:
            return
        keys = chain[1:]
        candidates = []
        for cname, info in classes.items():
            if info.get("dynamic") or keys[0] not in info.get("members", set()):
                continue
            if (info.get("member_types") or {}).get(keys[0]) in classes:
                candidates.append(cname)
        findings = []
        for owner in candidates:
            recv = owner
            for key in keys:
                info = classes.get(recv) or {}
                if info.get("dynamic"):
                    break
                if key not in info.get("members", set()):
                    findings.append((recv, key, info))
                    break
                nxt = (info.get("member_types") or {}).get(key)
                if nxt not in classes:
                    break
                recv = nxt
        unique = {(recv, member) for recv, member, _ in findings}
        if len(unique) != 1:
            return
        recv, member = next(iter(unique))
        info = next(i for r, m, i in findings if r == recv and m == member)
        key = (recv, member, node.lineno)
        if key in seen:
            return
        seen.add(key)
        problems.append({
            "kind": "member",
            "chain": _render_subscript_chain(chain),
            "receiver": recv,
            "member": member,
            "valid_members": sorted(info.get("members") or []),
            "nearest": _nearest(member, info.get("members") or []),
            "borrowed_from": _borrowed(member, recv, classes),
            "relationship": _relationship(recv, classes),
            "line": node.lineno,
            "file": filename,
            "detail": "{} has no member {!r}".format(recv, member),
        })

    _walk_scope(tree.body, _TypeEnv(surface))
    return problems


def _nearest(name, members):
    import difflib
    m = difflib.get_close_matches(name, sorted(members), n=1, cutoff=0.72)
    return m[0] if m else None


def _borrowed(member, receiver, classes):
    owners = [c for c, i in classes.items()
              if c != receiver and member in i["members"]]
    return owners[0] if len(owners) == 1 else None


def _relationship(receiver, classes):
    """The object graph around this receiver, BOTH ways:

      - what it REACHES (Outcome.totals -> Totals), which is the
        confusion that produced the live failure: a member that belongs
        to the child object, asserted on the parent;
      - what REACHES it, so a correction knows where the receiver came
        from.

    Both directions matter and both are cheap; a correction that only
    knew one of them still had to guess."""
    out = []
    own = classes.get(receiver) or {}
    for member, t in (own.get("member_types") or {}).items():
        out.append("{}.{} -> {}".format(receiver, member, t))
    for meth, t in (own.get("returns") or {}).items():
        if meth != "__init__":
            out.append("{}.{}() -> {}".format(receiver, meth, t))
    for c, i in classes.items():
        if c == receiver:
            continue
        for member, t in (i.get("member_types") or {}).items():
            if t == receiver:
                out.append("{}.{} -> {}".format(c, member, receiver))
        for meth, t in (i.get("returns") or {}).items():
            if t == receiver and meth != "__init__":
                out.append("{}.{}() -> {}".format(c, meth, receiver))
    return sorted(set(out))


# ------------------------------------------------------- fingerprinting

def semantic_fingerprint(problems) -> str:
    """One id for "this semantic defect". Normalized across variable
    names, formatting, line numbers and test ids, so a regenerated suite
    that repeats the same mistake is RECOGNISED as a repeat instead of
    being paid for again."""
    key = sorted({"{}::{}".format(p.get("receiver"), p.get("member"))
                  for p in (problems or [])
                  if p.get("kind") == "member"})
    if not key:
        key = sorted({str(p.get("kind")) for p in (problems or [])})
    return hashlib.sha256("|".join(key).encode("utf-8")).hexdigest()[:16]


def correction_prompt(problem: dict, candidate_code: str,
                      criterion: str | None = None,
                      baseline: str | None = None) -> str:
    """Everything a FOCUSED correction needs, and nothing else. The live
    correction failed three times because it was handed prose and the
    whole suite; this hands back one test, the exact chain, the inferred
    receiver, the members that exist, the nested relationship that was
    confused, the criterion it must cover and the classification it must
    keep."""
    lines = [
        "ONE test is invalid. Fix ONLY this test. Do not rewrite the "
        "others - they are already accepted.",
        "",
        "INVALID EXPRESSION : {}".format(problem.get("chain")),
        "INFERRED RECEIVER  : {}".format(problem.get("receiver")),
        "MEMBER THAT DOES NOT EXIST: {}".format(problem.get("member")),
        "MEMBERS {} REALLY HAS: {}".format(
            problem.get("receiver"),
            ", ".join(problem.get("valid_members") or []) or "(none)"),
    ]
    if problem.get("nearest"):
        lines.append("NEAREST REAL MEMBER: {}".format(problem["nearest"]))
    if problem.get("borrowed_from"):
        lines.append(
            "NOTE: {!r} is a member of {} - you appear to have used one "
            "class's member on another.".format(
                problem.get("member"), problem["borrowed_from"]))
    if problem.get("relationship"):
        lines.append("HOW {} IS REACHED: {}".format(
            problem.get("receiver"), "; ".join(problem["relationship"])))
    if criterion:
        lines.append("THIS TEST COVERS   : {}".format(criterion))
    if baseline:
        lines.append("REQUIRED BASELINE CLASSIFICATION: {} (it must stay "
                     "exactly this)".format(baseline))
    lines += [
        "",
        "THE REJECTED TEST:",
        "-----",
        candidate_code or "(empty)",
        "-----",
        "",
        "Return the corrected test only. Assert members that exist. If "
        "this ticket's criteria ADD the member you wanted, say which "
        "criterion in the docstring instead of asserting it on a class "
        "that does not have it yet.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    import tempfile
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    # Deliberately generic fixtures: nothing here names any live ticket's
    # project, class or member (the release contract enforces this).
    src = '''
class Totals:
    def __init__(self, left_rows: int, right_rows: int):
        self.left_rows = left_rows
        self.right_rows = right_rows
        self.differing = 0

    def ratio(self) -> float:
        return 0.0


class Outcome:
    def __init__(self, totals: Totals, label: str):
        self.totals = totals
        self.label = label
        self.ok = True

    def totals_for(self, name) -> Totals:
        return self.totals


class DetailedOutcome(Outcome):
    def __init__(self, totals, label, notes):
        super().__init__(totals, label)
        self.notes = notes


def build_outcome(path) -> Outcome:
    return Outcome(Totals(0, 0), "x")
'''
    td = Path(tempfile.mkdtemp())
    (td / "pkg").mkdir()
    (td / "pkg" / "core.py").write_text(src, encoding="utf-8")
    s = api_surface(td)

    check("classes are discovered from the project's own source",
          set(s["classes"]) == {"Totals", "Outcome", "DetailedOutcome"})
    check("members include fields and methods",
          {"left_rows", "right_rows", "differing", "ratio"}
          <= s["classes"]["Totals"]["members"])
    check("nested relationships are learned (Outcome.totals -> Totals)",
          s["classes"]["Outcome"]["member_types"]["totals"] == "Totals")
    check("method return types are learned",
          s["classes"]["Outcome"]["returns"]["totals_for"] == "Totals"
          and s["functions"]["build_outcome"] == "Outcome")
    check("inheritance is resolved - a subclass is not missing what it "
          "inherits",
          {"totals", "label", "ok", "notes"}
          <= s["classes"]["DetailedOutcome"]["members"])

    # THE LIVE SHAPE, generically: a member borrowed from the child
    # object and asserted on the parent.
    bad = ("def test_x():\n"
           "    outcome = build_outcome('p')\n"
           "    assert outcome.differing == 0\n")
    p = validate_source(bad, s)
    check("an invalid member on an inferred receiver is caught STATICALLY",
          len(p) == 1 and p[0]["receiver"] == "Outcome"
          and p[0]["member"] == "differing")
    check("the problem names the members that DO exist",
          "label" in p[0]["valid_members"]
          and "differing" not in p[0]["valid_members"])
    check("a member borrowed from another visible class is identified",
          p[0]["borrowed_from"] == "Totals")
    check("the nested relationship that was confused is spelled out",
          "Outcome.totals -> Totals" in p[0]["relationship"])

    good = ("def test_x():\n"
            "    outcome = build_outcome('p')\n"
            "    assert outcome.totals.differing == 0\n"
            "    assert outcome.label == 'x'\n")
    check("the CORRECT chain through the nested object passes",
          validate_source(good, s) == [])
    bad_json = ("def test_json():\n"
                "    payload = load_json('result.json')\n"
                "    assert payload['totals']['ok'] is True\n")
    pj = validate_source(bad_json, s)
    check("a serialized dictionary chain is checked against the same "
          "nested public API as attribute access",
          len(pj) == 1 and pj[0]["receiver"] == "Totals"
          and pj[0]["member"] == "ok"
          and pj[0]["chain"] == "payload['totals']['ok']")
    good_json = ("def test_json():\n"
                 "    payload = load_json('result.json')\n"
                 "    assert payload['totals']['differing'] == 0\n")
    check("a valid serialized dictionary chain stays valid",
          validate_source(good_json, s) == [])
    check("dynamic dictionary keys remain undecidable and are never rejected",
          validate_source("def t(payload, key):\n"
                          "    assert payload['totals'][key]\n", s) == [])
    check("top-level members stay separate from the child's members",
          validate_source("def t():\n"
                          "    o = build_outcome('p')\n"
                          "    assert o.totals.label\n", s))

    chained = ("def test_y():\n"
               "    o = Outcome(Totals(1, 2), 'l')\n"
               "    t = o.totals_for('a')\n"
               "    assert t.nope == 1\n")
    pc = validate_source(chained, s)
    check("a receiver reached through a method's RETURN type is inferred",
          len(pc) == 1 and pc[0]["receiver"] == "Totals"
          and pc[0]["member"] == "nope")
    check("a direct constructor call is a known receiver",
          validate_source("def t():\n    assert Totals(1, 2).missing\n",
                          s)[0]["receiver"] == "Totals")
    check("a near-miss suggests the real member",
          validate_source("def t():\n"
                          "    x = Totals(1, 2)\n"
                          "    assert x.left_row == 1\n",
                          s)[0]["nearest"] == "left_rows")

    # SILENCE where the source cannot decide - a false rejection costs a
    # regeneration, which is the failure this module exists to prevent.
    check("an unknown receiver is never reported",
          validate_source("def t(anything):\n"
                          "    assert anything.whatever == 1\n", s) == [])
    check("a stdlib/third-party receiver is never reported",
          validate_source("import json\n"
                          "def t():\n"
                          "    assert json.dumps({})\n", s) == [])
    check("a reassigned variable loses its type rather than keeping a "
          "stale one",
          validate_source("def t(x):\n"
                          "    o = Totals(1, 2)\n"
                          "    o = x\n"
                          "    assert o.anything\n", s) == [])
    check("an unparseable candidate is a typed problem, not a crash",
          validate_source("def t(:\n", s)[0]["kind"] == "syntax")

    # Fingerprints: the same defect, differently written, is ONE defect.
    f1 = semantic_fingerprint(validate_source(bad, s))
    f2 = semantic_fingerprint(validate_source(
        "def test_completely_renamed():\n"
        "    result_object = build_outcome('other/path')\n"
        "    # a comment that was not there before\n"
        "    assert result_object.differing == 0\n", s))
    check("the same semantic defect fingerprints identically across "
          "variable and formatting changes", f1 == f2)
    f3 = semantic_fingerprint(validate_source(
        "def t():\n    x = Totals(1, 2)\n    assert x.other\n", s))
    check("a different defect gets a different fingerprint", f1 != f3)
    check("a clean suite has no defect fingerprint collision with a "
          "broken one",
          semantic_fingerprint(validate_source(good, s)) != f1)

    # The correction prompt carries everything a focused fix needs.
    cp = correction_prompt(p[0], bad, criterion="AC2",
                           baseline="preservation")
    for needed in ("INVALID EXPRESSION", "INFERRED RECEIVER",
                   "MEMBERS Outcome REALLY HAS", "Outcome.totals -> Totals",
                   "AC2", "preservation", "Fix ONLY this test"):
        ok.append(("the correction prompt carries {!r}".format(needed),
                   needed in cp))
    check("the correction prompt includes the complete rejected test",
          "outcome.differing" in cp)

    # AUDIT 2026-08-05: FALSE REJECTIONS of valid code. The first version
    # built one environment per function by walking the whole subtree
    # first, so bindings leaked across functions and a later rebinding
    # retroactively invalidated the valid access before it. A false
    # rejection costs a correction and then a regeneration - the exact
    # loop this module exists to prevent, fired on CORRECT tests.
    check("AUDIT: two functions reusing one variable name for different "
          "classes are BOTH valid",
          validate_source("def test_one():\n"
                          "    r = build_outcome('p')\n"
                          "    assert r.label\n\n\n"
                          "def test_two():\n"
                          "    r = Totals(1, 2)\n"
                          "    assert r.left_rows == 1\n", s) == [])
    check("AUDIT: a REBINDING does not invalidate the earlier valid use",
          validate_source("def t():\n"
                          "    o = build_outcome('p')\n"
                          "    assert o.label\n"
                          "    o = Totals(1, 2)\n"
                          "    assert o.left_rows == 1\n", s) == [])
    check("AUDIT: a conditional rebinding leaves the type UNKNOWN rather "
          "than wrong",
          validate_source("def t(flag):\n"
                          "    o = build_outcome('p')\n"
                          "    if flag:\n"
                          "        o = Totals(1, 2)\n"
                          "    assert o.anything_at_all\n", s) == [])
    dyn_src = ('class Bag:\n'
               '    def __getattr__(self, n):\n        return 1\n\n\n'
               'def make() -> Bag:\n    return Bag()\n')
    (td / "pkg" / "dyn.py").write_text(dyn_src, encoding="utf-8")
    s_dyn = api_surface(td)
    check("AUDIT: a class with __getattr__ has no knowable member set and "
          "is never member-checked",
          validate_source("def t():\n    b = make()\n"
                          "    assert b.whatever\n", s_dyn) == [])
    shadow = ('class Cfg:\n'
              '    def __init__(self):\n        self.retries = 3\n\n\n'
              'class Rep:\n'
              '    def __init__(self):\n        self.rows = 0\n\n\n'
              'def fetch(p) -> Cfg:\n    return Cfg()\n\n\n'
              'class Session:\n'
              '    def fetch(self) -> Rep:\n        return Rep()\n')
    td2 = Path(tempfile.mkdtemp())
    (td2 / "m.py").write_text(shadow, encoding="utf-8")
    s_sh = api_surface(td2)
    check("AUDIT: a METHOD never shadows a module-level function of the "
          "same name",
          validate_source("def t():\n    c = fetch('p')\n"
                          "    assert c.retries == 3\n", s_sh) == [])
    unk = ('class Wild(SomethingExternal):\n'
           '    def __init__(self):\n        self.own = 1\n\n\n'
           'def wild() -> Wild:\n    return Wild()\n')
    td3 = Path(tempfile.mkdtemp())
    (td3 / "w.py").write_text(unk, encoding="utf-8")
    check("AUDIT: a class with an UNRESOLVED base may inherit anything, "
          "so it is never member-checked",
          validate_source("def t():\n    w = wild()\n"
                          "    assert w.inherited_thing\n",
                          api_surface(td3)) == [])
    check("...but the real defect is still caught after all of that",
          len(validate_source(bad, s)) == 1)

    # ===== TASK 20 / WORKSTREAM E SECTION 4 ==========================
    # "Existing API member validation catches cross-class vocabulary
    # confusion." test_spec's freeze-time audit pins the whole-suite
    # behaviour; this pins it at the seam that decides it, STATICALLY -
    # the live defect was decidable from the project's own source before
    # a single test ran, and paying three generations to rediscover it
    # at runtime is the failure this module exists to end.
    t20_borrowed = ("def t():\n"
                    "    o = build_outcome('p')\n"
                    "    assert o.differing == 1\n")
    t20_probs = [p for p in validate_source(t20_borrowed, s)
                 if p.get("kind") == "member"]
    check("[T20-E4-e] cross-class vocabulary confusion is caught "
          "STATICALLY - a member that is real on a DIFFERENT class is "
          "refused against the receiver that does not have it",
          len(t20_probs) == 1
          and t20_probs[0]["receiver"] == "Outcome"
          and t20_probs[0]["member"] == "differing")
    check("[T20-E4-e] ...and the finding NAMES the class the name was "
          "borrowed from, so the correction knows which vocabulary was "
          "crossed instead of guessing again",
          t20_probs[0].get("borrowed_from") == "Totals"
          and "differing" not in (t20_probs[0].get("valid_members") or []))
    check("[T20-E4-e] ...and it offers the receiver's REAL members, so "
          "the correction is targeted rather than a rewrite",
          sorted(t20_probs[0]["valid_members"])
          == sorted(s["classes"]["Outcome"]["members"]))
    t20_new = ("def t():\n"
               "    o = build_outcome('p')\n"
               "    assert o.brand_new_field == 1\n")
    check("[T20-E4-e] ...and a genuinely NEW name this ticket is adding "
          "is not the same thing as a crossed vocabulary - it carries no "
          "borrowed_from, so a feature-red test is never blocked as a "
          "confusion",
          all(not p.get("borrowed_from")
              for p in validate_source(t20_new, s)
              if p.get("kind") == "member"))
    t20_fp = semantic_fingerprint(t20_probs)
    check("[T20-E4-e] ...and the defect has a STABLE identity, so a "
          "regeneration that repeats it is recognised instead of being "
          "paid for twice",
          t20_fp
          and t20_fp == semantic_fingerprint(
              [p for p in validate_source(
                  "def t():\n"
                  "    renamed = build_outcome('other')\n"
                  "    assert renamed.differing == 99\n", s)
               if p.get("kind") == "member"]))

    check("the module declares its contract version",
          isinstance(CHAIN_VALIDATOR_VERSION, int)
          and CHAIN_VALIDATOR_VERSION >= 1)
    check("an empty project surface never rejects anything",
          validate_source(bad, {"classes": {}, "functions": {}}) == [])

    passed = sum(1 for _, c in ok if c)
    for name, cond in ok:
        print("  [{}] {}".format("PASS" if cond else "FAIL", name))
    print("\n  {}/{} passed".format(passed, len(ok)))
    return 0 if passed == len(ok) else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Docket generic receiver/member-chain validator")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--surface", default=None,
                    help="print the API surface of a source tree")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    if args.surface:
        s = api_surface(args.surface)
        print(json.dumps({c: sorted(i["members"])
                          for c, i in s["classes"].items()}, indent=2))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
