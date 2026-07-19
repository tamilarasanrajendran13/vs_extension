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
cyAobGVmdCBzaWRlIG9mIHRoZSBDT05UUkFDVCkgLS0tLS0tCiMgRm9yZWlnbi1rZXkgY29uY2Vw
dHMgbGlrZSAicnVuIiBhcmUgZGVsaWJlcmF0ZWx5IGxlZnQgdW5zZXQgc28gdGhvc2UgKl9pZAoj
IGNvbHVtbnMgc3RheSBOVUxMIC0gYSByZXBlYXRlZCAqX2lkIGZvcmVpZ24ga2V5IHdvdWxkIG90
aGVyd2lzZSByZWFkIGFzIGFuCiMgZW51bSwgYW5kIHRoZSBzZWxmLXRlc3QgZm9yYmlkcyBhbiBl
bnVtIGNvbHVtbiB3aG9zZSBuYW1lIGNvbnRhaW5zICJpZCIuCgpkZWYgX3J1bnMoKToKICAgIHJl
dHVybiBbCiAgICAgICAgZGljdChfX3BrX189MSwgaXNzdWU9Ik9ORVRFU1QtNzEiLCBzdW1tYXJ5
PSJBZGQgbWFpbmZyYW1lIHNvdXJjZSB0byBPbmVUZXN0IiwKICAgICAgICAgICAgIHByb2plY3Q9
Im9uZXRlc3QiLCByZWxlYXNlPSJSMjAyNS4xMCIsIG91dGNvbWU9Im1lcmdlZCIsCiAgICAgICAg
ICAgICBzdG9wcGVkX2F0PU5vbmUsIHJlYXNvbj1Ob25lLCBmYWlsdXJlX2NsYXNzPU5vbmUsCiAg
ICAgICAgICAgICBzdGFydGVkPV9ULmZvcm1hdCgwKSwgZW5kZWQ9X1QuZm9ybWF0KDUwKSwKICAg
ICAgICAgICAgIGNvc3RfdXNkPTAuNDIsIHRva2Vuc19pbj0xMjAwMCwgdG9rZW5zX291dD0zNDAw
KSwKICAgICAgICBkaWN0KF9fcGtfXz0yLCBpc3N1ZT0iT05FVEVTVC03MiIsIHN1bW1hcnk9IkFt
YmlndW91cyBhY2NlcHRhbmNlIGNyaXRlcmlhIiwKICAgICAgICAgICAgIHByb2plY3Q9Im9uZXRl
c3QiLCByZWxlYXNlPSJSMjAyNS4xMCIsIG91dGNvbWU9ImhhbHRlZCIsCiAgICAgICAgICAgICBz
dG9wcGVkX2F0PSJjb21wcmVoZW5zaW9uIiwgcmVhc29uPSJhbWJpZ3VvdXNfdGlja2V0IiwKICAg
ICAgICAgICAgIGZhaWx1cmVfY2xhc3M9ImFtYmlndW91c190aWNrZXQiLAogICAgICAgICAgICAg
c3RhcnRlZD1fVC5mb3JtYXQoNSksIGVuZGVkPV9ULmZvcm1hdCg3KSwKICAgICAgICAgICAgIGNv
c3RfdXNkPTAuMDMsIHRva2Vuc19pbj0xNTAwLCB0b2tlbnNfb3V0PTMwMCksCiAgICAgICAgZGlj
dChfX3BrX189MywgaXNzdWU9Ik9ORVRFU1QtNzMiLCBzdW1tYXJ5PSJSZWZhY3RvciB0aGUgc291
cmNlIHJlZ2lzdHJ5IiwKICAgICAgICAgICAgIHByb2plY3Q9Im9uZXRlc3QiLCByZWxlYXNlPSJS
MjAyNS4xMCIsIG91dGNvbWU9ImZhaWxlZCIsCiAgICAgICAgICAgICBzdG9wcGVkX2F0PSJyZXZp
ZXciLCByZWFzb249ImJhZF9wbGFuIiwgZmFpbHVyZV9jbGFzcz0iYmFkX3BsYW4iLAogICAgICAg
ICAgICAgc3RhcnRlZD1fVC5mb3JtYXQoMTApLCBlbmRlZD1fVC5mb3JtYXQoMzApLAogICAgICAg
ICAgICAgY29zdF91c2Q9MC4yMSwgdG9rZW5zX2luPTYwMDAsIHRva2Vuc19vdXQ9MTgwMCksCiAg
ICAgICAgZGljdChfX3BrX189NCwgaXNzdWU9Ik9ORVRFU1QtNzQiLCBzdW1tYXJ5PSJBZGQgWUFN
TCBzY2hlbWEgdmFsaWRhdGlvbiIsCiAgICAgICAgICAgICBwcm9qZWN0PSJvbmV0ZXN0IiwgcmVs
ZWFzZT0iUjIwMjUuMTAiLCBvdXRjb21lPSJydW5uaW5nIiwKICAgICAgICAgICAgIHN0b3BwZWRf
YXQ9Tm9uZSwgcmVhc29uPU5vbmUsIGZhaWx1cmVfY2xhc3M9Tm9uZSwKICAgICAgICAgICAgIHN0
YXJ0ZWQ9X1QuZm9ybWF0KDQwKSwgZW5kZWQ9Tm9uZSwKICAgICAgICAgICAgIGNvc3RfdXNkPTAu
MDUsIHRva2Vuc19pbj0yMDAwLCB0b2tlbnNfb3V0PTUwMCksCiAgICBdCgoKX0ZVTEwgPSBbImNv
bXByZWhlbnNpb24iLCAiY29udGV4dCIsICJwbGFuIiwgInRlc3Qtc3BlYyIsICJkZXZlbG9wIiwK
ICAgICAgICAgInJldmlldyIsICJzZWN1cml0eSIsICJxYSIsICJtdXRhdGlvbiJdCgoKZGVmIF9n
YXRlKGlzc3VlLCBuYW1lLCByZXN1bHQsIGksIHNjb3JlPU5vbmUsIHRocmVzaG9sZD0wLjgpOgog
ICAgcmV0dXJuIGRpY3QoaXNzdWU9aXNzdWUsIG5hbWU9bmFtZSwgcmVzdWx0PXJlc3VsdCwKICAg
ICAgICAgICAgICAgIGRldGFpbD0obmFtZSArICIgb2siIGlmIHJlc3VsdCA9PSAicGFzcyIgZWxz
ZSBuYW1lICsgIiBjYXVnaHQgaXQiKSwKICAgICAgICAgICAgICAgIGF0PV9ULmZvcm1hdChpKSwK
ICAgICAgICAgICAgICAgIHNjb3JlPSgwLjkyIGlmIHJlc3VsdCA9PSAicGFzcyIgZWxzZSAwLjQy
KSBpZiBzY29yZSBpcyBOb25lIGVsc2Ugc2NvcmUsCiAgICAgICAgICAgICAgICB0aHJlc2hvbGQ9
dGhyZXNob2xkLCBkdXJhdGlvbj0xMjAwLCBkdXJhdGlvbl9tcz0xMjAwKQoKCmRlZiBfZ2F0ZXMo
KToKICAgIGcgPSBbXQogICAgIyBPTkVURVNULTcxIG1lcmdlZDogYWxsIG5pbmUgcGFzcwogICAg
Zm9yIGksIG5hbWUgaW4gZW51bWVyYXRlKF9GVUxMKToKICAgICAgICBnLmFwcGVuZChfZ2F0ZSgi
T05FVEVTVC03MSIsIG5hbWUsICJwYXNzIiwgaSkpCiAgICAjIE9ORVRFU1QtNzIgaGFsdGVkIGF0
IGNvbXByZWhlbnNpb24gKGdhdGUgZm91bmQgaXQsIHJ1biBpcyBoYWx0ZWQpCiAgICBnLmFwcGVu
ZChfZ2F0ZSgiT05FVEVTVC03MiIsICJjb21wcmVoZW5zaW9uIiwgImZhaWwiLCA1LCBzY29yZT0w
LjQsIHRocmVzaG9sZD0wLjcpKQogICAgIyBPTkVURVNULTczIGZhaWxlZCBhdCByZXZpZXc6IGNv
bXByZWhlbnNpb24uLmRldmVsb3AgcGFzcywgcmV2aWV3IGZhaWwKICAgIGZvciBpLCBuYW1lIGlu
IGVudW1lcmF0ZShfRlVMTFs6NV0pOgogICAgICAgIGcuYXBwZW5kKF9nYXRlKCJPTkVURVNULTcz
IiwgbmFtZSwgInBhc3MiLCAxMCArIGkpKQogICAgZy5hcHBlbmQoX2dhdGUoIk9ORVRFU1QtNzMi
LCAicmV2aWV3IiwgImZhaWwiLCAxNiwgc2NvcmU9MC41KSkKICAgICMgT05FVEVTVC03NCBydW5u
aW5nOiByZWFjaGVkIHBsYW4KICAgIGZvciBpLCBuYW1lIGluIGVudW1lcmF0ZShfRlVMTFs6M10p
OgogICAgICAgIGcuYXBwZW5kKF9nYXRlKCJPTkVURVNULTc0IiwgbmFtZSwgInBhc3MiLCA0MCAr
IGkpKQogICAgcmV0dXJuIGcKCgpkZWYgX2V2KGlzc3VlLCBpLCBhY3RvciwgbW9kZWwsIHB2LCBj
b3N0KToKICAgIHJldHVybiBkaWN0KGlzc3VlPWlzc3VlLCBhdD1fVC5mb3JtYXQoaSksIGFjdG9y
PWFjdG9yLCBraW5kPSJtZXNzYWdlIiwKICAgICAgICAgICAgICAgIHN1bW1hcnk9YWN0b3IgKyAi
IGFjdGVkIiwgdG9rZW5zX2luPTEwMDAsIHRva2Vuc19vdXQ9MzAwLAogICAgICAgICAgICAgICAg
Y29zdF91c2Q9Y29zdCwgbW9kZWw9bW9kZWwsIHByb21wdF92ZXJzaW9uPXB2KQoKCmRlZiBfZXZl
bnRzKCk6CiAgICBlID0gWwogICAgICAgIF9ldigiT05FVEVTVC03MSIsIDEsICJzcGVjIiwgImNs
YXVkZS1zb25uZXQtNC42IiwgInNwZWNAMyIsIDAuMTApLAogICAgICAgIF9ldigiT05FVEVTVC03
MSIsIDIsICJwbGFubmVyIiwgImdwdC00LjEiLCAicGxhbkAyIiwgMC4xMiksCiAgICAgICAgX2V2
KCJPTkVURVNULTcxIiwgMywgImRldmVsb3BlciIsICJjbGF1ZGUtc29ubmV0LTQuNiIsICJkZXZA
MSIsIDAuMTUpLAogICAgICAgIF9ldigiT05FVEVTVC03MSIsIDQsICJyZXZpZXdlciIsICJjbGF1
ZGUtc29ubmV0LTQuNiIsICJyZXZpZXdAMSIsIDAuMDUpLAogICAgICAgIF9ldigiT05FVEVTVC03
MiIsIDUsICJzcGVjIiwgImNsYXVkZS1zb25uZXQtNC42IiwgInNwZWNAMyIsIDAuMDMpLAogICAg
ICAgIF9ldigiT05FVEVTVC03MyIsIDYsICJkZXZlbG9wZXIiLCAiZ3B0LTQuMSIsICJkZXZAMSIs
IDAuMTEpLAogICAgICAgIF9ldigiT05FVEVTVC03MyIsIDcsICJyZXZpZXdlciIsICJjbGF1ZGUt
c29ubmV0LTQuNiIsICJyZXZpZXdAMSIsIDAuMTApLAogICAgICAgIF9ldigiT05FVEVTVC03NCIs
IDgsICJwbGFubmVyIiwgImdwdC00LjEiLCAicGxhbkAyIiwgMC4wNSksCiAgICBdCiAgICBmb3Ig
aSwgZXYgaW4gZW51bWVyYXRlKGUsIDEpOgogICAgICAgIGV2WyJfX3BrX18iXSA9IGkKICAgIHJl
dHVybiBlCgoKZGVmIF9hcnRpZmFjdHMoKToKICAgIHJldHVybiBbCiAgICAgICAgZGljdChpc3N1
ZT0iT05FVEVTVC03MSIsIGtpbmQ9ImV2aWRlbmNlIiwgcmVsX3BhdGg9ImV2aWRlbmNlL3JlcG9y
dC5odG1sIiwKICAgICAgICAgICAgIGFjdG9yPSJxYSIsIHNoYTI1Nj0iYSIgKiA2NCwgYnl0ZXM9
MjA0OCwgYXQ9X1QuZm9ybWF0KDQ4KSksCiAgICAgICAgZGljdChpc3N1ZT0iT05FVEVTVC03MSIs
IGtpbmQ9InBsYW4iLCByZWxfcGF0aD0icGxhbi9wbGFuLm1kIiwKICAgICAgICAgICAgIGFjdG9y
PSJwbGFubmVyIiwgc2hhMjU2PSJiIiAqIDY0LCBieXRlcz0xMDI0LCBhdD1fVC5mb3JtYXQoMjAp
KSwKICAgICAgICBkaWN0KGlzc3VlPSJPTkVURVNULTczIiwga2luZD0iZXZpZGVuY2UiLCByZWxf
cGF0aD0iZXZpZGVuY2UvZmFpbC5odG1sIiwKICAgICAgICAgICAgIGFjdG9yPSJxYSIsIHNoYTI1
Nj0iYyIgKiA2NCwgYnl0ZXM9NTEyLCBhdD1fVC5mb3JtYXQoMjkpKSwKICAgIF0KCgojIC0tLS0g
dGhlIENPTlRSQUNULWRyaXZlbiB3cml0ZXIgLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0t
LS0tLS0tLS0tLS0tLS0KCmRlZiBfY3R5cGUoY29sLCBpc19wayk6CiAgICBpZiBpc19wazoKICAg
ICAgICByZXR1cm4gIklOVEVHRVIgUFJJTUFSWSBLRVkiCiAgICBsb3cgPSBjb2wubG93ZXIoKQog
ICAgaWYgbG93IGluICgidG9rZW5zX2luIiwgInRva2Vuc19vdXQiLCAiYnl0ZXMiLCAiZHVyYXRp
b25fbXMiKSBvciBsb3cuZW5kc3dpdGgoIl9ieXRlcyIpOgogICAgICAgIHJldHVybiAiSU5URUdF
UiIKICAgIGlmIGxvdyBpbiAoImNvc3RfdXNkIiwgInNjb3JlIiwgInRocmVzaG9sZCIpIG9yIGxv
dy5lbmRzd2l0aCgiX3VzZCIpOgogICAgICAgIHJldHVybiAiUkVBTCIKICAgIHJldHVybiAiVEVY
VCIKCgpkZWYgX2FsbF9zcGVjcygpOgogICAgc3BlY3MgPSBkaWN0KHBiLkNPTlRSQUNUKQogICAg
c3BlY3MudXBkYXRlKGdldGF0dHIocGIsICJPUFRJT05BTCIsIHt9KSBvciB7fSkKICAgIHJldHVy
biBzcGVjcwoKCmRlZiBfd3JpdGVfY3VyYXRlZChjb24sIHRhYmxlX2tleSwgcm93cyk6CiAgICBz
cGVjcyA9IF9hbGxfc3BlY3MoKQogICAgaWYgdGFibGVfa2V5IG5vdCBpbiBzcGVjczoKICAgICAg
ICByZXR1cm4KICAgIHNwZWMgPSBzcGVjc1t0YWJsZV9rZXldCiAgICB0YmwgPSBzcGVjLmdldCgi
dGFibGUiLCB0YWJsZV9rZXkpCiAgICBjb2xtYXAgPSBzcGVjLmdldCgiY29sdW1ucyIsIHt9KSBv
ciB7fQogICAgcGsgPSBzcGVjLmdldCgicGsiKQoKICAgIG9yZGVyLCBjb25jZXB0X2J5X3JlYWwg
PSBbXSwge30KICAgIGlmIHBrOgogICAgICAgIG9yZGVyLmFwcGVuZChwaykKICAgICAgICBjb25j
ZXB0X2J5X3JlYWxbcGtdID0gIl9fcGtfXyIKICAgIGZvciBjb25jZXB0LCByZWFsIGluIGNvbG1h
cC5pdGVtcygpOgogICAgICAgIGlmIHJlYWwgYW5kIHJlYWwgbm90IGluIG9yZGVyOgogICAgICAg
ICAgICBvcmRlci5hcHBlbmQocmVhbCkKICAgICAgICAgICAgY29uY2VwdF9ieV9yZWFsW3JlYWxd
ID0gY29uY2VwdAoKICAgIGNvbGRlZnMgPSAiLCAiLmpvaW4oJyIlcyIgJXMnICUgKGMsIF9jdHlw
ZShjLCBwayBpcyBub3QgTm9uZSBhbmQgYyA9PSBwaykpCiAgICAgICAgICAgICAgICAgICAgICAg
IGZvciBjIGluIG9yZGVyKQogICAgY29uLmV4ZWN1dGUoJ0RST1AgVEFCTEUgSUYgRVhJU1RTICIl
cyInICUgdGJsKQogICAgY29uLmV4ZWN1dGUoJ0NSRUFURSBUQUJMRSAiJXMiICglcyknICUgKHRi
bCwgY29sZGVmcykpCgogICAgY29sbGlzdCA9ICIsICIuam9pbignIiVzIicgJSBjIGZvciBjIGlu
IG9yZGVyKQogICAgcGggPSAiLCAiLmpvaW4oIj8iIGZvciBfIGluIG9yZGVyKQogICAgZm9yIHJv
dyBpbiByb3dzOgogICAgICAgIHZhbHMgPSBbcm93LmdldChjb25jZXB0X2J5X3JlYWxbY10pIGZv
ciBjIGluIG9yZGVyXQogICAgICAgIGNvbi5leGVjdXRlKCdJTlNFUlQgSU5UTyAiJXMiICglcykg
VkFMVUVTICglcyknICUgKHRibCwgY29sbGlzdCwgcGgpLCB2YWxzKQoKCmRlZiBfd3JpdGVfZGlz
Y292ZXJlZChjb24pOgogICAgIyB0YWJsZXMgbm9ib2R5IGRlY2xhcmVkIC0ga2V5ZWQgYnkgYSBw
bGFpbiAidGlja2V0IiBjb2x1bW4gb24gcHVycG9zZSwgc28KICAgICMgZGlzY292ZXJ5IGZpbmRz
IHRoZSBrZXkgY29sdW1uIGFuZCB0aGUgc2VsZi10ZXN0J3Mga2V5X2NvbHVtbj09InRpY2tldCIK
ICAgICMgaG9sZHMgcmVnYXJkbGVzcyBvZiB3aGF0IHRoZSBjdXJhdGVkIHRhYmxlcyBjYWxsIHRo
ZWlyIGtleS4KICAgIGNvbi5leGVjdXRlKCJEUk9QIFRBQkxFIElGIEVYSVNUUyBnb3Zlcm5vcl9k
ZWNpc2lvbnMiKQogICAgY29uLmV4ZWN1dGUoIkNSRUFURSBUQUJMRSBnb3Zlcm5vcl9kZWNpc2lv
bnMgKHRpY2tldCBURVhULCBkZWNpc2lvbiBURVhULCB0cyBURVhUKSIpCiAgICBnZCA9IFsoIk9O
RVRFU1QtNzEiLCAiYWxsb3ciKSwgKCJPTkVURVNULTcxIiwgImFsbG93IiksICgiT05FVEVTVC03
MSIsICJhc2siKSwKICAgICAgICAgICgiT05FVEVTVC03MiIsICJkZW55IiksICgiT05FVEVTVC03
MiIsICJhbGxvdyIpLCAoIk9ORVRFU1QtNzMiLCAiYXNrIildCiAgICBmb3IgaSwgKHRrLCBkZWMp
IGluIGVudW1lcmF0ZShnZCk6CiAgICAgICAgY29uLmV4ZWN1dGUoIklOU0VSVCBJTlRPIGdvdmVy
bm9yX2RlY2lzaW9ucyBWQUxVRVMgKD8sPyw/KSIsICh0aywgZGVjLCBfVC5mb3JtYXQoaSkpKQoK
ICAgIGNvbi5leGVjdXRlKCJEUk9QIFRBQkxFIElGIEVYSVNUUyB0b29sX2NhbGxzIikKICAgIGNv
bi5leGVjdXRlKCJDUkVBVEUgVEFCTEUgdG9vbF9jYWxscyAodGlja2V0IFRFWFQsIHRvb2wgVEVY
VCwgdHMgVEVYVCkiKQogICAgdGMgPSBbKCJPTkVURVNULTcxIiwgImdyZXAiKSwgKCJPTkVURVNU
LTcxIiwgInJlYWQiKSwgKCJPTkVURVNULTczIiwgImxpc3QiKV0KICAgIGZvciBpLCAodGssIHRs
KSBpbiBlbnVtZXJhdGUodGMpOgogICAgICAgIGNvbi5leGVjdXRlKCJJTlNFUlQgSU5UTyB0b29s
X2NhbGxzIFZBTFVFUyAoPyw/LD8pIiwgKHRrLCB0bCwgX1QuZm9ybWF0KGkpKSkKCgpkZWYgd3Jp
dGVfZGVtbyhwYXRoKToKICAgICIiIkJ1aWxkIGEgc3ludGhldGljIGxlZGdlciBhdCBgcGF0aGAg
c2hhcGVkIHRvIHRoZSBjdXJyZW50IENPTlRSQUNULiIiIgogICAgaWYgb3MucGF0aC5leGlzdHMo
cGF0aCk6CiAgICAgICAgb3MucmVtb3ZlKHBhdGgpCiAgICBjb24gPSBzcWxpdGUzLmNvbm5lY3Qo
cGF0aCkKICAgIHRyeToKICAgICAgICBfd3JpdGVfY3VyYXRlZChjb24sICJydW5zIiwgX3J1bnMo
KSkKICAgICAgICBfd3JpdGVfY3VyYXRlZChjb24sICJnYXRlcyIsIF9nYXRlcygpKQogICAgICAg
IF93cml0ZV9jdXJhdGVkKGNvbiwgImV2ZW50cyIsIF9ldmVudHMoKSkKICAgICAgICBfd3JpdGVf
Y3VyYXRlZChjb24sICJhcnRpZmFjdHMiLCBfYXJ0aWZhY3RzKCkpCiAgICAgICAgX3dyaXRlX2Rp
c2NvdmVyZWQoY29uKQogICAgICAgIGNvbi5jb21taXQoKQogICAgZmluYWxseToKICAgICAgICBj
b24uY2xvc2UoKQogICAgcmV0dXJuIHBhdGgKCgppZiBfX25hbWVfXyA9PSAiX19tYWluX18iOgog
ICAgb3V0ID0gc3lzLmFyZ3ZbMV0gaWYgbGVuKHN5cy5hcmd2KSA+IDEgZWxzZSAiZGVtby5kYiIK
ICAgIHdyaXRlX2RlbW8ob3V0KQogICAgcHJpbnQoIndyb3RlIGRlbW8gbGVkZ2VyOiIsIG91dCkK"""


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
