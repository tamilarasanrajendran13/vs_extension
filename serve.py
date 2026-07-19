#!/usr/bin/env python3
"""
Docket - the local server. Host 3.

The other two hosts cover almost everything:

    the webview      live, but only while VS Code is open
    report.py        anywhere, but frozen at the moment you ran it

This covers the gap between them, and it is a real gap: watching an overnight
run from a laptop on the sofa with VS Code closed. That is the whole
justification. If you are not doing that, use the other two.

WHY THIS IS ALLOWED TO EXIST
    A server outside the extension host cannot call `vscode.lm`. That constraint
    killed the old bridge and it has not moved. It does not apply here, because
    this server never calls a model. It reads ledger.db and renders it. It is a
    window, not a participant.

WHAT IT WILL NOT DO
    - bind anything but 127.0.0.1 (see --host, and read its help before you use it)
    - open the database anything but mode=ro
    - accept any method but GET and HEAD
    - serve any path but the two it defines
    - queue a run, edit a ticket, or write one byte anywhere

    A dashboard that can start a run is a dashboard that needs auth, a CSRF
    story, and a security review. This one needs none of those because it cannot
    do anything. Keep it that way.

USAGE
    python serve.py --db ledger.db
    python serve.py --db ledger.db --port 8787 --refresh 5
    python serve.py --demo                    # synthetic ledger, no db needed
    python serve.py --self-test
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import payload_builder  # noqa: E402
import report  # noqa: E402
import extra_tabs  # noqa: E402  # Reference / Knowledge / Slices tabs

SERVE_VERSION = "0.1"

# The poller lives HERE and not in app.js, on purpose.
#
# report.py asserts that the emailed report contains no `fetch(` at all. That is
# a real property worth keeping: the static export must be provably incapable of
# reaching the network, because it opens on a locked-down laptop and any attempt
# to phone home is a support ticket at best.
#
# So the shared frontend has no network code. The host that HAS a network
# supplies it. One frontend, three hosts, and it never learns which one it is
# in - the same rule that keeps the gateway from learning what a ticket is.
POLLER = """
<script>
(function () {
  var etag = "__ETAG__", ms = __REFRESH__ * 1000;
  if (!ms) return;
  var dot = document.createElement("div");
  dot.className = "live";
  dot.innerHTML = '<span class="live-dot"></span><span class="live-txt">live</span>';
  document.querySelector(".masthead-in").appendChild(dot);
  function tick() {
    fetch("/payload.json", { headers: { "If-None-Match": etag } })
      .then(function (r) {
        if (r.status === 304) return null;      // unchanged: do not repaint
        etag = r.headers.get("ETag") || etag;
        return r.json();
      })
      .then(function (p) {
        if (!p) return;
        window.DocketDashboard.render(p);
        dot.classList.add("beat");
        setTimeout(function () { dot.classList.remove("beat"); }, 700);
      })
      .catch(function () {
        dot.classList.add("dead");
        dot.querySelector(".live-txt").textContent = "server gone";
      });
  }
  setInterval(tick, ms);
})();
</script>
<style>
.live { margin-left: 12px; display: flex; align-items: center; gap: 6px; }
.live-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--ultra); transition: transform .3s;
}
.live-txt {
  font-family: var(--mono); font-size: 10px; letter-spacing: .08em;
  text-transform: uppercase; color: var(--ink-faint);
}
.live.beat .live-dot { transform: scale(2.1); }
.live.dead .live-dot { background: var(--carmine); }
@media (prefers-reduced-motion: reduce) { .live-dot { transition: none; } }
</style>
"""


class Ledger:
    """
    Renders on demand, and not one time more.

    A 4MB ledger takes ~0.3s to roll up. At --refresh 5 with a browser tab open
    overnight that is 5,700 needless rebuilds by morning, on the same laptop the
    loop is trying to use. The database's mtime tells us whether anything
    happened; if it has not, the cached payload is still correct.
    """

    def __init__(self, db, release=None, project=None, max_events=200,
                 max_rows=40, exclude=(), hero=payload_builder.DEFAULT_HERO):
        self.db = db
        self.opts = dict(release=release, project=project, event_limit=max_events,
                         max_rows=max_rows, exclude=exclude, hero=hero)
        self._lock = threading.Lock()
        self._stamp = None
        self._payload = None
        self._etag = None
        self.builds = 0

    def _mtime(self):
        try:
            st = os.stat(self.db)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def payload(self):
        with self._lock:
            now = self._mtime()
            if now != self._stamp or self._payload is None:
                self._payload = payload_builder.build(self.db, **self.opts)
                body = json.dumps(self._payload, default=str,
                                  separators=(",", ":")).encode()
                self._etag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
                self._stamp = now
                self.builds += 1
            return self._payload, self._etag


class Handler(BaseHTTPRequestHandler):
    ledger: Ledger = None
    refresh: int = 10
    server_version = f"docket/{SERVE_VERSION}"
    sys_version = ""

    def log_message(self, fmt, *args):
        if self.path != "/payload.json":  # do not narrate the poll
            sys.stderr.write("  %s %s\n" % (self.command, self.path))

    def _head(self, code, ctype, body=b"", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # It reads a ledger. It has no business being framed, sniffed, or cached
        # by anything, and it never talks to another origin.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; "
                         "script-src 'unsafe-inline'; connect-src 'self'; "
                         "img-src data:")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        return body

    def do_HEAD(self):
        self.do_GET(head_only=True)

    def do_GET(self, head_only=False):
        if self.path in ("/", "/index.html"):
            payload, etag = self.ledger.payload()
            html = report.render(payload)
            # Same three extra tabs the static report gets (Reference, Knowledge,
            # Slices), injected before the colophon so the router lists them.
            html = extra_tabs.inject(html, self.ledger.db)
            poller = (POLLER.replace("__ETAG__", etag.strip('"'))
                            .replace("__REFRESH__", str(self.refresh)))
            html = html.replace("</body>", poller + "</body>")
            body = html.encode()
            self.wfile.write(self._head(200, "text/html; charset=utf-8", body)
                             if head_only else
                             (self._head(200, "text/html; charset=utf-8", body), body)[1])
            return

        if self.path == "/payload.json":
            payload, etag = self.ledger.payload()
            if self.headers.get("If-None-Match") in (etag, etag.strip('"')):
                self._head(304, "application/json")
                return
            body = json.dumps(payload, default=str, separators=(",", ":")).encode()
            self._head(200, "application/json", body, {"ETag": etag})
            if not head_only:
                self.wfile.write(body)
            return

        body = b"not found. this server has two paths: / and /payload.json\n"
        self._head(404, "text/plain; charset=utf-8", body)
        if not head_only:
            self.wfile.write(body)

    def _deny(self):
        body = b"read-only. this server cannot change your ledger.\n"
        self._head(405, "text/plain; charset=utf-8", body, {"Allow": "GET, HEAD"})
        self.wfile.write(body)

    # Everything that could imply a write. Spelled out rather than left to the
    # base class's 501, so the refusal is a decision and not a default.
    do_POST = do_PUT = do_DELETE = do_PATCH = _deny


def serve(db, host="127.0.0.1", port=8787, refresh=10, **kw):
    Handler.ledger = Ledger(db, **kw)
    Handler.refresh = refresh
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd


def _self_test() -> int:
    import tempfile
    import urllib.request
    import urllib.error
    from _demo_ledger import write_demo

    passed = failed = 0

    def check(n, c):
        nonlocal passed, failed
        if c:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL  {n}")

    tmp = tempfile.mkdtemp()
    db = write_demo(os.path.join(tmp, "l.db"))
    httpd = serve(db, port=0)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"

    try:
        with urllib.request.urlopen(base + "/") as r:
            html = r.read().decode()
            check("serves the dashboard", r.status == 200)
        check("page is the real bundle", "DocketDashboard" in html)
        check("poller injected", "/payload.json" in html)
        check("live indicator injected", "live-dot" in html)
        check("no placeholders survive", "__DOCKET_" not in html)
        check("poller tokens replaced", "__ETAG__" not in html and "__REFRESH__" not in html)

        with urllib.request.urlopen(base + "/payload.json") as r:
            etag = r.headers["ETag"]
            p = json.loads(r.read())
            check("serves the payload", r.status == 200 and etag)
        check("payload is the real thing", p["schema"] == payload_builder.SCHEMA_VERSION)

        # unchanged db -> 304, and no rebuild
        before = Handler.ledger.builds
        req = urllib.request.Request(base + "/payload.json",
                                     headers={"If-None-Match": etag})
        try:
            urllib.request.urlopen(req)
            check("304 on unchanged ledger", False)
        except urllib.error.HTTPError as e:
            check("304 on unchanged ledger", e.code == 304)
        check("unchanged ledger does not rebuild", Handler.ledger.builds == before)

        # touch the ledger -> new etag, one rebuild
        con = __import__("sqlite3").connect(db)
        con.execute("UPDATE runs SET summary='touched' WHERE rowid=1")
        con.commit()
        con.close()
        with urllib.request.urlopen(base + "/payload.json") as r:
            check("changed ledger -> fresh etag", r.headers["ETag"] != etag)
        check("changed ledger rebuilds once", Handler.ledger.builds == before + 1)

        # it must not be able to change anything
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            req = urllib.request.Request(base + "/payload.json", method=method,
                                         data=b"{}")
            try:
                urllib.request.urlopen(req)
                check(f"{method} refused", False)
            except urllib.error.HTTPError as e:
                check(f"{method} refused", e.code == 405)

        try:
            urllib.request.urlopen(base + "/../../etc/passwd")
            check("no path traversal", False)
        except urllib.error.HTTPError as e:
            check("no path traversal", e.code in (404, 400))

        try:
            urllib.request.urlopen(base + "/ledger.db")
            check("does not serve the database", False)
        except urllib.error.HTTPError as e:
            check("does not serve the database", e.code == 404)

        with urllib.request.urlopen(base + "/") as r:
            check("declines to be framed", r.headers["X-Frame-Options"] == "DENY")
            check("no store", r.headers["Cache-Control"] == "no-store")

        check("bound to loopback only", httpd.server_address[0] == "127.0.0.1")

        # the ledger must be untouched by all of that
        st = os.stat(db)
        with urllib.request.urlopen(base + "/"):
            pass
        check("serving does not touch the db", os.stat(db).st_mtime_ns == st.st_mtime_ns)
    finally:
        httpd.shutdown()

    print(f"serve self-test: {passed}/{passed + failed}")
    return 0 if failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="read-only live view of ledger.db")
    ap.add_argument("--db", default="ledger.db")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1",
                    help="DEFAULT AND CORRECT VALUE IS 127.0.0.1. Binding "
                         "0.0.0.0 puts your ledger - every ticket summary, every "
                         "Snyk finding - on the network with no authentication "
                         "whatsoever. There is no auth here because there is "
                         "nothing to protect on loopback. Do not do this.")
    ap.add_argument("--refresh", type=int, default=10,
                    help="seconds between polls. 0 disables live updates.")
    ap.add_argument("--release")
    ap.add_argument("--project")
    ap.add_argument("--max-events", type=int, default=200)
    ap.add_argument("--max-rows", type=int, default=40)
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--hero", default=payload_builder.DEFAULT_HERO,
                    choices=sorted(payload_builder.HEROES))
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return _self_test()

    db = a.db
    if a.demo:
        import tempfile
        from _demo_ledger import write_demo
        db = write_demo(os.path.join(tempfile.mkdtemp(), "demo.db"))
        print("demo ledger (synthetic - not your data)", file=sys.stderr)
    elif not os.path.exists(db):
        print(f"no ledger at {db}. try --demo, or point --db at it.", file=sys.stderr)
        return 2

    if a.host != "127.0.0.1":
        print(f"\n  !! binding {a.host}, not loopback. Your ledger is now "
              f"readable by anything that can route to this machine, with no "
              f"authentication. This is almost certainly not what you want.\n",
              file=sys.stderr)

    httpd = serve(db, a.host, a.port, a.refresh, release=a.release,
                  project=a.project, max_events=a.max_events, max_rows=a.max_rows,
                  exclude=tuple(a.exclude), hero=a.hero)
    live = f"live, polling every {a.refresh}s" if a.refresh else "static"
    print(f"docket  http://{a.host}:{httpd.server_address[1]}/  ({live}, read-only)",
          file=sys.stderr)
    print("ctrl-c to stop", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
