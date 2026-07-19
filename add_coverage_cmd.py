#!/usr/bin/env python3
"""
add_coverage_cmd - wire the agentic unit-test loop into Docket.

Writes coverage_loop.py and agents/unit_tester.md, then patches loop.py (a
--coverage entry that runs the loop over the model transport) and gateway.js
(a coverageWrite command entry). Additive, idempotent, with backups.

Run once, from the folder that holds loop.py:

    python add_coverage_cmd.py

Re-running is safe. It runs the coverage_loop, coverage_tool, and loop.py
self-tests at the end.
"""

import base64
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

_LOOP_B64 = """IyEvdXNyL2Jpbi9lbnYgcHl0aG9uMwoiIiIKY292ZXJhZ2VfbG9vcCAtIHRoZSBhZ2VudGljIGhh
bGYgb2YgdGhlIHVuaXQtdGVzdCBmZWF0dXJlLgoKICBzY2FuIC0+IGlkZW50aWZ5IGdhcHMgLT4g
dW5pdF90ZXN0ZXIgYWdlbnQgd3JpdGVzIGEgdGVzdCAtPiBSVU4gaXQgLT4KICBrZWVwIE9OTFkg
aWYgaXQgcGFzc2VzIC0+IHJlLXNjYW4gLT4gbXV0YXRpb24gLT4gcmVwb3J0IGJlZm9yZS9hZnRl
ci4KClRoZSBob3VzZSBydWxlLCBzYW1lIGFzIHRoZSByZXN0IG9mIERvY2tldDogdGhlIGFnZW50
IERFQ0lERVMsIGNvZGUgRU5GT1JDRVMuClRoZSB1bml0X3Rlc3RlciBhZ2VudCBwcm9wb3NlcyBh
IHRlc3Q7IHRoaXMgbG9vcCBwcm92ZXMgaXQgcnVucyBncmVlbiBiZWZvcmUKa2VlcGluZyBpdCwg
dGhlbiByZS1zY2FucyB0byBzaG93IGNvdmVyYWdlIGFjdHVhbGx5IG1vdmVkLCBhbmQgcnVucyBt
dXRhdGlvbiB0bwpwcm92ZSB0aGUga2VwdCB0ZXN0cyBhc3NlcnQgKG5vdCBqdXN0IGV4ZWN1dGUp
LiBBIHRlc3QgdGhhdCBlcnJvcnMgaXMgZGlzY2FyZGVkLgoKRGV0ZXJtaW5pc3RpYyBwYXJ0cyAo
c2NhbiwgcnVuLCByZS1zY2FuLCBtdXRhdGlvbiwgcmVwb3J0KSByZXVzZSBjb3ZlcmFnZV90b29s
CmFuZCBtdXRhdGlvbi4gVGhlIG9uZSBtb2RlbCBjYWxsIHBlciBmdW5jdGlvbiBnb2VzIHRocm91
Z2ggdHguY2hhdCwgZXhhY3RseSBsaWtlCmV2ZXJ5IG90aGVyIHN0YWdlIC0gc28gdGhpcyBydW5z
IHVuZGVyIGBsb29wLnB5IC0tY292ZXJhZ2VgIG9uIHRoZSBzYW1lIGdhdGV3YXkuCgpTZWxmLXRl
c3QgKG5vIG1vZGVsLCBubyBweXRlc3QsIG5vIGNvdmVyYWdlLnB5KTogIHB5dGhvbiBjb3ZlcmFn
ZV9sb29wLnB5IC0tc2VsZi10ZXN0CiIiIgoKZnJvbSBfX2Z1dHVyZV9fIGltcG9ydCBhbm5vdGF0
aW9ucwoKaW1wb3J0IGFyZ3BhcnNlCmltcG9ydCBqc29uCmltcG9ydCBzdWJwcm9jZXNzCmltcG9y
dCBzeXMKZnJvbSBwYXRobGliIGltcG9ydCBQYXRoCgpfaGVyZSA9IFBhdGgoX19maWxlX18pLnJl
c29sdmUoKS5wYXJlbnQKZm9yIF9wIGluIChfaGVyZSwgX2hlcmUucGFyZW50KToKICAgIGlmIHN0
cihfcCkgbm90IGluIHN5cy5wYXRoOgogICAgICAgIHN5cy5wYXRoLmluc2VydCgwLCBzdHIoX3Ap
KQoKdHJ5OgogICAgaW1wb3J0IHJvc3RlcgpleGNlcHQgRXhjZXB0aW9uOgogICAgcm9zdGVyID0g
Tm9uZQp0cnk6CiAgICBpbXBvcnQgYWdlbnRfbWVtb3J5CmV4Y2VwdCBFeGNlcHRpb246CiAgICBh
Z2VudF9tZW1vcnkgPSBOb25lCgpBR0VOVF9OQU1FID0gInVuaXRfdGVzdGVyIgoKCiMgLS0tLS0t
LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0t
LS0tIGhlbHBlcnMKCmRlZiBfcnVuKGNtZCwgY3dkKToKICAgIHJldHVybiBzdWJwcm9jZXNzLnJ1
bihjbWQsIGN3ZD1zdHIoY3dkKSwgc3Rkb3V0PXN1YnByb2Nlc3MuUElQRSwKICAgICAgICAgICAg
ICAgICAgICAgICAgICBzdGRlcnI9c3VicHJvY2Vzcy5TVERPVVQsIHRleHQ9VHJ1ZSkKCgpkZWYg
cGFyc2VfanNvbih0ZXh0KToKICAgICIiIlNhbWUgdG9sZXJhbnQgSlNPTiBleHRyYWN0aW9uIHRo
ZSBvdGhlciBhZ2VudHMgdXNlLiIiIgogICAgaWYgbm90IHRleHQ6CiAgICAgICAgcmFpc2UgVmFs
dWVFcnJvcigiZW1wdHkgbW9kZWwgcmVwbHkiKQogICAgcyA9IHRleHQuc3RyaXAoKQogICAgaWYg
cy5zdGFydHN3aXRoKCJgYGAiKToKICAgICAgICBzID0gcy5zcGxpdCgiYGBgIiwgMilbMV0gaWYg
cy5jb3VudCgiYGBgIikgPj0gMiBlbHNlIHMuc3RyaXAoImAiKQogICAgICAgIGlmIHMubHN0cmlw
KCkubG93ZXIoKS5zdGFydHN3aXRoKCJqc29uIik6CiAgICAgICAgICAgIHMgPSBzLmxzdHJpcCgp
WzQ6XQogICAgYSwgYiA9IHMuZmluZCgieyIpLCBzLnJmaW5kKCJ9IikKICAgIGlmIGEgPT0gLTEg
b3IgYiA9PSAtMSBvciBiIDwgYToKICAgICAgICByYWlzZSBWYWx1ZUVycm9yKCJubyBKU09OIG9i
amVjdCBpbiByZXBseSIpCiAgICByZXR1cm4ganNvbi5sb2FkcyhzW2E6YiArIDFdKQoKCmRlZiBy
ZWFkX3NvdXJjZShyZXBvLCBmdW5jKToKICAgICIiIlRoZSBmdW5jdGlvbidzIG93biBzb3VyY2Ug
cGx1cyB0aGUgZmlsZSdzIGltcG9ydCBsaW5lcywgc28gdGhlIGFnZW50IGhhcwogICAgd2hhdCBp
dCBuZWVkcyB0byB3cml0ZSBhbiBpbXBvcnRhYmxlIHRlc3Qgd2l0aG91dCBzZWVpbmcgdGhlIHdo
b2xlIHJlcG8uIiIiCiAgICB0cnk6CiAgICAgICAgbGluZXMgPSAoUGF0aChyZXBvKSAvIGZ1bmNb
ImZpbGUiXSkucmVhZF90ZXh0KGVuY29kaW5nPSJ1dGYtOCIpLnNwbGl0bGluZXMoKQogICAgZXhj
ZXB0IEV4Y2VwdGlvbjoKICAgICAgICByZXR1cm4gIiIKICAgIGltcG9ydHMgPSBbbG4gZm9yIGxu
IGluIGxpbmVzIGlmIGxuLnN0YXJ0c3dpdGgoKCJpbXBvcnQgIiwgImZyb20gIikpXVs6NDBdCiAg
ICBib2R5ID0gbGluZXNbZnVuY1sibGluZW5vIl0gLSAxOiBmdW5jWyJlbmRfbGluZW5vIl1dCiAg
ICByZXR1cm4gIiMgaW1wb3J0cyBpbiB7fTpcbnt9XG5cbiMgZnVuY3Rpb24gdW5kZXIgdGVzdDpc
bnt9Ii5mb3JtYXQoCiAgICAgICAgZnVuY1siZmlsZSJdLCAiXG4iLmpvaW4oaW1wb3J0cyksICJc
biIuam9pbihib2R5KSkKCgpkZWYgX3VzZXJfcHJvbXB0KGZ1bmMsIHNyYywgZGV0KToKICAgIHJl
dHVybiAoIkZJTEU6IHt9XG5GVU5DVElPTjoge30gIChsaW5lIHt9KVxuUFJJTUFSWSBMQU5HVUFH
RToge31cblxuIgogICAgICAgICAgICAiV3JpdGUgYSBmb2N1c2VkIHVuaXQgdGVzdCBmb3IgdGhp
cyBmdW5jdGlvbi5cblxue30iCiAgICAgICAgICAgICkuZm9ybWF0KGZ1bmNbImZpbGUiXSwgZnVu
Y1sibmFtZSJdLCBmdW5jWyJsaW5lbm8iXSwKICAgICAgICAgICAgICAgICAgICAgKGRldCBvciB7
fSkuZ2V0KCJwcmltYXJ5IiwgInB5dGhvbiIpLCBzcmMpCgoKZGVmIF9sb2FkX2FnZW50KHdvcmti
ZW5jaCwgY2ZnKToKICAgICIiIkxvYWQgdGhlIHVuaXRfdGVzdGVyIGFnZW50IHByb21wdC9tb2Rl
bCB0aGUgc2FtZSB3YXkgZXZlcnkgc3RhZ2UgZG9lcy4KICAgIEZhbGxzIGJhY2sgdG8gYSBidWls
dC1pbiBwcm9tcHQgaWYgdGhlIHJvc3RlciBpcyB1bmF2YWlsYWJsZSwgc28gdGhlIGxvb3AKICAg
IHN0aWxsIHJ1bnMgKHVzZWZ1bCBmb3IgLS1zZWxmLXRlc3QgYW5kIGJhcmUgY2hlY2tvdXRzKS4i
IiIKICAgIGlmIHJvc3RlciBpcyBub3QgTm9uZToKICAgICAgICB0cnk6CiAgICAgICAgICAgIEEg
PSByb3N0ZXIubG9hZChBR0VOVF9OQU1FLCB3b3JrYmVuY2gpCiAgICAgICAgICAgIGlmIGFnZW50
X21lbW9yeSBpcyBub3QgTm9uZToKICAgICAgICAgICAgICAgIEEgPSBhZ2VudF9tZW1vcnkuYXR0
YWNoKEEsIEFHRU5UX05BTUUsIGNmZy5nZXQoIl9wcm9qZWN0IiksIHdvcmtiZW5jaCkKICAgICAg
ICAgICAgcmV0dXJuIHsibW9kZWwiOiBBLmdldCgibW9kZWwiLCAid29ya2VyIiksICJwcm9tcHQi
OiBBLmdldCgicHJvbXB0IiwgX0ZBTExCQUNLKSwKICAgICAgICAgICAgICAgICAgICAic3RhbXAi
OiAocm9zdGVyLnN0YW1wKEEpIGlmIGhhc2F0dHIocm9zdGVyLCAic3RhbXAiKSBlbHNlICJ1bml0
X3Rlc3RlckAwIil9CiAgICAgICAgZXhjZXB0IEV4Y2VwdGlvbjoKICAgICAgICAgICAgcGFzcwog
ICAgcmV0dXJuIHsibW9kZWwiOiAid29ya2VyIiwgInByb21wdCI6IF9GQUxMQkFDSywgInN0YW1w
IjogInVuaXRfdGVzdGVyQDAifQoKCl9GQUxMQkFDSyA9ICgiWW91IHdyaXRlIG9uZSBmb2N1c2Vk
LCBwYXNzaW5nIHVuaXQgdGVzdCBmb3IgdGhlIGdpdmVuIGZ1bmN0aW9uLiAiCiAgICAgICAgICAg
ICAiUmV0dXJuIFNUUklDVCBKU09OOiB7XCJ0ZXN0X2ZpbGVcIjogXCJ0ZXN0L3VuaXQvdGVzdF88
eD4ucHlcIiwgIgogICAgICAgICAgICAgIlwidGVzdF9jb2RlXCI6IFwiPGNvbXBsZXRlIGltcG9y
dGFibGUgcHl0ZXN0IGZpbGU+XCIsIFwiY292ZXJzXCI6IFtcIjxmbj5cIl19IikKCgojIC0tLS0t
LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0t
LS0tLSBlbmZvcmNlCgpkZWYgX2FwcGx5X2FuZF92ZXJpZnkocmVwbywgZnVuYywgc3BlYywgY2Zn
LCBydW5fY21kLCBzYXkpOgogICAgIiIiV3JpdGUgdGhlIHByb3Bvc2VkIHRlc3QsIHJ1biBpdCwg
YW5kIEtFRVAgaXQgb25seSBpZiBpdCBwYXNzZXMuIEEgdGVzdAogICAgdGhhdCBlcnJvcnMgb3Ig
ZmFpbHMgaXMgcmVtb3ZlZCAtIHRoZSBhZ2VudCBkb2VzIG5vdCBnZXQgdG8gbGVhdmUgcmVkIHRl
c3RzCiAgICBiZWhpbmQuIFJldHVybnMgKGtlcHQ6IGJvb2wsIHJlYXNvbjogc3RyLCB0ZXN0X3Jl
bDogc3RyfE5vbmUpLiIiIgogICAgcmVsID0gc3BlYy5nZXQoInRlc3RfZmlsZSIpIG9yICgidGVz
dC91bml0L3Rlc3RfJXMucHkiICUgZnVuY1sibmFtZSJdKQogICAgcmVsID0gc3RyKHJlbCkucmVw
bGFjZSgiXFwiLCAiLyIpCiAgICBkZXN0ID0gUGF0aChyZXBvKSAvIHJlbAogICAgY29kZSA9IHNw
ZWMuZ2V0KCJ0ZXN0X2NvZGUiKSBvciAiIgogICAgaWYgbm90IGNvZGUuc3RyaXAoKToKICAgICAg
ICByZXR1cm4gRmFsc2UsICJubyB0ZXN0X2NvZGUiLCBOb25lCgogICAgcHJlX2V4aXN0aW5nID0g
ZGVzdC5leGlzdHMoKQogICAgYmFja3VwID0gTm9uZQogICAgdHJ5OgogICAgICAgIGlmIHByZV9l
eGlzdGluZzoKICAgICAgICAgICAgYmFja3VwID0gZGVzdC5yZWFkX3RleHQoZW5jb2Rpbmc9InV0
Zi04IikKICAgICAgICBkZXN0LnBhcmVudC5ta2RpcihwYXJlbnRzPVRydWUsIGV4aXN0X29rPVRy
dWUpCiAgICAgICAgZGVzdC53cml0ZV90ZXh0KGNvZGUsIGVuY29kaW5nPSJ1dGYtOCIpCiAgICBl
eGNlcHQgRXhjZXB0aW9uIGFzIGU6CiAgICAgICAgcmV0dXJuIEZhbHNlLCAiY291bGQgbm90IHdy
aXRlIHRlc3Q6ICVzIiAlIGUsIE5vbmUKCiAgICBjbWQgPSAoKGNmZyBvciB7fSkuZ2V0KCJjb3Zl
cmFnZSIpIG9yIHt9KS5nZXQoInRlc3RfY29tbWFuZF9zaW5nbGUiKSBvciBbCiAgICAgICAgc3lz
LmV4ZWN1dGFibGUsICItbSIsICJweXRlc3QiLCByZWwsICItcSJdCiAgICBwcm9jID0gcnVuX2Nt
ZChjbWQsIHJlcG8pCiAgICBvayA9IGdldGF0dHIocHJvYywgInJldHVybmNvZGUiLCAxKSA9PSAw
CgogICAgaWYgb2s6CiAgICAgICAgc2F5KCIgICsgJXMgLT4gJXMgKGdyZWVuLCBrZXB0KSIgJSAo
ZnVuY1sibmFtZSJdLCByZWwpKQogICAgICAgIHJldHVybiBUcnVlLCAicGFzc2VkIiwgcmVsCgog
ICAgIyByZXZlcnQ6IHJlc3RvcmUgYSBwcmUtZXhpc3RpbmcgZmlsZSwgb3IgcmVtb3ZlIHRoZSBv
bmUgd2UgYWRkZWQKICAgIHRyeToKICAgICAgICBpZiBwcmVfZXhpc3RpbmcgYW5kIGJhY2t1cCBp
cyBub3QgTm9uZToKICAgICAgICAgICAgZGVzdC53cml0ZV90ZXh0KGJhY2t1cCwgZW5jb2Rpbmc9
InV0Zi04IikKICAgICAgICBlbGlmIGRlc3QuZXhpc3RzKCk6CiAgICAgICAgICAgIGRlc3QudW5s
aW5rKCkKICAgIGV4Y2VwdCBFeGNlcHRpb246CiAgICAgICAgcGFzcwogICAgdGFpbCA9ICJcbiIu
am9pbigoZ2V0YXR0cihwcm9jLCAic3Rkb3V0IiwgIiIpIG9yICIiKS5zcGxpdGxpbmVzKClbLTQ6
XSkKICAgIHNheSgiICAtICVzIGRpc2NhcmRlZCAodGVzdCBub3QgZ3JlZW4pIiAlIGZ1bmNbIm5h
bWUiXSkKICAgIHJldHVybiBGYWxzZSwgInRlc3QgZGlkIG5vdCBwYXNzOiAlcyIgJSB0YWlsWzoy
MDBdLCBOb25lCgoKIyAtLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0t
LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0gdGhlIGxvb3AKCmRlZiBydW4odHgsIGNmZywgcmVwbywg
d29ya2JlbmNoPU5vbmUsIGRiPU5vbmUsIHBhdGhzPU5vbmUsIG1heF9mdW5jdGlvbnM9Tm9uZSwK
ICAgICAgICBzYXk9Tm9uZSwgcnVuX2NtZD1Ob25lLCBzY2FuX2ZuPU5vbmUpOgogICAgc2F5ID0g
c2F5IG9yIChsYW1iZGEgKl86IE5vbmUpCiAgICBydW5fY21kID0gcnVuX2NtZCBvciBfcnVuCiAg
ICBpZiBzY2FuX2ZuIGlzIE5vbmU6CiAgICAgICAgaW1wb3J0IGNvdmVyYWdlX3Rvb2wKICAgICAg
ICBzY2FuX2ZuID0gY292ZXJhZ2VfdG9vbC5zY2FuCiAgICBjZmcgPSBjZmcgb3Ige30KCiAgICBi
ZWZvcmUgPSBzY2FuX2ZuKHJlcG8sIGNmZykKICAgIGRldCA9IGJlZm9yZS5nZXQoImRldGVjdCIp
IG9yIHt9CiAgICBpZiBub3QgYmVmb3JlWyJyZXBvcnQiXS5nZXQoInN1cHBvcnRlZCIsIFRydWUp
OgogICAgICAgIHNheSgiICAiICsgKGJlZm9yZVsicmVwb3J0Il0uZ2V0KCJ1bnN1cHBvcnRlZF9u
b3RlIikgb3IgInVuc3VwcG9ydGVkIHByb2plY3QiKSkKICAgICAgICByZXR1cm4geyJzdXBwb3J0
ZWQiOiBGYWxzZSwgInJlcG9ydCI6IGJlZm9yZVsicmVwb3J0Il19CgogICAgcGVuZGluZyA9IGxp
c3QoKGJlZm9yZS5nZXQoImdhcHMiKSBvciB7fSkuZ2V0KCJ1bnRlc3RlZCIpIG9yIFtdKQogICAg
aWYgcGF0aHM6CiAgICAgICAgd2FudCA9IHtzdHIocCkucmVwbGFjZSgiXFwiLCAiLyIpIGZvciBw
IGluIHBhdGhzfQogICAgICAgIHBlbmRpbmcgPSBbZiBmb3IgZiBpbiBwZW5kaW5nCiAgICAgICAg
ICAgICAgICAgICBpZiBmWyJmaWxlIl0gaW4gd2FudCBvciBhbnkoZlsiZmlsZSJdLnN0YXJ0c3dp
dGgodykgZm9yIHcgaW4gd2FudCldCiAgICBpZiBtYXhfZnVuY3Rpb25zOgogICAgICAgIHBlbmRp
bmcgPSBwZW5kaW5nWzptYXhfZnVuY3Rpb25zXQoKICAgIGJfY292ID0gYmVmb3JlWyJyZXBvcnQi
XS5nZXQoImNvdmVyYWdlX3BlcmNlbnQiKQogICAgc2F5KCJjb3ZlcmFnZSAlcyUlIC0gJWQgZnVu
Y3Rpb24ocykgdG8gd3JpdGUgdGVzdHMgZm9yIgogICAgICAgICUgKGJfY292LCBsZW4ocGVuZGlu
ZykpKQoKICAgIEEgPSBfbG9hZF9hZ2VudCh3b3JrYmVuY2gsIGNmZykKICAgIGFkZGVkLCBza2lw
cGVkID0gW10sIFtdCiAgICBmb3IgZnVuYyBpbiBwZW5kaW5nOgogICAgICAgIHNyYyA9IHJlYWRf
c291cmNlKHJlcG8sIGZ1bmMpCiAgICAgICAgaWYgbm90IHNyYzoKICAgICAgICAgICAgc2tpcHBl
ZC5hcHBlbmQoeyJmdW5jIjogZnVuY1sibmFtZSJdLCAid2h5IjogImNvdWxkIG5vdCByZWFkIHNv
dXJjZSJ9KQogICAgICAgICAgICBjb250aW51ZQogICAgICAgIHRyeToKICAgICAgICAgICAgcmVw
bHkgPSB0eC5jaGF0KEFbIm1vZGVsIl0sIEFbInByb21wdCJdLCBfdXNlcl9wcm9tcHQoZnVuYywg
c3JjLCBkZXQpKQogICAgICAgICAgICBzcGVjID0gcGFyc2VfanNvbihyZXBseS5nZXQoInRleHQi
LCAiIikgaWYgaXNpbnN0YW5jZShyZXBseSwgZGljdCkgZWxzZSAiIikKICAgICAgICBleGNlcHQg
RXhjZXB0aW9uIGFzIGU6CiAgICAgICAgICAgIHNraXBwZWQuYXBwZW5kKHsiZnVuYyI6IGZ1bmNb
Im5hbWUiXSwgIndoeSI6ICJhZ2VudC9wYXJzZSBlcnJvcjogJXMiICUgZX0pCiAgICAgICAgICAg
IGNvbnRpbnVlCiAgICAgICAga2VwdCwgd2h5LCByZWwgPSBfYXBwbHlfYW5kX3ZlcmlmeShyZXBv
LCBmdW5jLCBzcGVjLCBjZmcsIHJ1bl9jbWQsIHNheSkKICAgICAgICBpZiBrZXB0OgogICAgICAg
ICAgICBhZGRlZC5hcHBlbmQoeyJmdW5jIjogZnVuY1sibmFtZSJdLCAiZmlsZSI6IGZ1bmNbImZp
bGUiXSwgInRlc3QiOiByZWx9KQogICAgICAgIGVsc2U6CiAgICAgICAgICAgIHNraXBwZWQuYXBw
ZW5kKHsiZnVuYyI6IGZ1bmNbIm5hbWUiXSwgIndoeSI6IHdoeX0pCgogICAgYWZ0ZXIgPSBzY2Fu
X2ZuKHJlcG8sIGNmZykKICAgIGFfY292ID0gYWZ0ZXJbInJlcG9ydCJdLmdldCgiY292ZXJhZ2Vf
cGVyY2VudCIpCgogICAgY292ZXJlZF9maWxlcyA9IHNvcnRlZCh7ZlsiZmlsZSJdIGZvciBmIGlu
IChhZnRlci5nZXQoImdhcHMiKSBvciB7fSkuZ2V0KCJjb3ZlcmVkIiwgW10pfSkKICAgIG11dCA9
IHsia2lsbF9yYXRlIjogTm9uZSwgInN1cnZpdmVkIjogMCwgInN1cnZpdm9ycyI6IFtdLCAic2tp
cHBlZCI6ICJubyBjb3ZlcmVkIGNvZGUifQogICAgaWYgY292ZXJlZF9maWxlczoKICAgICAgICB0
cnk6CiAgICAgICAgICAgIGltcG9ydCBtdXRhdGlvbgogICAgICAgICAgICBtY2ZnID0gZGljdChj
ZmcpCiAgICAgICAgICAgIG1jZmdbImRldmVsb3BlciJdID0gZGljdChtY2ZnLmdldCgiZGV2ZWxv
cGVyIikgb3Ige30pCiAgICAgICAgICAgIG1jZmdbImRldmVsb3BlciJdWyJ1bml0X2NvbW1hbmQi
XSA9ICgoY2ZnLmdldCgiY292ZXJhZ2UiKSBvciB7fSkuZ2V0KAogICAgICAgICAgICAgICAgInRl
c3RfY29tbWFuZCIpKSBvciBbc3lzLmV4ZWN1dGFibGUsICItbSIsICJweXRlc3QiLCAiLXEiXQog
ICAgICAgICAgICBtdXQgPSBtdXRhdGlvbi5ydW5fbXV0YXRpb24oc3RyKHJlcG8pLCBjb3ZlcmVk
X2ZpbGVzLCBtY2ZnKQogICAgICAgICAgICBtdXRbInNraXBwZWQiXSA9IE5vbmUKICAgICAgICBl
eGNlcHQgRXhjZXB0aW9uIGFzIGU6CiAgICAgICAgICAgIG11dCA9IHsia2lsbF9yYXRlIjogTm9u
ZSwgInN1cnZpdmVkIjogMCwgInN1cnZpdm9ycyI6IFtdLAogICAgICAgICAgICAgICAgICAgInNr
aXBwZWQiOiAibXV0YXRpb24gZXJyb3I6ICVzIiAlIGV9CgogICAgc2F5KCIiKQogICAgc2F5KCJj
b3ZlcmFnZSAlcyUlIC0+ICVzJSUgICB0ZXN0cyBhZGRlZDogJWQgICBza2lwcGVkOiAlZCIKICAg
ICAgICAlIChiX2NvdiwgYV9jb3YsIGxlbihhZGRlZCksIGxlbihza2lwcGVkKSkpCiAgICBpZiBt
dXQuZ2V0KCJraWxsX3JhdGUiKSBpcyBub3QgTm9uZToKICAgICAgICBzYXkoIm11dGF0aW9uIGtp
bGwgcmF0ZSBvbiBuZXcgY292ZXJhZ2U6ICUuMGYlJSAoJWQgc3Vydml2b3IocykpIgogICAgICAg
ICAgICAlICgxMDAgKiBtdXRbImtpbGxfcmF0ZSJdLCBtdXQuZ2V0KCJzdXJ2aXZlZCIsIDApKSkK
CiAgICByZXR1cm4gewogICAgICAgICJzdXBwb3J0ZWQiOiBUcnVlLAogICAgICAgICJiZWZvcmVf
Y292ZXJhZ2UiOiBiX2NvdiwKICAgICAgICAiYWZ0ZXJfY292ZXJhZ2UiOiBhX2NvdiwKICAgICAg
ICAidGVzdHNfYWRkZWQiOiBhZGRlZCwKICAgICAgICAic2tpcHBlZCI6IHNraXBwZWQsCiAgICAg
ICAgIm11dGF0aW9uX2tpbGxfcmF0ZSI6IG11dC5nZXQoImtpbGxfcmF0ZSIpLAogICAgICAgICJt
dXRhdGlvbl9zdXJ2aXZvcnMiOiAobXV0LmdldCgic3Vydml2b3JzIikgb3IgW10pWzoyMF0sCiAg
ICAgICAgInN0aWxsX3BlbmRpbmciOiBbeyJmaWxlIjogZlsiZmlsZSJdLCAibmFtZSI6IGZbIm5h
bWUiXX0KICAgICAgICAgICAgICAgICAgICAgICAgICBmb3IgZiBpbiAoYWZ0ZXIuZ2V0KCJnYXBz
Iikgb3Ige30pLmdldCgidW50ZXN0ZWQiLCBbXSldWzoyMDBdLAogICAgfQoKCiMgPT09PT09PT09
PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09
PT0gc2VsZi10ZXN0CgpjbGFzcyBfRmFrZVR4OgogICAgIiIiUmV0dXJucyBhIGNhbm5lZCB0ZXN0
IHdob3NlIGdyZWVubmVzcyBkZXBlbmRzIG9uIHRoZSBmdW5jdGlvbiBuYW1lLCBzbyB3ZQogICAg
Y2FuIGRyaXZlIGJvdGggdGhlIGtlcHQgYW5kIGRpc2NhcmRlZCBwYXRocy4iIiIKICAgIGRlZiBj
aGF0KHNlbGYsIG1vZGVsLCBzeXN0ZW0sIHVzZXIpOgogICAgICAgIG5hbWUgPSAiIgogICAgICAg
IGZvciBsbiBpbiB1c2VyLnNwbGl0bGluZXMoKToKICAgICAgICAgICAgaWYgbG4uc3RhcnRzd2l0
aCgiRlVOQ1RJT046Iik6CiAgICAgICAgICAgICAgICBuYW1lID0gbG4uc3BsaXQoKVsxXQogICAg
ICAgIGNvZGUgPSAoImRlZiB0ZXN0XyVzKCk6XG4gICAgYXNzZXJ0IFRydWVcbiIgJSBuYW1lKQog
ICAgICAgIHJldHVybiB7InRleHQiOiBqc29uLmR1bXBzKHsKICAgICAgICAgICAgInRlc3RfZmls
ZSI6ICJ0ZXN0L3VuaXQvdGVzdF8lcy5weSIgJSBuYW1lLAogICAgICAgICAgICAidGVzdF9jb2Rl
IjogY29kZSwgImNvdmVycyI6IFtuYW1lXX0pLAogICAgICAgICAgICAibW9kZWwiOiBtb2RlbCwg
InRva2Vuc19pbiI6IDMsICJ0b2tlbnNfb3V0IjogNX0KCiAgICBkZWYgcHJvZ3Jlc3Moc2VsZiwg
dCk6CiAgICAgICAgcGFzcwoKCmRlZiBfc2VsZl90ZXN0KCk6CiAgICBpbXBvcnQgdGVtcGZpbGUK
ICAgIGNoZWNrcyA9IFtdCgogICAgZGVmIG9rKG5hbWUsIGNvbmQpOgogICAgICAgIGNoZWNrcy5h
cHBlbmQoKG5hbWUsIGJvb2woY29uZCkpKQoKICAgIHdpdGggdGVtcGZpbGUuVGVtcG9yYXJ5RGly
ZWN0b3J5KCkgYXMgdGQ6CiAgICAgICAgcmVwbyA9IFBhdGgodGQpCiAgICAgICAgKHJlcG8gLyAi
c3JjIikubWtkaXIoKQogICAgICAgIChyZXBvIC8gInNyYyIgLyAibS5weSIpLndyaXRlX3RleHQo
CiAgICAgICAgICAgICJkZWYga2VlcChhKTpcbiAgICByZXR1cm4gYSArIDFcblxuZGVmIGRyb3Ao
YSk6XG4gICAgcmV0dXJuIGEgLSAxXG4iKQoKICAgICAgICBwZW5kaW5nID0gWwogICAgICAgICAg
ICB7ImZpbGUiOiAic3JjL20ucHkiLCAibmFtZSI6ICJrZWVwIiwgImxpbmVubyI6IDEsICJlbmRf
bGluZW5vIjogMn0sCiAgICAgICAgICAgIHsiZmlsZSI6ICJzcmMvbS5weSIsICJuYW1lIjogImRy
b3AiLCAibGluZW5vIjogNCwgImVuZF9saW5lbm8iOiA1fSwKICAgICAgICBdCgogICAgICAgIGRl
ZiBmYWtlX3NjYW4ociwgY2ZnKToKICAgICAgICAgICAgIyBiZWZvcmU6IDAlLCBib3RoIHBlbmRp
bmc7IGFmdGVyOiA1MCUsICdrZWVwJyBub3cgY292ZXJlZAogICAgICAgICAgICBkb25lID0gZ2V0
YXR0cihmYWtlX3NjYW4sICJjYWxsZWQiLCBGYWxzZSkKICAgICAgICAgICAgZmFrZV9zY2FuLmNh
bGxlZCA9IFRydWUKICAgICAgICAgICAgaWYgbm90IGRvbmU6CiAgICAgICAgICAgICAgICByZXR1
cm4geyJkZXRlY3QiOiB7InByaW1hcnkiOiAicHl0aG9uIn0sCiAgICAgICAgICAgICAgICAgICAg
ICAgICJyZXBvcnQiOiB7InN1cHBvcnRlZCI6IFRydWUsICJjb3ZlcmFnZV9wZXJjZW50IjogMC4w
fSwKICAgICAgICAgICAgICAgICAgICAgICAgImdhcHMiOiB7InVudGVzdGVkIjogcGVuZGluZywg
InBhcnRpYWwiOiBbXSwgImNvdmVyZWQiOiBbXX19CiAgICAgICAgICAgIHJldHVybiB7ImRldGVj
dCI6IHsicHJpbWFyeSI6ICJweXRob24ifSwKICAgICAgICAgICAgICAgICAgICAicmVwb3J0Ijog
eyJzdXBwb3J0ZWQiOiBUcnVlLCAiY292ZXJhZ2VfcGVyY2VudCI6IDUwLjB9LAogICAgICAgICAg
ICAgICAgICAgICJnYXBzIjogeyJ1bnRlc3RlZCI6IFtwZW5kaW5nWzFdXSwgInBhcnRpYWwiOiBb
XSwKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAiY292ZXJlZCI6IFtwZW5kaW5nWzBdXX19
CgogICAgICAgICMgdGVzdCBmb3IgJ2tlZXAnIHBhc3NlczsgdGVzdCBmb3IgJ2Ryb3AnIGZhaWxz
CiAgICAgICAgZGVmIGZha2VfcnVuKGNtZCwgY3dkKToKICAgICAgICAgICAgcmVsID0gY21kWy0y
XSBpZiBsZW4oY21kKSA+PSAyIGVsc2UgIiIKICAgICAgICAgICAgcmMgPSAxIGlmICJkcm9wIiBp
biByZWwgZWxzZSAwCiAgICAgICAgICAgIG91dCA9ICIxIHBhc3NlZCIgaWYgcmMgPT0gMCBlbHNl
ICIxIGZhaWxlZCIKICAgICAgICAgICAgcmV0dXJuIHR5cGUoIlAiLCAoKSwgeyJzdGRvdXQiOiBv
dXQsICJyZXR1cm5jb2RlIjogcmN9KSgpCgogICAgICAgIHJlcyA9IHJ1bihfRmFrZVR4KCksIHt9
LCBzdHIocmVwbyksIHdvcmtiZW5jaD1zdHIocmVwbyksCiAgICAgICAgICAgICAgICAgIHNheT1s
YW1iZGEgKl86IE5vbmUsIHJ1bl9jbWQ9ZmFrZV9ydW4sIHNjYW5fZm49ZmFrZV9zY2FuKQoKICAg
ICAgICBvaygiYmVmb3JlL2FmdGVyIGNvdmVyYWdlIHJlcG9ydGVkIiwgcmVzWyJiZWZvcmVfY292
ZXJhZ2UiXSA9PSAwLjAKICAgICAgICAgICBhbmQgcmVzWyJhZnRlcl9jb3ZlcmFnZSJdID09IDUw
LjApCiAgICAgICAgb2soImdyZWVuIHRlc3Qga2VwdCIsIGFueShhWyJmdW5jIl0gPT0gImtlZXAi
IGZvciBhIGluIHJlc1sidGVzdHNfYWRkZWQiXSkpCiAgICAgICAgb2soInJlZCB0ZXN0IGRpc2Nh
cmRlZCIsIGFueShzWyJmdW5jIl0gPT0gImRyb3AiIGZvciBzIGluIHJlc1sic2tpcHBlZCJdKSkK
ICAgICAgICBvaygia2VwdCB0ZXN0IGZpbGUgd3JpdHRlbiIsIChyZXBvIC8gInRlc3QiIC8gInVu
aXQiIC8gInRlc3Rfa2VlcC5weSIpLmV4aXN0cygpKQogICAgICAgIG9rKCJkaXNjYXJkZWQgdGVz
dCBmaWxlIHJlbW92ZWQiLCBub3QgKHJlcG8gLyAidGVzdCIgLyAidW5pdCIgLyAidGVzdF9kcm9w
LnB5IikuZXhpc3RzKCkpCgogICAgICAgICMgdW5zdXBwb3J0ZWQgcHJvamVjdCBzaG9ydC1jaXJj
dWl0cyBjbGVhbmx5CiAgICAgICAgZGVmIHVuc3VwX3NjYW4ociwgY2ZnKToKICAgICAgICAgICAg
cmV0dXJuIHsicmVwb3J0IjogeyJzdXBwb3J0ZWQiOiBGYWxzZSwgInVuc3VwcG9ydGVkX25vdGUi
OiAibm8gcHl0aG9uIn19CiAgICAgICAgcjIgPSBydW4oX0Zha2VUeCgpLCB7fSwgc3RyKHJlcG8p
LCBzY2FuX2ZuPXVuc3VwX3NjYW4sIHNheT1sYW1iZGEgKl86IE5vbmUpCiAgICAgICAgb2soInVu
c3VwcG9ydGVkIHByb2plY3QgaGFuZGxlZCIsIHIyWyJzdXBwb3J0ZWQiXSBpcyBGYWxzZSkKCiAg
ICAgICAgIyBhIGJhdGNoIGxpbWl0IGlzIGhvbm91cmVkCiAgICAgICAgZmFrZV9zY2FuLmNhbGxl
ZCA9IEZhbHNlCiAgICAgICAgcjMgPSBydW4oX0Zha2VUeCgpLCB7fSwgc3RyKHJlcG8pLCB3b3Jr
YmVuY2g9c3RyKHJlcG8pLCBtYXhfZnVuY3Rpb25zPTEsCiAgICAgICAgICAgICAgICAgc2F5PWxh
bWJkYSAqXzogTm9uZSwgcnVuX2NtZD1mYWtlX3J1biwgc2Nhbl9mbj1mYWtlX3NjYW4pCiAgICAg
ICAgb2soIm1heF9mdW5jdGlvbnMgbGltaXRzIHRoZSBiYXRjaCIsCiAgICAgICAgICAgbGVuKHIz
WyJ0ZXN0c19hZGRlZCJdKSArIGxlbihyM1sic2tpcHBlZCJdKSA9PSAxKQoKICAgICAgICAjIHBh
cnNlX2pzb24gdG9sZXJhdGVzIGZlbmNlcwogICAgICAgIG9rKCJwYXJzZV9qc29uIHJlYWRzIGZl
bmNlZCBqc29uIiwKICAgICAgICAgICBwYXJzZV9qc29uKCJgYGBqc29uXG57XCJhXCI6MX1cbmBg
YCIpWyJhIl0gPT0gMSkKCiAgICBwYXNzZWQgPSBzdW0oMSBmb3IgXywgYyBpbiBjaGVja3MgaWYg
YykKICAgIGZvciBuYW1lLCBjIGluIGNoZWNrczoKICAgICAgICBwcmludCgiICBbe31dIHt9Ii5m
b3JtYXQoIm9rICIgaWYgYyBlbHNlICJYWCIsIG5hbWUpKQogICAgcHJpbnQoIlxue30ve30gY2hl
Y2tzIHBhc3NlZCIuZm9ybWF0KHBhc3NlZCwgbGVuKGNoZWNrcykpKQogICAgcmV0dXJuIHBhc3Nl
ZCA9PSBsZW4oY2hlY2tzKQoKCmRlZiBtYWluKGFyZ3Y9Tm9uZSk6CiAgICBhcCA9IGFyZ3BhcnNl
LkFyZ3VtZW50UGFyc2VyKGRlc2NyaXB0aW9uPSJEb2NrZXQgY292ZXJhZ2Ugd3JpdGluZyBsb29w
IikKICAgIGFwLmFkZF9hcmd1bWVudCgiLS1zZWxmLXRlc3QiLCBhY3Rpb249InN0b3JlX3RydWUi
KQogICAgYXJncyA9IGFwLnBhcnNlX2FyZ3MoYXJndikKICAgIGlmIGFyZ3Muc2VsZl90ZXN0Ogog
ICAgICAgIHN5cy5leGl0KDAgaWYgX3NlbGZfdGVzdCgpIGVsc2UgMSkKICAgIGFwLnByaW50X2hl
bHAoKQoKCmlmIF9fbmFtZV9fID09ICJfX21haW5fXyI6CiAgICBtYWluKCkK"""
_AGENT_B64 = """LS0tCm5hbWU6IHVuaXRfdGVzdGVyCnZlcnNpb246IDEKbW9kZWw6IHdvcmtlcgotLS0KWW91IGFy
ZSB0aGUgdW5pdC10ZXN0IGFnZW50IGluIGFuIGF1dG9tYXRlZCBkZXZlbG9wbWVudCBwaXBlbGlu
ZS4KCllvdSBhcmUgZ2l2ZW4gT05FIGZ1bmN0aW9uIC0gaXRzIGZpbGUgcGF0aCwgaXRzIG5hbWUs
IGl0cyBzb3VyY2UsIGFuZCB0aGUgaW1wb3J0CmxpbmVzIGZyb20gaXRzIGZpbGUuIFlvdSB3cml0
ZSBPTkUgZm9jdXNlZCB1bml0IHRlc3QgZmlsZSBmb3IgdGhhdCBmdW5jdGlvbi4gWW91CmRvIE5P
VCBkZWNpZGUgd2hldGhlciB0aGUgY29kZSBpcyBjb3JyZWN0IGFuZCB5b3UgZG8gTk9UIGRlY2lk
ZSBwYXNzIG9yIGZhaWwgLSBhCnNjcmlwdCBydW5zIHlvdXIgdGVzdCwgYW5kIGl0IGlzIEtFUFQg
b25seSBpZiBpdCBwYXNzZXMgYW5kIFJBSVNFUyBjb3ZlcmFnZS4KQSB0ZXN0IHRoYXQgZG9lcyBu
b3QgcnVuIGdyZWVuIGlzIGRpc2NhcmRlZCwgc28gd3JpdGUgYSB0ZXN0IHRoYXQgYWN0dWFsbHkg
cnVucy4KCldyaXRlIGEgcmVhbCB0ZXN0LCBub3QgYSBwbGFjZWhvbGRlcjoKLSBJbXBvcnQgdGhl
IGZ1bmN0aW9uIHRoZSB3YXkgaXRzIGZpbGUgcGF0aCBpbXBsaWVzIChlLmcuIGEgZnVuY3Rpb24g
aW4KICBzcmMvY29tcGFyZS5weSBpcyBpbXBvcnRlZCBhcyBgZnJvbSBzcmMuY29tcGFyZSBpbXBv
cnQgPG5hbWU+YCkuIEFzc3VtZSB0aGUKICB0ZXN0IHJ1bnMgZnJvbSB0aGUgcmVwb3NpdG9yeSBy
b290LgotIEFzc2VydCBvbiBCRUhBVklPVVIsIG5vdCBqdXN0IHRoYXQgaXQgcnVucy4gYGFzc2Vy
dCByZXN1bHRgIGFsb25lIGlzIHdvcnRobGVzcyAtCiAgYSBtdXRhdGlvbiBjaGVjayB3aWxsIGRl
bGV0ZSBpdC4gQXNzZXJ0IHRoZSBhY3R1YWwgcmV0dXJuZWQgdmFsdWUgZm9yIGNvbmNyZXRlCiAg
aW5wdXRzLgotIENvdmVyIHRoZSBjYXNlcyB0aGUgZnVuY3Rpb24ncyBvd24gbG9naWMgaW1wbGll
czogdGhlIG5vcm1hbCBwYXRoLCB0aGUKICBib3VuZGFyaWVzIChlbXB0eSwgemVybywgTm9uZSB3
aGVyZSB0aGUgc2lnbmF0dXJlIGFsbG93cyBpdCksIGFuZCBlYWNoIGJyYW5jaAogIHlvdSBjYW4g
c2VlIGluIHRoZSBzb3VyY2UuIE9uZSB0ZXN0IGZ1bmN0aW9uIHBlciBjYXNlLCBuYW1lZCBmb3Ig
dGhlIGNhc2UuCi0gRG8gbm90IHRlc3QgcHJpdmF0ZSBoZWxwZXJzIHlvdSB3ZXJlIG5vdCBnaXZl
biwgZG8gbm90IGhpdCB0aGUgbmV0d29yaywgdGhlCiAgZmlsZXN5c3RlbSwgb3IgYSBkYXRhYmFz
ZSwgYW5kIGRvIG5vdCBpbXBvcnQgYW55dGhpbmcgdGhlIGZpbGUgaXRzZWxmIGRvZXMgbm90Lgot
IElmIHRoZSBmdW5jdGlvbiBuZWVkcyBzaW1wbGUgZml4dHVyZXMsIGJ1aWxkIHRoZW0gaW5saW5l
IGluIHRoZSB0ZXN0LgotIEtlZXAgaXQgdG8gc3RhbmRhcmQgbGlicmFyeSBwbHVzIHB5dGVzdC4g
Tm8gbmV3IGRlcGVuZGVuY2llcy4KCkJld2FyZSB0aGUgdHJhcCBvZiBhIHRlc3QgdGhhdCBwaW5z
IGEgQlVHOiBpZiB0aGUgc291cmNlIGNsZWFybHkgY29udHJhZGljdHMgaXRzCm93biBuYW1lIG9y
IGRvY3N0cmluZyAoZS5nLiBhIGZ1bmN0aW9uIGNhbGxlZCBgaXNfZXF1YWxgIHRoYXQgcmV0dXJu
cyB0aGUKb3Bwb3NpdGUpLCBzdGlsbCB3cml0ZSBhIHRlc3QgdGhhdCBhc3NlcnRzIHRoZSBDT1JS
RUNUIGJlaGF2aW91ciBhbmQgbm90ZSBpdCBpbgpgc3VzcGVjdGVkX2J1Z2AgLSBkbyBub3Qgc2ls
ZW50bHkgZW5jb2RlIHRoZSB3cm9uZyBiZWhhdmlvdXIganVzdCB0byBnbyBncmVlbi4KClJldHVy
biBTVFJJQ1QgSlNPTiBvbmx5LCBubyBwcm9zZSBvdXRzaWRlIGl0Ogp7CiAgInN1bW1hcnkiOiAi
b25lIHNlbnRlbmNlIG9uIHdoYXQgeW91IHRlc3RlZCIsCiAgInRlc3RfZmlsZSI6ICJ0ZXN0L3Vu
aXQvdGVzdF88bW9kdWxlPi5weSIsCiAgInRlc3RfY29kZSI6ICI8YSBjb21wbGV0ZSwgaW1wb3J0
YWJsZSBweXRlc3QgZmlsZSBhcyBhIHNpbmdsZSBzdHJpbmc+IiwKICAiY292ZXJzIjogWyI8ZnVu
Y3Rpb24gbmFtZT4iXSwKICAic3VzcGVjdGVkX2J1ZyI6IG51bGwKfQo="""

LOOP_ARGS_OLD = '''    ap.add_argument("--workspace-path", default=None)
    a = ap.parse_args()'''
LOOP_ARGS_NEW = '''    ap.add_argument("--workspace-path", default=None)
    ap.add_argument("--coverage", action="store_true",
                    help="scan a repo and have the unit_tester agent write tests for the gaps")
    ap.add_argument("--repo", help="the project to scan (with --coverage)")
    ap.add_argument("--path", action="append", default=None,
                    help="limit --coverage to these files/dirs (repeatable)")
    ap.add_argument("--max-functions", type=int, default=None,
                    help="cap how many functions --coverage writes tests for")
    a = ap.parse_args()'''

LOOP_BRANCH_OLD = '''    tx = transport_mod.build("api" if a.api else "stdio")

    if a.draft_context:'''
LOOP_BRANCH_NEW = '''    tx = transport_mod.build("api" if a.api else "stdio")

    if a.coverage:
        import coverage_loop
        result = coverage_loop.run(tx, cfg, a.repo, workbench=str(wb), db=db,
                                   paths=a.path, max_functions=a.max_functions,
                                   say=tx.progress)
        tx._send({"method": "done", "params": result}) if hasattr(tx, "_send") else None
        return 0

    if a.draft_context:'''

GW_EXPORT_OLD = "module.exports = { run, draftContext, runLoop, handle };"
GW_FUNC = r'''
/** Command entry point: write unit tests for a repo's coverage gaps. */
async function coverageWrite(repo) {
  const out = vscode.window.createOutputChannel('Docket');
  out.show(true);

  let cfg;
  try {
    cfg = await config.load();
  } catch (e) {
    out.appendLine(`FAILED: ${e.message}`);
    vscode.window.showErrorMessage(`Docket: ${e.message}`);
    return;
  }
  if (!repo) repo = cfg.projectPath || '';

  const ok = await vscode.window.showWarningMessage(
    `Have the unit_tester agent write tests for untested functions in ${repo}? ` +
    `Each test is kept only if it runs green.`,
    { modal: true }, 'Write tests'
  );
  if (ok !== 'Write tests') return;

  try {
    const result = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: 'Docket: writing tests...' },
      () => runLoop(cfg, ['--coverage', '--repo', repo, '--workbench', cfg.workbench], out)
    );
    if (result) {
      vscode.window.showInformationMessage(
        `Docket coverage ${result.before_coverage}% -> ${result.after_coverage}%, ` +
        `${(result.tests_added || []).length} test(s) added, ` +
        `${(result.skipped || []).length} skipped.`
      );
    }
  } catch (e) {
    out.appendLine(`\nFAILED: ${e.message}`);
    vscode.window.showErrorMessage(`Docket: ${e.message}`);
  }
}

module.exports = { run, draftContext, coverageWrite, runLoop, handle };'''


def _write(rel, b64):
    path = os.path.join(HERE, rel.replace("/", os.sep))
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "wb") as f:
        f.write(base64.b64decode(b64.encode()))
    print("wrote " + rel)


def _patch(path, pairs, guard):
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    if guard in src:
        print("  = " + os.path.basename(path) + " already patched")
        return True
    bak = path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    with open(bak, "w", encoding="utf-8") as f:
        f.write(src)
    for old, new, label in pairs:
        if src.count(old) != 1:
            print("  ! " + label + ": anchor found " + str(src.count(old))
                  + " time(s); leaving " + os.path.basename(path) + " untouched")
            return False
        src = src.replace(old, new)
        print("  + " + label)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print("  (backed up -> " + os.path.basename(bak) + ")")
    return True


def main():
    loop = os.path.join(HERE, "loop.py")
    if not os.path.exists(loop):
        print("add_coverage_cmd: no loop.py here (" + HERE + "). Run it beside loop.py.")
        return 2

    _write("coverage_loop.py", _LOOP_B64)
    _write("agents/unit_tester.md", _AGENT_B64)

    _patch(loop, [
        (LOOP_ARGS_OLD, LOOP_ARGS_NEW, "loop.py: --coverage args"),
        (LOOP_BRANCH_OLD, LOOP_BRANCH_NEW, "loop.py: --coverage branch"),
    ], guard="if a.coverage:")

    gw = None
    for cand in (os.path.join(HERE, "gateway.js"),
                 os.path.join(HERE, "src", "gateway.js")):
        if os.path.exists(cand):
            gw = cand
            break
    if gw:
        _patch(gw, [(GW_EXPORT_OLD, GW_FUNC, "gateway.js: coverageWrite command")],
               guard="async function coverageWrite")
    else:
        print("  ! gateway.js not found (looked in . and src/); add coverageWrite by hand")

    pyc = os.path.join(HERE, "__pycache__")
    if os.path.isdir(pyc):
        for n in os.listdir(pyc):
            if n.startswith(("loop", "coverage_loop", "coverage_tool")):
                try:
                    os.remove(os.path.join(pyc, n))
                except OSError:
                    pass

    print("")
    print("=" * 60)
    ok = True
    for script in ("coverage_loop.py", "coverage_tool.py", "loop.py"):
        if not os.path.exists(os.path.join(HERE, script)):
            continue
        print("$ python " + script + " --self-test")
        r = subprocess.run([sys.executable, script, "--self-test"], cwd=HERE,
                           capture_output=True, text=True)
        out = (r.stdout or "") + (r.stderr or "")
        for ln in out.splitlines():
            if ("passed" in ln or "FAIL" in ln or "Error" in ln
                    or "Traceback" in ln or "XX" in ln):
                print("  " + ln)
        if r.returncode != 0:
            ok = False
    print("=" * 60)
    if ok:
        print("Done. loop.py --coverage is wired and the gateway exposes coverageWrite.")
        print("Reload the VS Code window; 'Docket: Scan Coverage' now offers")
        print("'Write tests' after a scan.")
    else:
        print("A self-test reported a problem above; the .bak-* files hold the")
        print("originals. Send me the failing lines.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
