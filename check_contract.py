#!/usr/bin/env python3
"""
Docket - is the CONTRACT edit actually in the file being run?

--doctor showed every table PARTIAL with the OLD column names (issue->ticket,
at->ts). That means the file on disk still has the default CONTRACT. This prints
what the file ACTUALLY contains, so we can see whether the edit saved and whether
you are running the file you think you are.

    python check_contract.py

Run it from the SAME folder, the SAME way, you run payload_builder.py.
"""
import os
import sys

# import the exact module that --doctor would import
sys.path.insert(0, os.getcwd())
try:
    import payload_builder as pb
except Exception as e:
    print("could not import payload_builder from this folder:", e)
    sys.exit(1)

print("running file:", os.path.abspath(pb.__file__))
print()
runs = pb.CONTRACT["runs"]["columns"]
gates = pb.CONTRACT["gates"]["columns"]

want = {
    "runs.issue": (runs.get("issue"), "ticket_id"),
    "runs.reason": (runs.get("reason"), "failure_class"),
    "runs.summary": (runs.get("summary"), None),
    "runs.stopped_at": (runs.get("stopped_at"), None),
    "gates.issue": (gates.get("issue"), "ticket_id"),
    "gates.name": (gates.get("name"), "gate_name"),
    "gates.result": (gates.get("result"), "outcome"),
    "gates.at": (gates.get("at"), "created_at"),
}

ok = True
for k, (got, expect) in want.items():
    good = got == expect
    ok = ok and good
    mark = "ok " if good else "NOT EDITED"
    print(f"  [{mark}] {k:18} is {got!r:14} (want {expect!r})")

print()
if ok:
    print("The CONTRACT edit IS in this file. If --doctor still shows PARTIAL,")
    print("you are running a DIFFERENT copy - check the path printed above.")
else:
    print("The edit did NOT save to this file. The values above still show the")
    print("old defaults. Re-paste the CONTRACT block, SAVE, and run this again.")
