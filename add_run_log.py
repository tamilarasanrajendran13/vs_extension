#!/usr/bin/env python3
"""
add_run_log - wire per-run channel logging into the pipeline.

Every run already prints a stage-by-stage account to the VS Code channel via
say()/tx.progress, but nothing saves it. This writes run_log.py and patches
loop.py (additively, idempotently, with a backup) so that each run ALSO writes
that output to a timestamped file under the ticket's evidence/ folder, recorded
as an artifact - visible on the dashboard, attachable to Jira - without bloating
the ledger.

Run once, from the folder that holds loop.py:

    python add_run_log.py

Re-running is safe (the loop.py edits are skipped if already present). It runs
run_log.py --self-test and loop.py --self-test at the end.
"""

import base64
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

_RUN_LOG_B64 = """\
IyEvdXNyL2Jpbi9lbnYgcHl0aG9uMwoiIiIKcnVuX2xvZyAtIGNhcHR1cmUgYSBydW4ncyBjaGFu
bmVsIG91dHB1dCB0byBhIHBlci1ydW4gZXZpZGVuY2UgbG9nIGZpbGUuCgpFdmVyeSBsaW5lIHRo
ZSBoYXJuZXNzIHByaW50cyB0byB0aGUgVlMgQ29kZSBjaGFubmVsICh2aWEgdHgucHJvZ3Jlc3Mg
LyBzYXkpIGlzCmFsc28gd3JpdHRlbiBoZXJlLCB0aW1lc3RhbXBlZC4gU28gZWFjaCBydW4gbGVh
dmVzIGEgc3RhZ2UtYnktc3RhZ2UsIGxpbmUtYnktbGluZQphY2NvdW50IG9uIGRpc2s6CgogIC0g
cmVjb3JkZWQgYXMgYW4gZXZpZGVuY2UgYXJ0aWZhY3QgKHNob3dzIG9uIHRoZSBkYXNoYm9hcmQg
KyBydW4gZHJpbGwtZG93biksCiAgLSBhdHRhY2hhYmxlIHRvIHRoZSBKaXJhIHRpY2tldCBhbG9u
Z3NpZGUgdGhlIG90aGVyIGV2aWRlbmNlLAogIC0gYW5kIGl0IGRvZXMgTk9UIGJsb2F0IHRoZSBs
ZWRnZXIgb3IgdGhlIGRhc2hib2FyZCAtIHRob3NlIG9ubHkgbGluayB0byBpdCwKICAgIHdoaWNo
IHdhcyB0aGUgd2hvbGUgcG9pbnQ6IHRoZSBkZXRhaWwgbGl2ZXMgaW4gYSBmaWxlLCBrZXllZCBi
eSBydW4gYW5kIHRpbWUuCgpGaWxlcyBsYW5kIGF0OiAgPHdvcmtzcGFjZT4vZXZpZGVuY2UvcnVu
LTxydW5faWQ+LTxZWVlZTU1ERC1ISE1NU1M+LmxvZwpzbyBydW5zIG9mIHRoZSBzYW1lIHRpY2tl
dCwgYW5kIGl0ZXJhdGlvbnMgYWNyb3NzIGRheXMsIGFyZSB0b2xkIGFwYXJ0IGJ5IG5hbWUuCgpT
ZWxmLXRlc3QgKHN0ZGxpYiBvbmx5KTogIHB5dGhvbiBydW5fbG9nLnB5IC0tc2VsZi10ZXN0CiIi
IgoKZnJvbSBfX2Z1dHVyZV9fIGltcG9ydCBhbm5vdGF0aW9ucwoKaW1wb3J0IGFyZ3BhcnNlCmlt
cG9ydCBkYXRldGltZQppbXBvcnQgc3lzCmZyb20gcGF0aGxpYiBpbXBvcnQgUGF0aAoKX0JBUiA9
ICI9IiAqIDYwCgoKY2xhc3MgUnVuTG9nOgogICAgZGVmIF9faW5pdF9fKHNlbGYsIGZoLCBwYXRo
LCByZWxfcGF0aCk6CiAgICAgICAgc2VsZi5fZmggPSBmaAogICAgICAgIHNlbGYucGF0aCA9IHBh
dGgKICAgICAgICBzZWxmLnJlbF9wYXRoID0gcmVsX3BhdGgKICAgICAgICBzZWxmLl9jbG9zZWQg
PSBGYWxzZQoKICAgIGRlZiB3cml0ZShzZWxmLCB0ZXh0PSIiKToKICAgICAgICAiIiJXcml0ZSBv
bmUgc2F5KCkgY2FsbCwgdGltZXN0YW1wZWQgcGVyIG5vbi1lbXB0eSBsaW5lLiIiIgogICAgICAg
IGlmIHNlbGYuX2Nsb3NlZDoKICAgICAgICAgICAgcmV0dXJuCiAgICAgICAgdHMgPSBkYXRldGlt
ZS5kYXRldGltZS5ub3coKS5zdHJmdGltZSgiJUg6JU06JVMiKQogICAgICAgIHMgPSAiIiBpZiB0
ZXh0IGlzIE5vbmUgZWxzZSBzdHIodGV4dCkKICAgICAgICBpZiBzID09ICIiOgogICAgICAgICAg
ICBzZWxmLl9maC53cml0ZSgiXG4iKQogICAgICAgIGVsc2U6CiAgICAgICAgICAgIGZvciBsaW5l
IGluIHMuc3BsaXQoIlxuIik6CiAgICAgICAgICAgICAgICBpZiBsaW5lLnN0cmlwKCkgPT0gIiI6
CiAgICAgICAgICAgICAgICAgICAgc2VsZi5fZmgud3JpdGUoIlxuIikKICAgICAgICAgICAgICAg
IGVsc2U6CiAgICAgICAgICAgICAgICAgICAgc2VsZi5fZmgud3JpdGUodHMgKyAiICAiICsgbGlu
ZSArICJcbiIpCiAgICAgICAgc2VsZi5fZmx1c2goKQoKICAgIGRlZiBzdGFnZShzZWxmLCBuYW1l
KToKICAgICAgICAiIiJPcHRpb25hbCBleHBsaWNpdCBzdGFnZSBiYW5uZXIsIGlmIHRoZSBjYWxs
ZXIgd2FudHMgY2xlYXIgc2VjdGlvbnMuIiIiCiAgICAgICAgaWYgc2VsZi5fY2xvc2VkOgogICAg
ICAgICAgICByZXR1cm4KICAgICAgICB0cyA9IGRhdGV0aW1lLmRhdGV0aW1lLm5vdygpLnN0cmZ0
aW1lKCIlSDolTTolUyIpCiAgICAgICAgc2VsZi5fZmgud3JpdGUoIlxuIiArIF9CQVIgKyAiXG4i
ICsgdHMgKyAiICBTVEFHRTogIiArIHN0cihuYW1lKS51cHBlcigpCiAgICAgICAgICAgICAgICAg
ICAgICAgKyAiXG4iICsgX0JBUiArICJcbiIpCiAgICAgICAgc2VsZi5fZmx1c2goKQoKICAgIGRl
ZiBjbG9zZShzZWxmKToKICAgICAgICBpZiBzZWxmLl9jbG9zZWQ6CiAgICAgICAgICAgIHJldHVy
bgogICAgICAgIHNlbGYuX2Nsb3NlZCA9IFRydWUKICAgICAgICB0cnk6CiAgICAgICAgICAgIHNl
bGYuX2ZoLndyaXRlKCJcbiIgKyAoIi0iICogNjApICsgIlxucnVuIGxvZyBjbG9zZWQgIgogICAg
ICAgICAgICAgICAgICAgICAgICAgICArIGRhdGV0aW1lLmRhdGV0aW1lLm5vdygpLmlzb2Zvcm1h
dCh0aW1lc3BlYz0ic2Vjb25kcyIpICsgIlxuIikKICAgICAgICAgICAgc2VsZi5fZmguY2xvc2Uo
KQogICAgICAgIGV4Y2VwdCBFeGNlcHRpb246CiAgICAgICAgICAgIHBhc3MKCiAgICBkZWYgX2Zs
dXNoKHNlbGYpOgogICAgICAgIHRyeToKICAgICAgICAgICAgc2VsZi5fZmguZmx1c2goKQogICAg
ICAgIGV4Y2VwdCBFeGNlcHRpb246CiAgICAgICAgICAgIHBhc3MKCgpkZWYgX3NhZmUocyk6CiAg
ICByZXR1cm4gIiIuam9pbihjIGlmIChjLmlzYWxudW0oKSBvciBjIGluICItXy4iKSBlbHNlICIt
IiBmb3IgYyBpbiBzdHIocykpCgoKZGVmIG9wZW5fZm9yKHdzLCBydW5faWQsIHRpY2tldF9pZCwg
cHJvamVjdD1Ob25lLCByZWxlYXNlPU5vbmUpOgogICAgIiIiT3BlbiBhIHBlci1ydW4gbG9nIHVu
ZGVyIDx3cz4vZXZpZGVuY2UvIGFuZCB3cml0ZSB0aGUgaGVhZGVyLgoKICAgIE5ldmVyIHJhaXNl
cyBmb3IgYSBsb2dnaW5nIHJlYXNvbiB0aGUgY2FsbGVyIGNhbm5vdCByZWNvdmVyIGZyb20gLSBv
biBhbnkKICAgIGZpbGVzeXN0ZW0gdHJvdWJsZSBpdCByZXR1cm5zIGEgbm8tb3Agc2luayBzbyBh
IHJ1biBpcyBuZXZlciBibG9ja2VkIGJ5IGl0cwogICAgb3duIGxvZy4KICAgICIiIgogICAgdHJ5
OgogICAgICAgIGV2aWRlbmNlID0gUGF0aCh3cykgLyAiZXZpZGVuY2UiCiAgICAgICAgZXZpZGVu
Y2UubWtkaXIocGFyZW50cz1UcnVlLCBleGlzdF9vaz1UcnVlKQogICAgICAgIHN0YW1wID0gZGF0
ZXRpbWUuZGF0ZXRpbWUubm93KCkuc3RyZnRpbWUoIiVZJW0lZC0lSCVNJVMiKQogICAgICAgIG5h
bWUgPSAicnVuLXt9LXt9LmxvZyIuZm9ybWF0KF9zYWZlKHJ1bl9pZCksIHN0YW1wKQogICAgICAg
IHBhdGggPSBldmlkZW5jZSAvIG5hbWUKICAgICAgICBmaCA9IG9wZW4ocGF0aCwgInciLCBlbmNv
ZGluZz0idXRmLTgiKQogICAgICAgIGhlYWRlciA9IFsKICAgICAgICAgICAgX0JBUiwgIkRPQ0tF
VCBSVU4gTE9HIiwKICAgICAgICAgICAgInRpY2tldCA6IHt9Ii5mb3JtYXQodGlja2V0X2lkKSwK
ICAgICAgICAgICAgInJ1biAgICA6IHt9Ii5mb3JtYXQocnVuX2lkKSwKICAgICAgICAgICAgInBy
b2plY3Q6IHt9Ii5mb3JtYXQocHJvamVjdCBvciAiLSIpLAogICAgICAgICAgICAicmVsZWFzZTog
e30iLmZvcm1hdChyZWxlYXNlIG9yICItIiksCiAgICAgICAgICAgICJzdGFydGVkOiB7fSIuZm9y
bWF0KGRhdGV0aW1lLmRhdGV0aW1lLm5vdygpLmlzb2Zvcm1hdCh0aW1lc3BlYz0ic2Vjb25kcyIp
KSwKICAgICAgICAgICAgX0JBUiwgIiIsCiAgICAgICAgXQogICAgICAgIGZoLndyaXRlKCJcbiIu
am9pbihoZWFkZXIpICsgIlxuIikKICAgICAgICBmaC5mbHVzaCgpCiAgICAgICAgcmV0dXJuIFJ1
bkxvZyhmaCwgc3RyKHBhdGgpLCAiZXZpZGVuY2UvIiArIG5hbWUpCiAgICBleGNlcHQgRXhjZXB0
aW9uOgogICAgICAgIHJldHVybiBfTnVsbExvZygpCgoKY2xhc3MgX051bGxMb2c6CiAgICAiIiJB
IHNpbmsgdXNlZCBvbmx5IGlmIHRoZSByZWFsIGxvZyBjb3VsZCBub3QgYmUgb3BlbmVkOyBuZXZl
ciBibG9ja3MgYSBydW4uIiIiCiAgICByZWxfcGF0aCA9IE5vbmUKICAgIHBhdGggPSBOb25lCgog
ICAgZGVmIHdyaXRlKHNlbGYsIHRleHQ9IiIpOgogICAgICAgIHBhc3MKCiAgICBkZWYgc3RhZ2Uo
c2VsZiwgbmFtZSk6CiAgICAgICAgcGFzcwoKICAgIGRlZiBjbG9zZShzZWxmKToKICAgICAgICBw
YXNzCgoKZGVmIHRlZShwcm9ncmVzc19mbiwgcmxvZyk6CiAgICAiIiJXcmFwIGEgcHJvZ3Jlc3Mv
c2F5IGZ1bmN0aW9uIHNvIGV2ZXJ5IGNhbGwgaXMgQUxTTyB3cml0dGVuIHRvIHRoZSBsb2cuCgog
ICAgTG9nZ2luZyBmYWlsdXJlcyBhcmUgc3dhbGxvd2VkIC0gdGhlIGNoYW5uZWwgb3V0cHV0IG11
c3QgbmV2ZXIgYnJlYWsgYmVjYXVzZQogICAgYSBsb2cgd3JpdGUgZmFpbGVkLgogICAgIiIiCiAg
ICBkZWYgc2F5KHRleHQ9IiIpOgogICAgICAgIHRyeToKICAgICAgICAgICAgcmxvZy53cml0ZSh0
ZXh0KQogICAgICAgIGV4Y2VwdCBFeGNlcHRpb246CiAgICAgICAgICAgIHBhc3MKICAgICAgICBp
ZiBwcm9ncmVzc19mbiBpcyBub3QgTm9uZToKICAgICAgICAgICAgcmV0dXJuIHByb2dyZXNzX2Zu
KHRleHQpCiAgICByZXR1cm4gc2F5CgoKIyA9PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09
PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PSBzZWxmLXRlc3QKCmRlZiBfc2Vs
Zl90ZXN0KCk6CiAgICBpbXBvcnQgdGVtcGZpbGUKCiAgICBjaGVja3MgPSBbXQoKICAgIGRlZiBv
ayhuYW1lLCBjb25kKToKICAgICAgICBjaGVja3MuYXBwZW5kKChuYW1lLCBib29sKGNvbmQpKSkK
CiAgICB3aXRoIHRlbXBmaWxlLlRlbXBvcmFyeURpcmVjdG9yeSgpIGFzIHRkOgogICAgICAgIHdz
ID0gUGF0aCh0ZCkgLyAiZGV2ZWxvcG1lbnQiIC8gIlIxIiAvICJPVC0xIgoKICAgICAgICBybCA9
IG9wZW5fZm9yKHdzLCAiT1QtMS1ydW4tMyIsICJPVC0xIiwgcHJvamVjdD0ib25ldGVzdCIsIHJl
bGVhc2U9IlIxIikKICAgICAgICBvaygibG9nIGZpbGUgY3JlYXRlZCB1bmRlciBldmlkZW5jZS8i
LCBQYXRoKHJsLnBhdGgpLmV4aXN0cygpKQogICAgICAgIG9rKCJyZWxfcGF0aCBwb2ludHMgaW50
byBldmlkZW5jZS8iLCBybC5yZWxfcGF0aC5zdGFydHN3aXRoKCJldmlkZW5jZS9ydW4tT1QtMS1y
dW4tMy0iKSkKICAgICAgICBvaygicmVsX3BhdGggZW5kcyAubG9nIiwgcmwucmVsX3BhdGguZW5k
c3dpdGgoIi5sb2ciKSkKCiAgICAgICAgIyB0ZWUgYm90aCB0byBhIGNhcHR1cmVkIGNoYW5uZWwg
YW5kIHRoZSBmaWxlCiAgICAgICAgc2VlbiA9IFtdCiAgICAgICAgc2F5ID0gdGVlKGxhbWJkYSB0
PSIiOiBzZWVuLmFwcGVuZCh0KSwgcmwpCiAgICAgICAgc2F5KCJsZWFkIGRlY2xhcmluZyB0aGUg
Ymxhc3QgcmFkaXVzLi4uIikKICAgICAgICBzYXkoIiIpICAgICAgICAgICAgICAgICAgICAgICAj
IGJsYW5rIGxpbmUKICAgICAgICBzYXkoIiAgTUFZIHRvdWNoICgyKTpcbiAgICBbZmlsZV0gc3Jj
L2EucHlcbiAgICBbZmlsZV0gc3JjL2IucHkiKQogICAgICAgIHJsLnN0YWdlKCJkZXZlbG9wIikK
ICAgICAgICBzYXkoImRldmVsb3BlciB3cml0aW5nIGNvZGUuLi4iKQogICAgICAgIHJsLmNsb3Nl
KCkKCiAgICAgICAgdGV4dCA9IFBhdGgocmwucGF0aCkucmVhZF90ZXh0KGVuY29kaW5nPSJ1dGYt
OCIpCiAgICAgICAgb2soImNoYW5uZWwgc3RpbGwgcmVjZWl2ZWQgb3V0cHV0Iiwgc2VlblswXSA9
PSAibGVhZCBkZWNsYXJpbmcgdGhlIGJsYXN0IHJhZGl1cy4uLiIpCiAgICAgICAgb2soImhlYWRl
ciBuYW1lcyB0aGUgdGlja2V0ICsgcnVuIiwgInRpY2tldCA6IE9ULTEiIGluIHRleHQgYW5kICJy
dW4gICAgOiBPVC0xLXJ1bi0zIiBpbiB0ZXh0KQogICAgICAgIG9rKCJsaW5lcyBhcmUgdGltZXN0
YW1wZWQiLCBhbnkobFs6OF0uY291bnQoIjoiKSA9PSAyIGFuZCAibGVhZCBkZWNsYXJpbmciIGlu
IGwKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIGZvciBsIGluIHRleHQu
c3BsaXRsaW5lcygpKSkKICAgICAgICBvaygibXVsdGktbGluZSBzYXkgcHJlc2VydmVkIiwgInNy
Yy9hLnB5IiBpbiB0ZXh0IGFuZCAic3JjL2IucHkiIGluIHRleHQpCiAgICAgICAgb2soInN0YWdl
IGJhbm5lciB3cml0dGVuIiwgIlNUQUdFOiBERVZFTE9QIiBpbiB0ZXh0KQogICAgICAgIG9rKCJj
bG9zZXMgY2xlYW5seSIsICJydW4gbG9nIGNsb3NlZCIgaW4gdGV4dCkKICAgICAgICBvaygid3Jp
dGUgYWZ0ZXIgY2xvc2UgaXMgYSBuby1vcCIsIChybC53cml0ZSgibGF0ZSIpLCAibGF0ZSIgbm90
IGluCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgUGF0aChybC5w
YXRoKS5yZWFkX3RleHQoZW5jb2Rpbmc9InV0Zi04IikpWzFdKQoKICAgICAgICAjIGEgYmFkIHBh
dGggbXVzdCBkZWdyYWRlIHRvIGEgbnVsbCBzaW5rLCBuZXZlciByYWlzZQogICAgICAgIGJhZCA9
IG9wZW5fZm9yKCIvbm9uZXhpc3RlbnRceDAwL3giLCAiUiIsICJUIikKICAgICAgICB0cnk6CiAg
ICAgICAgICAgIHMyID0gdGVlKGxhbWJkYSB0PSIiOiBOb25lLCBiYWQpCiAgICAgICAgICAgIHMy
KCJzdGlsbCBmaW5lIikKICAgICAgICAgICAgYmFkLmNsb3NlKCkKICAgICAgICAgICAgb2soImJh
ZCBwYXRoIC0+IG51bGwgc2luaywgbm8gcmFpc2UiLCBUcnVlKQogICAgICAgIGV4Y2VwdCBFeGNl
cHRpb246CiAgICAgICAgICAgIG9rKCJiYWQgcGF0aCAtPiBudWxsIHNpbmssIG5vIHJhaXNlIiwg
RmFsc2UpCgogICAgcGFzc2VkID0gc3VtKDEgZm9yIF8sIGMgaW4gY2hlY2tzIGlmIGMpCiAgICBm
b3IgbmFtZSwgYyBpbiBjaGVja3M6CiAgICAgICAgcHJpbnQoIiAgW3t9XSB7fSIuZm9ybWF0KCJv
ayAiIGlmIGMgZWxzZSAiWFgiLCBuYW1lKSkKICAgIHByaW50KCJcbnt9L3t9IGNoZWNrcyBwYXNz
ZWQiLmZvcm1hdChwYXNzZWQsIGxlbihjaGVja3MpKSkKICAgIHJldHVybiBwYXNzZWQgPT0gbGVu
KGNoZWNrcykKCgpkZWYgbWFpbihhcmd2PU5vbmUpOgogICAgYXAgPSBhcmdwYXJzZS5Bcmd1bWVu
dFBhcnNlcihkZXNjcmlwdGlvbj0iRG9ja2V0IHBlci1ydW4gY2hhbm5lbCBsb2ciKQogICAgYXAu
YWRkX2FyZ3VtZW50KCItLXNlbGYtdGVzdCIsIGFjdGlvbj0ic3RvcmVfdHJ1ZSIpCiAgICBhcmdz
ID0gYXAucGFyc2VfYXJncyhhcmd2KQogICAgaWYgYXJncy5zZWxmX3Rlc3Q6CiAgICAgICAgc3lz
LmV4aXQoMCBpZiBfc2VsZl90ZXN0KCkgZWxzZSAxKQogICAgYXAucHJpbnRfaGVscCgpCgoKaWYg
X19uYW1lX18gPT0gIl9fbWFpbl9fIjoKICAgIG1haW4oKQo=\n"""

A1_OLD = '''    )
    say(f"run {run_id}")'''

A1_NEW = '''    )

    # Capture every channel line to a per-run evidence log (run_log.py). Wrapping
    # say here means all stages are logged with no change to any stage.
    _rlog = None
    try:
        import run_log as _run_log
        _rlog = _run_log.open_for(ws, run_id, ticket_id, project=project, release=release)
        say = _run_log.tee(tx.progress, _rlog)
    except Exception:
        pass  # logging must never block a run

    say(f"run {run_id}")'''

A2_OLD = '''        raise


def _self_test() -> int:'''

A2_NEW = '''        raise
    finally:
        # Close and record the per-run log as an evidence artifact (best-effort).
        try:
            if _rlog is not None and getattr(_rlog, "rel_path", None):
                _rlog.close()
                ledger.record_artifact(run_id, ticket_id, "evidence", _rlog.rel_path,
                                       workspace_path=str(ws), actor="system", db=db)
        except Exception:
            pass


def _self_test() -> int:'''


def main():
    loop = os.path.join(HERE, "loop.py")
    if not os.path.exists(loop):
        print("add_run_log: no loop.py in this folder (" + HERE + ").")
        print("  Put add_run_log.py beside loop.py and run it there.")
        return 2

    # 1. write run_log.py
    with open(os.path.join(HERE, "run_log.py"), "wb") as f:
        f.write(base64.b64decode(_RUN_LOG_B64.encode()))
    print("wrote run_log.py")

    # 2. patch loop.py additively
    with open(loop, "r", encoding="utf-8") as f:
        src = f.read()
    bak = loop + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    with open(bak, "w", encoding="utf-8") as f:
        f.write(src)
    print("backed up loop.py -> " + os.path.basename(bak))

    changed = False
    if "_run_log.open_for" not in src:
        if src.count(A1_OLD) == 1:
            src = src.replace(A1_OLD, A1_NEW); changed = True
            print("  + wrapped say() to capture the run log")
        else:
            print("  ! could not find the say-setup point (" + str(src.count(A1_OLD))
                  + " matches); left loop.py untouched.")
            return 3
    else:
        print("  = say() capture already present")

    if "if _rlog is not None and getattr(_rlog" not in src:
        if src.count(A2_OLD) == 1:
            src = src.replace(A2_OLD, A2_NEW); changed = True
            print("  + added the finally that records the log artifact")
        else:
            print("  ! could not find the run_ticket finally point (" + str(src.count(A2_OLD))
                  + " matches); left loop.py partially patched - restore from the .bak.")
            return 3
    else:
        print("  = log artifact finally already present")

    if changed:
        with open(loop, "w", encoding="utf-8") as f:
            f.write(src)

    pyc = os.path.join(HERE, "__pycache__")
    if os.path.isdir(pyc):
        for n in os.listdir(pyc):
            if n.startswith(("loop", "run_log")):
                try:
                    os.remove(os.path.join(pyc, n))
                except OSError:
                    pass

    # 3. verify
    print("")
    print("=" * 60)
    ok = True
    for script in ("run_log.py", "loop.py"):
        print("$ python " + script + " --self-test")
        r = subprocess.run([sys.executable, script, "--self-test"], cwd=HERE,
                           capture_output=True, text=True)
        out = (r.stdout or "") + (r.stderr or "")
        for ln in out.splitlines():
            if ("checks passed" in ln or "FAIL" in ln or "self-test" in ln
                    or "Error" in ln or "Traceback" in ln or "XX" in ln):
                print("  " + ln)
        if r.returncode != 0:
            ok = False
    print("=" * 60)
    if ok:
        print("Done. Every run now writes evidence/run-<id>-<time>.log, recorded")
        print("as an artifact. Rebuild the dashboard to see it on the Artifacts tab.")
    else:
        print("A self-test reported a problem above. loop.py.bak-* holds the original;")
        print("send me the failing lines and I will adjust.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
