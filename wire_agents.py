#!/usr/bin/env python3
"""
wire_agents - fill in the Agents tab roster.

The Agents tab shows every agent that has logged an event, and pulls each
agent's description from AGENT_INFO inside payload_builder.py. Agents that ran
but are not in AGENT_INFO show blank; agents that have not run yet do not show
at all. This wires in a fuller roster without you editing payload_builder.py by
hand:

  1. writes agent_info.py beside payload_builder.py (the full roster of
     descriptions - you edit ONLY this file to add or fix agents later)
  2. patches payload_builder.py, additively and idempotently, to:
       - merge agent_info.py over its built-in AGENT_INFO at import time
       - seed the roster with every described agent, so the whole cast shows
         even before an agent has run (renders with 0 calls)
  3. backs up payload_builder.py first, then runs --self-test and --doctor

Run once, from the docket/ folder:

    python wire_agents.py
    python wire_agents.py --db ledger.db     # if your ledger is elsewhere

Re-running is safe: the payload_builder patches are skipped if already present,
and agent_info.py is rewritten from this script. After it runs, open the Agents
tab. If a card still says "no description on file", read the role off it and add
a matching key to agent_info.py.
"""

import argparse
import base64
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

_AGENT_INFO_B64 = """\
IyEvdXNyL2Jpbi9lbnYgcHl0aG9uMwoiIiIKYWdlbnRfaW5mbyAtIHdoYXQgZWFjaCBhZ2VudCBp
cyBGT1IsIGtlcHQgb3V0IG9mIHBheWxvYWRfYnVpbGRlci5weS4KCnBheWxvYWRfYnVpbGRlci5w
eSBtZXJnZXMgdGhpcyBvdmVyIGl0cyBidWlsdC1pbiBBR0VOVF9JTkZPIGF0IGltcG9ydCB0aW1l
LCBzbwp0aGUgQWdlbnRzIHRhYiBjYW4gYmUga2VwdCBjdXJyZW50IGJ5IGVkaXRpbmcgT05MWSB0
aGlzIGZpbGUgLSBubyBuZWVkIHRvIHRvdWNoCnBheWxvYWRfYnVpbGRlci5weSAoYW5kIGl0cyBD
T05UUkFDVCkgYWdhaW4uCgpUaGUga2V5IGlzIHRoZSBhZ2VudCdzIHJvbGUgZXhhY3RseSBhcyBp
dCBhcHBlYXJzIGluIHRoZSBsZWRnZXIncyBldmVudHMuYWN0b3IKY29sdW1uLCBsb3dlci1jYXNl
ZC4gSWYgYSBjYXJkIG9uIHRoZSBBZ2VudHMgdGFiIHN0aWxsIHNheXMgIm5vIGRlc2NyaXB0aW9u
IG9uCmZpbGUiLCByZWFkIHRoZSByb2xlIG9mZiB0aGF0IGNhcmQgYW5kIGFkZCBhIG1hdGNoaW5n
IGtleSBoZXJlLiBJZiB5b3Ugc2VlIHR3bwpjYXJkcyBmb3IgdGhlIHNhbWUgYWdlbnQgKG9uZSBk
ZXNjcmliZWQgd2l0aCAwIGNhbGxzLCBvbmUgd2l0aCBzdGF0cyBidXQgbm8KZGVzY3JpcHRpb24p
LCB0aGUga2V5IGhlcmUgZG9lcyBub3QgbWF0Y2ggdGhlIGxlZGdlcidzIGFjdG9yIHN0cmluZyAt
IHJlbmFtZSB0aGUKa2V5IHRvIG1hdGNoLgoiIiIKCkFHRU5UX0lORk8gPSB7CiAgICAjIC0tLS0g
Y29tcHJlaGVuc2lvbiAtLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0t
LS0tLS0tLS0tCiAgICAiamlyYSI6IHsKICAgICAgICAidGl0bGUiOiAiSmlyYSBhZ2VudCIsCiAg
ICAgICAgImRvZXMiOiAiVGFsa3MgdG8gSmlyYTogZmV0Y2hlcyB0aGUgdGlja2V0IGFuZCBhY2Nl
cHRhbmNlIGNyaXRlcmlhLCAiCiAgICAgICAgICAgICAgICAicG9zdHMgdGhlIHNwZWMgYWdlbnQn
cyBjbGFyaWZ5aW5nIHF1ZXN0aW9ucyBiYWNrIHRvIHRoZSBhdXRob3IsICIKICAgICAgICAgICAg
ICAgICJhbmQgcmVhZHMgdGhlIHJlcGxpZXMuIiwKICAgICAgICAic3RhZ2UiOiAiY29tcHJlaGVu
c2lvbiIsCiAgICAgICAgInJlYWRzIjogIkppcmEgQVBJICh0aWNrZXQsIGFjY2VwdGFuY2UgY3Jp
dGVyaWEpIiwKICAgICAgICAid3JpdGVzIjogInRpY2tldCBkYXRhLCBhdXRob3Igcm91bmQtdHJp
cHMiLAogICAgfSwKICAgICJzcGVjIjogewogICAgICAgICJ0aXRsZSI6ICJTcGVjIGFnZW50IiwK
ICAgICAgICAiZG9lcyI6ICJSZWFkcyB0aGUgSmlyYSB0aWNrZXQgYW5kIGp1ZGdlcyB3aGV0aGVy
IGl0IGNhbiBiZSBidWlsdCBmcm9tLiAiCiAgICAgICAgICAgICAgICAiUnVucyB0aGUgY29tcHJl
aGVuc2lvbiBnYXRlIChzcGVjQDEwKSwgcG9zdHMgY2xhcmlmeWluZyAiCiAgICAgICAgICAgICAg
ICAicXVlc3Rpb25zIGJhY2sgdG8gdGhlIGF1dGhvciwgYW5kIGNsYXNzaWZpZXMgYmxvY2tlcnMu
IiwKICAgICAgICAic3RhZ2UiOiAiY29tcHJlaGVuc2lvbiIsCiAgICAgICAgInJlYWRzIjogIkpp
cmEgdGlja2V0LCBhY2NlcHRhbmNlIGNyaXRlcmlhIiwKICAgICAgICAid3JpdGVzIjogImNvbXBy
ZWhlbnNpb24ubWQsIGF1dGhvciBxdWVzdGlvbnMiLAogICAgfSwKICAgICMgLS0tLSBjb250ZXh0
IC0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0t
LS0KICAgICJjYXJ0b2dyYXBoZXIiOiB7CiAgICAgICAgInRpdGxlIjogIkNhcnRvZ3JhcGhlciIs
CiAgICAgICAgImRvZXMiOiAiRXhwbG9yZXMgdGhlIHJlcG9zaXRvcnkgd2l0aCBncmVwL2xpc3Qv
cmVhZCB0b29scyB0byBtYXAgdGhlICIKICAgICAgICAgICAgICAgICJjb2RlIGFyb3VuZCB0aGUg
dGlja2V0LiBCdWlsZHMgdGhlIGRvc3NpZXIgdGhlIHJlc3Qgb2YgdGhlICIKICAgICAgICAgICAg
ICAgICJwaXBlbGluZSByZWFzb25zIG92ZXIuIiwKICAgICAgICAic3RhZ2UiOiAiY29udGV4dCIs
CiAgICAgICAgInJlYWRzIjogInJlcG9zaXRvcnkgKHJlYWQtb25seSB0b29scykiLAogICAgICAg
ICJ3cml0ZXMiOiAiZG9zc2llciAvIHJlcG8gbWFwIiwKICAgIH0sCiAgICAiZHJhZnRlciI6IHsK
ICAgICAgICAidGl0bGUiOiAiQ29udGV4dCBkcmFmdGVyIiwKICAgICAgICAiZG9lcyI6ICJUdXJu
cyB0aGUgY2FydG9ncmFwaGVyJ3MgZmluZGluZ3MgaW50byBhIHJhdGlmaWVkIGNvbnRleHQgIgog
ICAgICAgICAgICAgICAgImRvY3VtZW50LiBSZXF1aXJlcyBodW1hbiBzaWduLW9mZiBiZWZvcmUg
dGhlIHBsYW4gaXMgYnVpbHQuIiwKICAgICAgICAic3RhZ2UiOiAiY29udGV4dCIsCiAgICAgICAg
InJlYWRzIjogImRvc3NpZXIiLAogICAgICAgICJ3cml0ZXMiOiAiY29udGV4dC5tZCAoaHVtYW4t
cmF0aWZpZWQpIiwKICAgIH0sCiAgICAibGVhZCI6IHsKICAgICAgICAidGl0bGUiOiAiTGVhZCBh
Z2VudCIsCiAgICAgICAgImRvZXMiOiAiRGVjbGFyZXMgdGhlIGJsYXN0IHJhZGl1cyAtIHRoZSBm
aWxlcyBhbmQgYm91bmRhcmllcyBhIGNoYW5nZSAiCiAgICAgICAgICAgICAgICAibWF5IHRvdWNo
IC0gdmVyaWZpZWQgYWdhaW5zdCB0aGUgZmlsZXN5c3RlbS4gT24gYSBzcGxpdCB0aWNrZXQgIgog
ICAgICAgICAgICAgICAgIml0IGFsc28gY29vcmRpbmF0ZXMgdGhlIHdvcmtlcnMgYW5kIGNvYWNo
ZXMgYSBmYWlsaW5nIHNsaWNlLiIsCiAgICAgICAgInN0YWdlIjogImNvbnRleHQiLAogICAgICAg
ICJyZWFkcyI6ICJjb250ZXh0Lm1kLCBmaWxlc3lzdGVtIiwKICAgICAgICAid3JpdGVzIjogImJs
YXN0IHJhZGl1cywgc2xpY2UgYXNzaWdubWVudHMsIGNvYWNoaW5nIiwKICAgIH0sCiAgICAicGFy
dGl0aW9uZXIiOiB7CiAgICAgICAgInRpdGxlIjogIlBhcnRpdGlvbmVyIiwKICAgICAgICAiZG9l
cyI6ICJEZWNpZGVzIHdoZXRoZXIgYSB0aWNrZXQgc3BsaXRzIGludG8gaW5kZXBlbmRlbnQgc2xp
Y2VzLCBhbmQgIgogICAgICAgICAgICAgICAgImhvdy4gT25seSBzcGxpdHMgd2hlbiB0aGUgc2xp
Y2VzIGdlbnVpbmVseSBkbyBub3QgdG91Y2ggZWFjaCAiCiAgICAgICAgICAgICAgICAib3RoZXI7
IG90aGVyd2lzZSB0aGUgdGlja2V0IHN0YXlzIGEgc2luZ2xlIHN0cmVhbS4iLAogICAgICAgICJz
dGFnZSI6ICJjb250ZXh0IiwKICAgICAgICAicmVhZHMiOiAiYmxhc3QgcmFkaXVzLCBwbGFuIiwK
ICAgICAgICAid3JpdGVzIjogInNsaWNlIHBsYW4iLAogICAgfSwKICAgICMgLS0tLSBwbGFuIC0t
LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0t
LS0KICAgICJwbGFubmVyIjogewogICAgICAgICJ0aXRsZSI6ICJQbGFubmVyIiwKICAgICAgICAi
ZG9lcyI6ICJQcm9kdWNlcyB0aGUgaW1wbGVtZW50YXRpb24gcGxhbi4gQ2FuIHJ1biBhIGJsaW5k
IGJha2Utb2ZmIC0gIgogICAgICAgICAgICAgICAgInNldmVyYWwgcGxhbnMgZ2VuZXJhdGVkIGFu
ZCBqdWRnZWQgd2l0aG91dCBrbm93aW5nIHdoaWNoIGlzICIKICAgICAgICAgICAgICAgICJ3aGlj
aC4iLAogICAgICAgICJzdGFnZSI6ICJwbGFuIiwKICAgICAgICAicmVhZHMiOiAiY29udGV4dC5t
ZCwgYWNjZXB0YW5jZSBjcml0ZXJpYSIsCiAgICAgICAgIndyaXRlcyI6ICJwbGFuLm1kIiwKICAg
IH0sCiAgICAianVkZ2UiOiB7CiAgICAgICAgInRpdGxlIjogIkp1ZGdlIiwKICAgICAgICAiZG9l
cyI6ICJTY29yZXMgcGxhbnMgKGFuZCBvdGhlciBiYWtlLW9mZnMpIGJsaW5kLCBhZ2FpbnN0IHRo
ZSBmcm96ZW4gIgogICAgICAgICAgICAgICAgImFjY2VwdGFuY2UgY3JpdGVyaWEsIHRvIHBpY2sg
dGhlIHN0cm9uZ2VzdCB3aXRob3V0IGJpYXMuIiwKICAgICAgICAic3RhZ2UiOiAicGxhbiIsCiAg
ICAgICAgInJlYWRzIjogImNhbmRpZGF0ZSBwbGFucyIsCiAgICAgICAgIndyaXRlcyI6ICJzY29y
ZXMsIHNlbGVjdGlvbiIsCiAgICB9LAogICAgIyAtLS0tIHRlc3Qtc3BlYyAtLS0tLS0tLS0tLS0t
LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLQogICAgInRlc3Qtc3Bl
YyI6IHsKICAgICAgICAidGl0bGUiOiAiVGVzdC1zcGVjIGFnZW50IiwKICAgICAgICAiZG9lcyI6
ICJGcmVlemVzIHRoZSBhY2NlcHRhbmNlIHRlc3RzIGZyb20gdGhlIHRpY2tldCwgYmVmb3JlIGFu
eSBjb2RlICIKICAgICAgICAgICAgICAgICJleGlzdHMsIHRoZW4gbG9ja3MgdGhlbSBzbyB0aGUg
aW1wbGVtZW50YXRpb24gY2Fubm90IG1vdmUgdGhlICIKICAgICAgICAgICAgICAgICJnb2FscG9z
dHMuIFRlc3RzIHdyaXR0ZW4gYWZ0ZXIgY29kZSBjb25mb3JtIHRvIHRoZSBjb2RlLiIsCiAgICAg
ICAgInN0YWdlIjogInRlc3Qtc3BlYyIsCiAgICAgICAgInJlYWRzIjogImFjY2VwdGFuY2UgY3Jp
dGVyaWEiLAogICAgICAgICJ3cml0ZXMiOiAiZnJvemVuIHRlc3Qgc3VpdGUgKGxvY2tlZCkiLAog
ICAgfSwKICAgICMgLS0tLSBkZXZlbG9wIC0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0t
LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0KICAgICJkZXZlbG9wZXIiOiB7CiAgICAgICAgInRp
dGxlIjogIkRldmVsb3BlciIsCiAgICAgICAgImRvZXMiOiAiV3JpdGVzIHRoZSBjb2RlIGFnYWlu
c3QgdGhlIGZyb3plbiBwbGFuIGFuZCB0ZXN0IHNwZWMuIEV2ZXJ5ICIKICAgICAgICAgICAgICAg
ICJlZGl0IHBhc3NlcyB0aHJvdWdoIHRoZSBnb3Zlcm5vciBmb3IgYmxhc3QtcmFkaXVzIGVuZm9y
Y2VtZW50LiIsCiAgICAgICAgInN0YWdlIjogImRldmVsb3AiLAogICAgICAgICJyZWFkcyI6ICJw
bGFuLm1kLCB0ZXN0IHNwZWMsIHJlcG9zaXRvcnkiLAogICAgICAgICJ3cml0ZXMiOiAiY29kZSAo
ZGlmZi5wYXRjaCkiLAogICAgfSwKICAgICJsZWFkLWRldmVsb3BlciI6IHsKICAgICAgICAidGl0
bGUiOiAiTGVhZCBkZXZlbG9wZXIiLAogICAgICAgICJkb2VzIjogIlRoZSBkZXZlbG9wZXIgcm9s
ZSBvbiBhIHNwbGl0IHRpY2tldDogb3ducyBvbmUgc2xpY2UsIHdyaXRlcyAiCiAgICAgICAgICAg
ICAgICAiaXRzIGNvZGUgaW5zaWRlIHRoZSBibGFzdCByYWRpdXMsIGFuZCBhbnN3ZXJzIHRvIHRo
ZSBsZWFkIHRoYXQgIgogICAgICAgICAgICAgICAgImNvb3JkaW5hdGVzIHRoZSBzbGljZXMuIiwK
ICAgICAgICAic3RhZ2UiOiAiZGV2ZWxvcCIsCiAgICAgICAgInJlYWRzIjogInNsaWNlIHBsYW4s
IHRlc3Qgc3BlYywgcmVwb3NpdG9yeSIsCiAgICAgICAgIndyaXRlcyI6ICJzbGljZSBjb2RlIiwK
ICAgIH0sCiAgICAid29ya2VyIjogewogICAgICAgICJ0aXRsZSI6ICJXb3JrZXIiLAogICAgICAg
ICJkb2VzIjogIlJ1bnMgYSBzaW5nbGUgc2xpY2Ugb2YgYSBzcGxpdCB0aWNrZXQgZW5kIHRvIGVu
ZCB1bmRlciB0aGUgIgogICAgICAgICAgICAgICAgImxlYWQuIENvYWNoZWQgYW5kIHJldHJpZWQg
YnkgdGhlIGxlYWQgd2hlbiBpdHMgc2xpY2UgZmFpbHM7ICIKICAgICAgICAgICAgICAgICJlYWNo
IGNvYWNoaW5nIHJvdW5kIGlzIHJlY29yZGVkLiIsCiAgICAgICAgInN0YWdlIjogImRldmVsb3Ai
LAogICAgICAgICJyZWFkcyI6ICJzbGljZSBzcGVjIiwKICAgICAgICAid3JpdGVzIjogInNsaWNl
IHJlc3VsdCIsCiAgICB9LAogICAgImNoZWNrcG9pbnRlciI6IHsKICAgICAgICAidGl0bGUiOiAi
Q2hlY2twb2ludGVyIiwKICAgICAgICAiZG9lcyI6ICJTYXZlcyB0aGUgb3JpZ2luYWwgc3RhdGUg
YW5kIGEgY2hlY2twb2ludCBwZXIgdGFzaywgYW5kIHByb3ZlcyAiCiAgICAgICAgICAgICAgICAi
YW55IHJvbGxiYWNrIGlzIGJ5dGUtaWRlbnRpY2FsIHRvIHdoZXJlIHlvdSBzdGFydGVkLiAiCiAg
ICAgICAgICAgICAgICAiRGV0ZXJtaW5pc3RpYywgbm90IGEgbW9kZWwuIiwKICAgICAgICAic3Rh
Z2UiOiAiZGV2ZWxvcCIsCiAgICAgICAgInJlYWRzIjogImZpbGVzeXN0ZW0iLAogICAgICAgICJ3
cml0ZXMiOiAiY2hlY2twb2ludHMiLAogICAgfSwKICAgICMgLS0tLSByZXZpZXcgLS0tLS0tLS0t
LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0KICAgICJy
ZXZpZXdlciI6IHsKICAgICAgICAidGl0bGUiOiAiUmV2aWV3ZXIiLAogICAgICAgICJkb2VzIjog
IlJldmlld3MgdGhlIGltcGxlbWVudGF0aW9uIGZvciBjb3JyZWN0bmVzcywgc3R5bGUsIGFuZCAi
CiAgICAgICAgICAgICAgICAiYWRoZXJlbmNlIHRvIHRoZSBwbGFuLiBTZWVzIHRoZSBkaWZmIGFu
ZCB0aGUgdGlja2V0IG9ubHkgLSBubyAiCiAgICAgICAgICAgICAgICAicGxhbiwgbm8gZGV2ZWxv
cGVyIHJlYXNvbmluZyAtIHNvIGl0IGNhbm5vdCBydWJiZXItc3RhbXAuIiwKICAgICAgICAic3Rh
Z2UiOiAicmV2aWV3IiwKICAgICAgICAicmVhZHMiOiAiZGlmZiwgdGlja2V0IiwKICAgICAgICAi
d3JpdGVzIjogInJldmlldyB2ZXJkaWN0IiwKICAgIH0sCiAgICAjIC0tLS0gc2VjdXJpdHkgLS0t
LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tCiAg
ICAic2VjdXJpdHkiOiB7CiAgICAgICAgInRpdGxlIjogIlNlY3VyaXR5IGFnZW50IiwKICAgICAg
ICAiZG9lcyI6ICJTY2FucyB0aGUgY2hhbmdlIGZvciB2dWxuZXJhYmlsaXRpZXMgLSBTbnlrIGFu
ZCBkZXBlbmRlbmN5L2NvZGUgIgogICAgICAgICAgICAgICAgImFuYWx5c2lzIGZvciBDVkVzIGFu
ZCB1bnNhZmUgcGF0dGVybnMuIEZhaWwtY2xvc2VkIG9uIGhpZ2ggIgogICAgICAgICAgICAgICAg
ImZpbmRpbmdzLiIsCiAgICAgICAgInN0YWdlIjogInNlY3VyaXR5IiwKICAgICAgICAicmVhZHMi
OiAiZGlmZiwgZGVwZW5kZW5jaWVzIiwKICAgICAgICAid3JpdGVzIjogInNlY3VyaXR5IGZpbmRp
bmdzIChzbnlrLmpzb24pLCB0cmlhZ2UiLAogICAgfSwKICAgICMgLS0tLSBxYSAtLS0tLS0tLS0t
LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0KICAg
ICJxYSI6IHsKICAgICAgICAidGl0bGUiOiAiUUEgYWdlbnQiLAogICAgICAgICJkb2VzIjogIlZl
cmlmaWVzIGVuZC10by1lbmQgYmVoYXZpb3VyIGFnYWluc3QgdGhlIGFjY2VwdGFuY2UgY3JpdGVy
aWEgIgogICAgICAgICAgICAgICAgInVzaW5nIHRoZSBmcm96ZW4gc3VpdGUgYXMgdGhlIGF1dGhv
cml0eS4iLAogICAgICAgICJzdGFnZSI6ICJxYSIsCiAgICAgICAgInJlYWRzIjogImFjY2VwdGFu
Y2UgY3JpdGVyaWEsIGZyb3plbiB0ZXN0cyIsCiAgICAgICAgIndyaXRlcyI6ICJxYSBldmlkZW5j
ZSIsCiAgICB9LAogICAgImxlYWQtcWEiOiB7CiAgICAgICAgInRpdGxlIjogIkxlYWQgUUEiLAog
ICAgICAgICJkb2VzIjogIlJ1bnMgUUEgcGVyIHNsaWNlIG9uIGEgc3BsaXQgdGlja2V0LCBhZ2Fp
bnN0IHRoZSBmcm96ZW4gc3VpdGUsICIKICAgICAgICAgICAgICAgICJhbmQgcmVwb3J0cyBlYWNo
IHNsaWNlJ3Mgb3V0Y29tZSBiYWNrIHRvIHRoZSBsZWFkLiIsCiAgICAgICAgInN0YWdlIjogInFh
IiwKICAgICAgICAicmVhZHMiOiAiZnJvemVuIHN1aXRlLCBzbGljZSIsCiAgICAgICAgIndyaXRl
cyI6ICJwZXItc2xpY2UgUUEgZXZpZGVuY2UiLAogICAgfSwKICAgICMgLS0tLSBtdXRhdGlvbiAt
LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0K
ICAgICJtdXRhdGlvbiI6IHsKICAgICAgICAidGl0bGUiOiAiTXV0YXRpb24gZW5naW5lIiwKICAg
ICAgICAiZG9lcyI6ICJEZXRlcm1pbmlzdGljYWxseSBtdXRhdGVzIHRoZSBjb2RlIGFuZCBjaGVj
a3MgdGhlIGZyb3plbiB0ZXN0cyAiCiAgICAgICAgICAgICAgICAibm90aWNlLiBUaGUga2lsbC1y
YXRlIGdhdGUgLSBjb3ZlcmFnZSBzYXlzIGEgbGluZSByYW4sIHRoaXMgIgogICAgICAgICAgICAg
ICAgInNheXMgYSBwbGFudGVkIGJ1ZyB3b3VsZCBiZSBjYXVnaHQuIiwKICAgICAgICAic3RhZ2Ui
OiAibXV0YXRpb24iLAogICAgICAgICJyZWFkcyI6ICJjb2RlLCBmcm96ZW4gdGVzdHMiLAogICAg
ICAgICJ3cml0ZXMiOiAibXV0YXRpb24gcmVwb3J0IChraWxsIHJhdGUpIiwKICAgIH0sCiAgICAj
IC0tLS0gcmV0cm8gLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0t
LS0tLS0tLS0tLS0tLS0tCiAgICAicmV0cm8iOiB7CiAgICAgICAgInRpdGxlIjogIlJldHJvIGFn
ZW50IiwKICAgICAgICAiZG9lcyI6ICJBZnRlciBhIHRpY2tldCBsYW5kcywgcHJvcG9zZXMgd2hh
dCB0aGUgcGlwZWxpbmUgc2hvdWxkICIKICAgICAgICAgICAgICAgICJyZW1lbWJlciAtIGNvbnRl
eHQgZ2FwcywgcmVjdXJyaW5nIGZhaWx1cmVzIC0gZm9yIHlvdSB0byAiCiAgICAgICAgICAgICAg
ICAicmF0aWZ5IGludG8gYWdlbnQgbWVtb3J5LiIsCiAgICAgICAgInN0YWdlIjogIm11dGF0aW9u
IiwKICAgICAgICAicmVhZHMiOiAiZnVsbCBydW4gaGlzdG9yeSIsCiAgICAgICAgIndyaXRlcyI6
ICJwcm9wb3NlZCBsZWFybmluZ3MiLAogICAgfSwKICAgICMgLS0tLSBjcm9zcy1jdXR0aW5nIC0t
LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0KICAgICJn
b3Zlcm5vciI6IHsKICAgICAgICAidGl0bGUiOiAiR292ZXJub3IiLAogICAgICAgICJkb2VzIjog
IkVuZm9yY2VzIHRoZSBydWxlcyB0aGUgYWdlbnRzIGNhbm5vdCBiZW5kOiBldmVyeSBhY3Rpb24g
aXMgIgogICAgICAgICAgICAgICAgImFsbG93ZWQsIGFza2VkIChwYXVzZWQgZm9yIHlvdSksIG9y
IGRlbmllZCBieSByb2xlLiBBIHdyaXRlICIKICAgICAgICAgICAgICAgICJvdXRzaWRlIHRoZSBi
bGFzdCByYWRpdXMgaXMgZGVuaWVkLCBub3QgcG9saXRlbHkgZGVjbGluZWQuIiwKICAgICAgICAi
c3RhZ2UiOiBOb25lLAogICAgICAgICJyZWFkcyI6ICJldmVyeSBhZ2VudCBhY3Rpb24iLAogICAg
ICAgICJ3cml0ZXMiOiAiYWxsb3cgLyBhc2sgLyBkZW55IGRlY2lzaW9ucyIsCiAgICB9LAogICAg
InN5c3RlbSI6IHsKICAgICAgICAidGl0bGUiOiAiU3lzdGVtIiwKICAgICAgICAiZG9lcyI6ICJU
aGUgb3JjaGVzdHJhdG9yIGl0c2VsZiAtIGxvb3AgYm9va2tlZXBpbmcsIGdhdGUgc2VxdWVuY2lu
ZywgIgogICAgICAgICAgICAgICAgImFuZCB0aGUgbGVkZ2VyIHdyaXRlcyB0aGF0IGFyZSBub3Qg
YW55IHNpbmdsZSBhZ2VudCdzIHdvcmsuIiwKICAgICAgICAic3RhZ2UiOiBOb25lLAogICAgICAg
ICJyZWFkcyI6ICJjb25maWcsIGxlZGdlciIsCiAgICAgICAgIndyaXRlcyI6ICJydW4gLyBnYXRl
IC8gZXZlbnQgcm93cyIsCiAgICB9LAp9Cg==
"""

SEED_ANCHOR = '''        if e.get("model"):
            t["models"].add(e["model"])
    out = []'''

SEED_PATCH = '''        if e.get("model"):
            t["models"].add(e["model"])
    # seed the roster with every described agent, so the full cast shows even
    # before it has logged an event (renders with 0 calls).
    for _role in AGENT_INFO:
        tally.setdefault(_role, {"role": _role, "calls": 0, "_in": [], "_out": [],
                                 "_cost": [], "models": set()})
    out = []'''

MERGE_BLOCK = '''

# --- external agent descriptions: edit agent_info.py to add/adjust agents ----
try:
    from agent_info import AGENT_INFO as _EXTRA_AGENTS
    AGENT_INFO.update(_EXTRA_AGENTS)
except Exception:
    pass
'''


def main():
    ap = argparse.ArgumentParser(description="wire the full agent roster into the dashboard")
    ap.add_argument("--db", default="ledger.db")
    args = ap.parse_args()

    pb = os.path.join(HERE, "payload_builder.py")
    if not os.path.exists(pb):
        print("wire_agents: no payload_builder.py in this folder (" + HERE + ").")
        print("  Put wire_agents.py in docket/, beside payload_builder.py, and run it there.")
        return 2

    # 1. write agent_info.py
    ai = os.path.join(HERE, "agent_info.py")
    with open(ai, "wb") as f:
        f.write(base64.b64decode(_AGENT_INFO_B64.encode()))
    print("wrote agent_info.py (edit this file to add/adjust agents)")

    # 2. patch payload_builder.py additively
    with open(pb, "r", encoding="utf-8") as f:
        src = f.read()
    bak = pb + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    with open(bak, "w", encoding="utf-8") as f:
        f.write(src)
    print("backed up payload_builder.py -> " + os.path.basename(bak))

    changed = False
    if "for _role in AGENT_INFO:" not in src:
        if src.count(SEED_ANCHOR) == 1:
            src = src.replace(SEED_ANCHOR, SEED_PATCH)
            changed = True
            print("  + seeded the roster with the full described cast")
        else:
            print("  ! could not find the roster seed point (found "
                  + str(src.count(SEED_ANCHOR)) + " matches); skipped that patch.")
            print("    The descriptions will still apply to agents that have run.")
    else:
        print("  = roster seed already present")

    if "_EXTRA_AGENTS" not in src:
        src = src.rstrip() + "\n" + MERGE_BLOCK
        changed = True
        print("  + merged agent_info.py into AGENT_INFO")
    else:
        print("  = agent_info.py merge already present")

    if changed:
        with open(pb, "w", encoding="utf-8") as f:
            f.write(src)

    # clear stale bytecode
    pyc = os.path.join(HERE, "__pycache__")
    if os.path.isdir(pyc):
        for n in os.listdir(pyc):
            if n.startswith(("payload_builder", "agent_info")):
                try:
                    os.remove(os.path.join(pyc, n))
                except OSError:
                    pass

    # 3. verify
    print("")
    print("=" * 60)
    ok = True
    print("$ python payload_builder.py --self-test")
    r = subprocess.run([sys.executable, pb, "--self-test"], cwd=HERE,
                       capture_output=True, text=True)
    for ln in ((r.stdout or "") + (r.stderr or "")).splitlines():
        if "FAIL" in ln or "self-test" in ln or "Error" in ln or "Traceback" in ln:
            print("  " + ln)
    ok = ok and r.returncode == 0

    if os.path.exists(os.path.join(HERE, args.db)):
        print("$ python payload_builder.py --db " + args.db + " --doctor")
        r2 = subprocess.run([sys.executable, pb, "--db", args.db, "--doctor"],
                            cwd=HERE, capture_output=True, text=True)
        for ln in ((r2.stdout or "") + (r2.stderr or "")).splitlines()[-6:]:
            print("  " + ln)
    print("=" * 60)
    if ok:
        print("Done. Rebuild the dashboard and open the Agents tab:")
        print("    python report.py --db " + args.db + " --out report.html")
        print("Every described agent now shows; ones that have not run read 0 calls.")
    else:
        print("Self-test reported a problem above. payload_builder.py.bak-* holds the")
        print("original; send me the FAIL line and I will adjust.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
