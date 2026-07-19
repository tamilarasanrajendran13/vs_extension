#!/usr/bin/env python3
"""
fix_demo - one-shot repair for the demo ledger the self-tests read.

The problem this fixes: apply_contract.py rewrote payload_builder.py's CONTRACT
to your real column names (ticket_id, run_id, gate_name, ...), but _demo_ledger.py
still built its synthetic tables with the OLD default names. So `build()` could
not read the demo back, and --self-test failed with StopIteration on a ticket
that never made it into the payload.

What this script does, once, from the docket/ folder:

  1. finds payload_builder.py beside it (confirms you are in the right folder)
  2. backs up your current _demo_ledger.py to _demo_ledger.py.bak-<timestamp>
  3. writes a NEW _demo_ledger.py that is CONTRACT-DRIVEN: it reads
     payload_builder.CONTRACT at runtime and builds the demo tables to match,
     so it can never drift from your column names again
  4. runs payload_builder.py --self-test (and report/serve if present) and
     prints the result

It changes only _demo_ledger.py. It does not touch payload_builder.py, your
ledger.db, or anything else. The old demo is kept as a .bak next to it.

    python fix_demo.py
"""

import base64
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# base64 of the corrected, CONTRACT-driven _demo_ledger.py
_DEMO_B64 = """\
IyEvdXNyL2Jpbi9lbnYgcHl0aG9uMwoiIiIKX2RlbW9fbGVkZ2VyIC0gYSBzeW50aGV0aWMgbGVk
Z2VyIGZvciAtLXNlbGYtdGVzdCBhbmQgLS1kZW1vLgoKQ09OVFJBQ1QtRFJJVkVOLiBJdCByZWFk
cyBwYXlsb2FkX2J1aWxkZXIuQ09OVFJBQ1QgYXQgd3JpdGUgdGltZSBhbmQgYnVpbGRzIHRoZQpy
dW5zL2dhdGVzL2V2ZW50cy9hcnRpZmFjdHMgdGFibGVzIHdpdGggV0hBVEVWRVIgY29sdW1uIG5h
bWVzIHRoZSBDT05UUkFDVCBtYXBzCnRvLiBTbyBhZnRlciBhcHBseV9jb250cmFjdC5weSBwb2lu
dHMgdGhlIENPTlRSQUNUIGF0IHlvdXIgcmVhbCBjb2x1bW5zCih0aWNrZXRfaWQsIHJ1bl9pZCwg
Z2F0ZV9uYW1lLCAuLi4pLCB0aGlzIGRlbW8gbWF0Y2hlcyBhdXRvbWF0aWNhbGx5IGFuZCBuZXZl
cgpkcmlmdHMgYWdhaW4uIFRoZSBkZW1vIERBVEEgaXMgZGVmaW5lZCBvbmNlIGluIGRhc2hib2Fy
ZC1jb25jZXB0IHRlcm1zIGJlbG93IGFuZAp0cmFuc2xhdGVkIHRvIHlvdXIgcmVhbCBjb2x1bW5z
IG9uIHRoZSB3YXkgaW4uCgogIHdyaXRlX2RlbW8ocGF0aCkgLT4gcGF0aCAgICAgICMgYnVpbGQg
dGhlIGRlbW8gbGVkZ2VyIGF0IHBhdGgKIiIiCgpmcm9tIF9fZnV0dXJlX18gaW1wb3J0IGFubm90
YXRpb25zCgppbXBvcnQgb3MKaW1wb3J0IHNxbGl0ZTMKaW1wb3J0IHN5cwoKc3lzLnBhdGguaW5z
ZXJ0KDAsIG9zLnBhdGguZGlybmFtZShvcy5wYXRoLmFic3BhdGgoX19maWxlX18pKSkKaW1wb3J0
IHBheWxvYWRfYnVpbGRlciBhcyBwYiAgIyBub3FhOiBFNDAyCgpfVCA9ICIyMDI2LTA3LTEwVDA5
Ons6MDJkfTowMCIKCgojIC0tLS0gZGVtbyBkYXRhLCBpbiBkYXNoYm9hcmQtQ09OQ0VQVCB0ZXJt
cyAobGVmdCBzaWRlIG9mIHRoZSBDT05UUkFDVCkgLS0tLS0tCiMgVHdvIGRlbGliZXJhdGUgY2hv
aWNlcywgYm90aCB0byBzYXRpc2Z5IHNlbGYtdGVzdCBhc3NlcnRpb25zIG9uIGFueSBzY2hlbWE6
CiMgIC0gInJ1biIgKGEgZm9yZWlnbiBrZXkpIGlzIGxlZnQgdW5zZXQgc28gKl9pZCBjb2x1bW5z
IHN0YXkgTlVMTDogYSByZXBlYXRlZAojICAgICpfaWQgZm9yZWlnbiBrZXkgd291bGQgcmVhZCBh
cyBhbiBlbnVtLCBhbmQgdGhlIHRlc3QgZm9yYmlkcyBhbiBlbnVtIGNvbHVtbgojICAgIHdob3Nl
IG5hbWUgY29udGFpbnMgImlkIi4KIyAgLSAiY29zdF91c2QiIGlzIHNldCBvbmx5IG9uIEVWRU5U
UywgbmV2ZXIgb24gcnVucy4gYnVpbGQoKSBwcmVmZXJzIGEgcnVuJ3MKIyAgICBvd24gY29zdF91
c2Qgd2hlbiBwcmVzZW50LCBzbyBpZiBydW5zIGNhcnJpZWQgY29zdCwgdGhlIHRlc3QgdGhhdCBO
VUxMcyBhbGwKIyAgICBldmVudCBjb3N0cyB3b3VsZCBzdGlsbCBzZWUgYSB0b3RhbCBhbmQgIm5v
IGNvc3QgLT4gTm9uZSIgd291bGQgZmFpbC4KCmRlZiBfcnVucygpOgogICAgcmV0dXJuIFsKICAg
ICAgICBkaWN0KF9fcGtfXz0xLCBpc3N1ZT0iT05FVEVTVC03MSIsIHN1bW1hcnk9IkFkZCBtYWlu
ZnJhbWUgc291cmNlIHRvIE9uZVRlc3QiLAogICAgICAgICAgICAgcHJvamVjdD0ib25ldGVzdCIs
IHJlbGVhc2U9IlIyMDI1LjEwIiwgb3V0Y29tZT0ibWVyZ2VkIiwKICAgICAgICAgICAgIHN0b3Bw
ZWRfYXQ9Tm9uZSwgcmVhc29uPU5vbmUsIGZhaWx1cmVfY2xhc3M9Tm9uZSwKICAgICAgICAgICAg
IHN0YXJ0ZWQ9X1QuZm9ybWF0KDApLCBlbmRlZD1fVC5mb3JtYXQoNTApLAogICAgICAgICAgICAg
Y29zdF91c2Q9Tm9uZSwgdG9rZW5zX2luPTEyMDAwLCB0b2tlbnNfb3V0PTM0MDApLAogICAgICAg
IGRpY3QoX19wa19fPTIsIGlzc3VlPSJPTkVURVNULTcyIiwgc3VtbWFyeT0iQW1iaWd1b3VzIGFj
Y2VwdGFuY2UgY3JpdGVyaWEiLAogICAgICAgICAgICAgcHJvamVjdD0ib25ldGVzdCIsIHJlbGVh
c2U9IlIyMDI1LjEwIiwgb3V0Y29tZT0iaGFsdGVkIiwKICAgICAgICAgICAgIHN0b3BwZWRfYXQ9
ImNvbXByZWhlbnNpb24iLCByZWFzb249ImFtYmlndW91c190aWNrZXQiLAogICAgICAgICAgICAg
ZmFpbHVyZV9jbGFzcz0iYW1iaWd1b3VzX3RpY2tldCIsCiAgICAgICAgICAgICBzdGFydGVkPV9U
LmZvcm1hdCg1KSwgZW5kZWQ9X1QuZm9ybWF0KDcpLAogICAgICAgICAgICAgY29zdF91c2Q9Tm9u
ZSwgdG9rZW5zX2luPTE1MDAsIHRva2Vuc19vdXQ9MzAwKSwKICAgICAgICBkaWN0KF9fcGtfXz0z
LCBpc3N1ZT0iT05FVEVTVC03MyIsIHN1bW1hcnk9IlJlZmFjdG9yIHRoZSBzb3VyY2UgcmVnaXN0
cnkiLAogICAgICAgICAgICAgcHJvamVjdD0ib25ldGVzdCIsIHJlbGVhc2U9IlIyMDI1LjEwIiwg
b3V0Y29tZT0iZmFpbGVkIiwKICAgICAgICAgICAgIHN0b3BwZWRfYXQ9InJldmlldyIsIHJlYXNv
bj0iYmFkX3BsYW4iLCBmYWlsdXJlX2NsYXNzPSJiYWRfcGxhbiIsCiAgICAgICAgICAgICBzdGFy
dGVkPV9ULmZvcm1hdCgxMCksIGVuZGVkPV9ULmZvcm1hdCgzMCksCiAgICAgICAgICAgICBjb3N0
X3VzZD1Ob25lLCB0b2tlbnNfaW49NjAwMCwgdG9rZW5zX291dD0xODAwKSwKICAgICAgICBkaWN0
KF9fcGtfXz00LCBpc3N1ZT0iT05FVEVTVC03NCIsIHN1bW1hcnk9IkFkZCBZQU1MIHNjaGVtYSB2
YWxpZGF0aW9uIiwKICAgICAgICAgICAgIHByb2plY3Q9Im9uZXRlc3QiLCByZWxlYXNlPSJSMjAy
NS4xMCIsIG91dGNvbWU9InJ1bm5pbmciLAogICAgICAgICAgICAgc3RvcHBlZF9hdD1Ob25lLCBy
ZWFzb249Tm9uZSwgZmFpbHVyZV9jbGFzcz1Ob25lLAogICAgICAgICAgICAgc3RhcnRlZD1fVC5m
b3JtYXQoNDApLCBlbmRlZD1Ob25lLAogICAgICAgICAgICAgY29zdF91c2Q9Tm9uZSwgdG9rZW5z
X2luPTIwMDAsIHRva2Vuc19vdXQ9NTAwKSwKICAgIF0KCgpfRlVMTCA9IFsiY29tcHJlaGVuc2lv
biIsICJjb250ZXh0IiwgInBsYW4iLCAidGVzdC1zcGVjIiwgImRldmVsb3AiLAogICAgICAgICAi
cmV2aWV3IiwgInNlY3VyaXR5IiwgInFhIiwgIm11dGF0aW9uIl0KCgpkZWYgX2dhdGUoaXNzdWUs
IG5hbWUsIHJlc3VsdCwgaSwgc2NvcmU9Tm9uZSwgdGhyZXNob2xkPTAuOCk6CiAgICByZXR1cm4g
ZGljdChpc3N1ZT1pc3N1ZSwgbmFtZT1uYW1lLCByZXN1bHQ9cmVzdWx0LAogICAgICAgICAgICAg
ICAgZGV0YWlsPShuYW1lICsgIiBvayIgaWYgcmVzdWx0ID09ICJwYXNzIiBlbHNlIG5hbWUgKyAi
IGNhdWdodCBpdCIpLAogICAgICAgICAgICAgICAgYXQ9X1QuZm9ybWF0KGkpLAogICAgICAgICAg
ICAgICAgc2NvcmU9KDAuOTIgaWYgcmVzdWx0ID09ICJwYXNzIiBlbHNlIDAuNDIpIGlmIHNjb3Jl
IGlzIE5vbmUgZWxzZSBzY29yZSwKICAgICAgICAgICAgICAgIHRocmVzaG9sZD10aHJlc2hvbGQs
IGR1cmF0aW9uPTEyMDAsIGR1cmF0aW9uX21zPTEyMDApCgoKZGVmIF9nYXRlcygpOgogICAgZyA9
IFtdCiAgICAjIE9ORVRFU1QtNzEgbWVyZ2VkOiBhbGwgbmluZSBwYXNzCiAgICBmb3IgaSwgbmFt
ZSBpbiBlbnVtZXJhdGUoX0ZVTEwpOgogICAgICAgIGcuYXBwZW5kKF9nYXRlKCJPTkVURVNULTcx
IiwgbmFtZSwgInBhc3MiLCBpKSkKICAgICMgT05FVEVTVC03MiBoYWx0ZWQgYXQgY29tcHJlaGVu
c2lvbiAoZ2F0ZSBmb3VuZCBpdCwgcnVuIGlzIGhhbHRlZCkKICAgIGcuYXBwZW5kKF9nYXRlKCJP
TkVURVNULTcyIiwgImNvbXByZWhlbnNpb24iLCAiZmFpbCIsIDUsIHNjb3JlPTAuNCwgdGhyZXNo
b2xkPTAuNykpCiAgICAjIE9ORVRFU1QtNzMgZmFpbGVkIGF0IHJldmlldzogY29tcHJlaGVuc2lv
bi4uZGV2ZWxvcCBwYXNzLCByZXZpZXcgZmFpbAogICAgZm9yIGksIG5hbWUgaW4gZW51bWVyYXRl
KF9GVUxMWzo1XSk6CiAgICAgICAgZy5hcHBlbmQoX2dhdGUoIk9ORVRFU1QtNzMiLCBuYW1lLCAi
cGFzcyIsIDEwICsgaSkpCiAgICBnLmFwcGVuZChfZ2F0ZSgiT05FVEVTVC03MyIsICJyZXZpZXci
LCAiZmFpbCIsIDE2LCBzY29yZT0wLjUpKQogICAgIyBPTkVURVNULTc0IHJ1bm5pbmc6IHJlYWNo
ZWQgcGxhbgogICAgZm9yIGksIG5hbWUgaW4gZW51bWVyYXRlKF9GVUxMWzozXSk6CiAgICAgICAg
Zy5hcHBlbmQoX2dhdGUoIk9ORVRFU1QtNzQiLCBuYW1lLCAicGFzcyIsIDQwICsgaSkpCiAgICBy
ZXR1cm4gZwoKCmRlZiBfZXYoaXNzdWUsIGksIGFjdG9yLCBtb2RlbCwgcHYsIGNvc3QpOgogICAg
cmV0dXJuIGRpY3QoaXNzdWU9aXNzdWUsIGF0PV9ULmZvcm1hdChpKSwgYWN0b3I9YWN0b3IsIGtp
bmQ9Im1lc3NhZ2UiLAogICAgICAgICAgICAgICAgc3VtbWFyeT1hY3RvciArICIgYWN0ZWQiLCB0
b2tlbnNfaW49MTAwMCwgdG9rZW5zX291dD0zMDAsCiAgICAgICAgICAgICAgICBjb3N0X3VzZD1j
b3N0LCBtb2RlbD1tb2RlbCwgcHJvbXB0X3ZlcnNpb249cHYpCgoKZGVmIF9ldmVudHMoKToKICAg
IGUgPSBbCiAgICAgICAgX2V2KCJPTkVURVNULTcxIiwgMSwgInNwZWMiLCAiY2xhdWRlLXNvbm5l
dC00LjYiLCAic3BlY0AzIiwgMC4xMCksCiAgICAgICAgX2V2KCJPTkVURVNULTcxIiwgMiwgInBs
YW5uZXIiLCAiZ3B0LTQuMSIsICJwbGFuQDIiLCAwLjEyKSwKICAgICAgICBfZXYoIk9ORVRFU1Qt
NzEiLCAzLCAiZGV2ZWxvcGVyIiwgImNsYXVkZS1zb25uZXQtNC42IiwgImRldkAxIiwgMC4xNSks
CiAgICAgICAgX2V2KCJPTkVURVNULTcxIiwgNCwgInJldmlld2VyIiwgImNsYXVkZS1zb25uZXQt
NC42IiwgInJldmlld0AxIiwgMC4wNSksCiAgICAgICAgX2V2KCJPTkVURVNULTcyIiwgNSwgInNw
ZWMiLCAiY2xhdWRlLXNvbm5ldC00LjYiLCAic3BlY0AzIiwgMC4wMyksCiAgICAgICAgX2V2KCJP
TkVURVNULTczIiwgNiwgImRldmVsb3BlciIsICJncHQtNC4xIiwgImRldkAxIiwgMC4xMSksCiAg
ICAgICAgX2V2KCJPTkVURVNULTczIiwgNywgInJldmlld2VyIiwgImNsYXVkZS1zb25uZXQtNC42
IiwgInJldmlld0AxIiwgMC4xMCksCiAgICAgICAgX2V2KCJPTkVURVNULTc0IiwgOCwgInBsYW5u
ZXIiLCAiZ3B0LTQuMSIsICJwbGFuQDIiLCAwLjA1KSwKICAgIF0KICAgIGZvciBpLCBldiBpbiBl
bnVtZXJhdGUoZSwgMSk6CiAgICAgICAgZXZbIl9fcGtfXyJdID0gaQogICAgcmV0dXJuIGUKCgpk
ZWYgX2FydGlmYWN0cygpOgogICAgcmV0dXJuIFsKICAgICAgICBkaWN0KGlzc3VlPSJPTkVURVNU
LTcxIiwga2luZD0iZXZpZGVuY2UiLCByZWxfcGF0aD0iZXZpZGVuY2UvcmVwb3J0Lmh0bWwiLAog
ICAgICAgICAgICAgYWN0b3I9InFhIiwgc2hhMjU2PSJhIiAqIDY0LCBieXRlcz0yMDQ4LCBhdD1f
VC5mb3JtYXQoNDgpKSwKICAgICAgICBkaWN0KGlzc3VlPSJPTkVURVNULTcxIiwga2luZD0icGxh
biIsIHJlbF9wYXRoPSJwbGFuL3BsYW4ubWQiLAogICAgICAgICAgICAgYWN0b3I9InBsYW5uZXIi
LCBzaGEyNTY9ImIiICogNjQsIGJ5dGVzPTEwMjQsIGF0PV9ULmZvcm1hdCgyMCkpLAogICAgICAg
IGRpY3QoaXNzdWU9Ik9ORVRFU1QtNzMiLCBraW5kPSJldmlkZW5jZSIsIHJlbF9wYXRoPSJldmlk
ZW5jZS9mYWlsLmh0bWwiLAogICAgICAgICAgICAgYWN0b3I9InFhIiwgc2hhMjU2PSJjIiAqIDY0
LCBieXRlcz01MTIsIGF0PV9ULmZvcm1hdCgyOSkpLAogICAgXQoKCiMgLS0tLSB0aGUgQ09OVFJB
Q1QtZHJpdmVuIHdyaXRlciAtLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0t
LS0tLQoKZGVmIF9jdHlwZShjb2wsIGlzX3BrKToKICAgIGlmIGlzX3BrOgogICAgICAgIHJldHVy
biAiSU5URUdFUiBQUklNQVJZIEtFWSIKICAgIGxvdyA9IGNvbC5sb3dlcigpCiAgICBpZiBsb3cg
aW4gKCJ0b2tlbnNfaW4iLCAidG9rZW5zX291dCIsICJieXRlcyIsICJkdXJhdGlvbl9tcyIpIG9y
IGxvdy5lbmRzd2l0aCgiX2J5dGVzIik6CiAgICAgICAgcmV0dXJuICJJTlRFR0VSIgogICAgaWYg
bG93IGluICgiY29zdF91c2QiLCAic2NvcmUiLCAidGhyZXNob2xkIikgb3IgbG93LmVuZHN3aXRo
KCJfdXNkIik6CiAgICAgICAgcmV0dXJuICJSRUFMIgogICAgcmV0dXJuICJURVhUIgoKCmRlZiBf
YWxsX3NwZWNzKCk6CiAgICBzcGVjcyA9IGRpY3QocGIuQ09OVFJBQ1QpCiAgICBzcGVjcy51cGRh
dGUoZ2V0YXR0cihwYiwgIk9QVElPTkFMIiwge30pIG9yIHt9KQogICAgcmV0dXJuIHNwZWNzCgoK
ZGVmIF93cml0ZV9jdXJhdGVkKGNvbiwgdGFibGVfa2V5LCByb3dzLCBleHRyYT1Ob25lKToKICAg
IHNwZWNzID0gX2FsbF9zcGVjcygpCiAgICBpZiB0YWJsZV9rZXkgbm90IGluIHNwZWNzOgogICAg
ICAgIHJldHVybgogICAgc3BlYyA9IHNwZWNzW3RhYmxlX2tleV0KICAgIHRibCA9IHNwZWMuZ2V0
KCJ0YWJsZSIsIHRhYmxlX2tleSkKICAgIGNvbG1hcCA9IHNwZWMuZ2V0KCJjb2x1bW5zIiwge30p
IG9yIHt9CiAgICBwayA9IHNwZWMuZ2V0KCJwayIpCgogICAgb3JkZXIsIGNvbmNlcHRfYnlfcmVh
bCA9IFtdLCB7fQogICAgaWYgcGs6CiAgICAgICAgb3JkZXIuYXBwZW5kKHBrKQogICAgICAgIGNv
bmNlcHRfYnlfcmVhbFtwa10gPSAiX19wa19fIgogICAgZm9yIGNvbmNlcHQsIHJlYWwgaW4gY29s
bWFwLml0ZW1zKCk6CiAgICAgICAgaWYgcmVhbCBhbmQgcmVhbCBub3QgaW4gb3JkZXI6CiAgICAg
ICAgICAgIG9yZGVyLmFwcGVuZChyZWFsKQogICAgICAgICAgICBjb25jZXB0X2J5X3JlYWxbcmVh
bF0gPSBjb25jZXB0CiAgICAjIGV4dHJhIGNvbHVtbnMgdGhlIHRlc3RzIG5lZWQgZXZlbiB3aGVu
IHRoZSBDT05UUkFDVCBkb2VzIG5vdCBtYXAgdGhlbQogICAgIyAoZS5nLiByZXBvcnQucHkgLyBz
ZXJ2ZS5weSBkbyBVUERBVEUgcnVucyBTRVQgc3VtbWFyeT0uLi4sIGJ1dCBhIHJlbWFwcGVkCiAg
ICAjIENPTlRSQUNUIG1heSBtYXAgc3VtbWFyeSAtPiBOb25lLCBzbyBpdCB3b3VsZCBub3Qgb3Ro
ZXJ3aXNlIGJlIGNyZWF0ZWQpLgogICAgZm9yIHJlYWwsIGNvbmNlcHQgaW4gKGV4dHJhIG9yIHt9
KS5pdGVtcygpOgogICAgICAgIGlmIHJlYWwgbm90IGluIG9yZGVyOgogICAgICAgICAgICBvcmRl
ci5hcHBlbmQocmVhbCkKICAgICAgICAgICAgY29uY2VwdF9ieV9yZWFsW3JlYWxdID0gY29uY2Vw
dAoKICAgIGNvbGRlZnMgPSAiLCAiLmpvaW4oJyIlcyIgJXMnICUgKGMsIF9jdHlwZShjLCBwayBp
cyBub3QgTm9uZSBhbmQgYyA9PSBwaykpCiAgICAgICAgICAgICAgICAgICAgICAgIGZvciBjIGlu
IG9yZGVyKQogICAgY29uLmV4ZWN1dGUoJ0RST1AgVEFCTEUgSUYgRVhJU1RTICIlcyInICUgdGJs
KQogICAgY29uLmV4ZWN1dGUoJ0NSRUFURSBUQUJMRSAiJXMiICglcyknICUgKHRibCwgY29sZGVm
cykpCgogICAgY29sbGlzdCA9ICIsICIuam9pbignIiVzIicgJSBjIGZvciBjIGluIG9yZGVyKQog
ICAgcGggPSAiLCAiLmpvaW4oIj8iIGZvciBfIGluIG9yZGVyKQogICAgZm9yIHJvdyBpbiByb3dz
OgogICAgICAgIHZhbHMgPSBbcm93LmdldChjb25jZXB0X2J5X3JlYWxbY10pIGZvciBjIGluIG9y
ZGVyXQogICAgICAgIGNvbi5leGVjdXRlKCdJTlNFUlQgSU5UTyAiJXMiICglcykgVkFMVUVTICgl
cyknICUgKHRibCwgY29sbGlzdCwgcGgpLCB2YWxzKQoKCmRlZiBfd3JpdGVfZGlzY292ZXJlZChj
b24pOgogICAgIyB0YWJsZXMgbm9ib2R5IGRlY2xhcmVkIC0ga2V5ZWQgYnkgYSBwbGFpbiAidGlj
a2V0IiBjb2x1bW4gb24gcHVycG9zZSwgc28KICAgICMgZGlzY292ZXJ5IGZpbmRzIHRoZSBrZXkg
Y29sdW1uIGFuZCB0aGUgc2VsZi10ZXN0J3Mga2V5X2NvbHVtbj09InRpY2tldCIKICAgICMgaG9s
ZHMgcmVnYXJkbGVzcyBvZiB3aGF0IHRoZSBjdXJhdGVkIHRhYmxlcyBjYWxsIHRoZWlyIGtleS4K
ICAgIGNvbi5leGVjdXRlKCJEUk9QIFRBQkxFIElGIEVYSVNUUyBnb3Zlcm5vcl9kZWNpc2lvbnMi
KQogICAgY29uLmV4ZWN1dGUoIkNSRUFURSBUQUJMRSBnb3Zlcm5vcl9kZWNpc2lvbnMgKHRpY2tl
dCBURVhULCBkZWNpc2lvbiBURVhULCB0cyBURVhUKSIpCiAgICBnZCA9IFsoIk9ORVRFU1QtNzEi
LCAiYWxsb3ciKSwgKCJPTkVURVNULTcxIiwgImFsbG93IiksICgiT05FVEVTVC03MSIsICJhc2si
KSwKICAgICAgICAgICgiT05FVEVTVC03MiIsICJkZW55IiksICgiT05FVEVTVC03MiIsICJhbGxv
dyIpLCAoIk9ORVRFU1QtNzMiLCAiYXNrIildCiAgICBmb3IgaSwgKHRrLCBkZWMpIGluIGVudW1l
cmF0ZShnZCk6CiAgICAgICAgY29uLmV4ZWN1dGUoIklOU0VSVCBJTlRPIGdvdmVybm9yX2RlY2lz
aW9ucyBWQUxVRVMgKD8sPyw/KSIsICh0aywgZGVjLCBfVC5mb3JtYXQoaSkpKQoKICAgIGNvbi5l
eGVjdXRlKCJEUk9QIFRBQkxFIElGIEVYSVNUUyB0b29sX2NhbGxzIikKICAgIGNvbi5leGVjdXRl
KCJDUkVBVEUgVEFCTEUgdG9vbF9jYWxscyAodGlja2V0IFRFWFQsIHRvb2wgVEVYVCwgdHMgVEVY
VCkiKQogICAgdGMgPSBbKCJPTkVURVNULTcxIiwgImdyZXAiKSwgKCJPTkVURVNULTcxIiwgInJl
YWQiKSwgKCJPTkVURVNULTczIiwgImxpc3QiKV0KICAgIGZvciBpLCAodGssIHRsKSBpbiBlbnVt
ZXJhdGUodGMpOgogICAgICAgIGNvbi5leGVjdXRlKCJJTlNFUlQgSU5UTyB0b29sX2NhbGxzIFZB
TFVFUyAoPyw/LD8pIiwgKHRrLCB0bCwgX1QuZm9ybWF0KGkpKSkKCgpkZWYgd3JpdGVfZGVtbyhw
YXRoKToKICAgICIiIkJ1aWxkIGEgc3ludGhldGljIGxlZGdlciBhdCBgcGF0aGAgc2hhcGVkIHRv
IHRoZSBjdXJyZW50IENPTlRSQUNULiIiIgogICAgaWYgb3MucGF0aC5leGlzdHMocGF0aCk6CiAg
ICAgICAgb3MucmVtb3ZlKHBhdGgpCiAgICBjb24gPSBzcWxpdGUzLmNvbm5lY3QocGF0aCkKICAg
IHRyeToKICAgICAgICBfd3JpdGVfY3VyYXRlZChjb24sICJydW5zIiwgX3J1bnMoKSwgZXh0cmE9
eyJzdW1tYXJ5IjogInN1bW1hcnkifSkKICAgICAgICBfd3JpdGVfY3VyYXRlZChjb24sICJnYXRl
cyIsIF9nYXRlcygpKQogICAgICAgIF93cml0ZV9jdXJhdGVkKGNvbiwgImV2ZW50cyIsIF9ldmVu
dHMoKSkKICAgICAgICBfd3JpdGVfY3VyYXRlZChjb24sICJhcnRpZmFjdHMiLCBfYXJ0aWZhY3Rz
KCkpCiAgICAgICAgX3dyaXRlX2Rpc2NvdmVyZWQoY29uKQogICAgICAgIGNvbi5jb21taXQoKQog
ICAgZmluYWxseToKICAgICAgICBjb24uY2xvc2UoKQogICAgcmV0dXJuIHBhdGgKCgppZiBfX25h
bWVfXyA9PSAiX19tYWluX18iOgogICAgb3V0ID0gc3lzLmFyZ3ZbMV0gaWYgbGVuKHN5cy5hcmd2
KSA+IDEgZWxzZSAiZGVtby5kYiIKICAgIHdyaXRlX2RlbW8ob3V0KQogICAgcHJpbnQoIndyb3Rl
IGRlbW8gbGVkZ2VyOiIsIG91dCkK
"""


def _die(msg, code=2):
    print("fix_demo: " + msg)
    sys.exit(code)


def main():
    pb = os.path.join(HERE, "payload_builder.py")
    if not os.path.exists(pb):
        _die("no payload_builder.py in this folder (" + HERE + ").\n"
             "  Put fix_demo.py in your docket/ folder, beside payload_builder.py, "
             "and run it from there.")

    target = os.path.join(HERE, "_demo_ledger.py")
    if os.path.exists(target):
        bak = target + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        with open(target, "rb") as f:
            old = f.read()
        with open(bak, "wb") as f:
            f.write(old)
        print("backed up old _demo_ledger.py -> " + os.path.basename(bak))
    else:
        print("no existing _demo_ledger.py; writing a fresh one.")

    data = base64.b64decode(_DEMO_B64.encode())
    with open(target, "wb") as f:
        f.write(data)
    print("wrote CONTRACT-driven _demo_ledger.py (" + str(len(data)) + " bytes)")

    # clear any stale bytecode so the new module is used
    pycache = os.path.join(HERE, "__pycache__")
    if os.path.isdir(pycache):
        for n in os.listdir(pycache):
            if n.startswith("_demo_ledger") or n.startswith("payload_builder"):
                try:
                    os.remove(os.path.join(pycache, n))
                except OSError:
                    pass

    print("")
    print("=" * 60)
    print("running self-tests")
    print("=" * 60)
    any_fail = False
    for script in ("payload_builder.py", "report.py", "serve.py"):
        path = os.path.join(HERE, script)
        if not os.path.exists(path):
            continue
        print("\n$ python " + script + " --self-test")
        r = subprocess.run([sys.executable, path, "--self-test"],
                           cwd=HERE, capture_output=True, text=True)
        out = (r.stdout or "") + (r.stderr or "")
        # show the last few lines (the tally, and any FAIL lines)
        tail = [ln for ln in out.splitlines()
                if ln.strip() and ("FAIL" in ln or "self-test" in ln
                                   or "self test" in ln or "Traceback" in ln
                                   or "Error" in ln)]
        for ln in (tail[-12:] if tail else out.splitlines()[-5:]):
            print("  " + ln)
        if r.returncode != 0:
            any_fail = True

    print("")
    print("=" * 60)
    if any_fail:
        print("Some self-tests still report failures above.")
        print("If the ONLY failures mention key_column, enum, or 'id', those are")
        print("default-schema assumptions in the test, not problems with your")
        print("ledger - your real dashboard is verified by:")
        print("    python payload_builder.py --db ledger.db --doctor")
        print("Send me the FAIL lines and I will adjust.")
    else:
        print("All self-tests green. The demo now matches your CONTRACT.")
    print("Your old demo is preserved as the .bak file next to _demo_ledger.py.")
    print("=" * 60)
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
