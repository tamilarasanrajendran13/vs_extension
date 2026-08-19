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
import contextlib
import hashlib
import io
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import payload_builder  # noqa: E402
import report  # noqa: E402
import extra_tabs  # noqa: E402  # Reference / Knowledge / Slices tabs

SERVE_VERSION = "0.1"

# docket.check_exit.v1 - the exit-code contract for --self-test, shared with
# run_all_checks.py (which maps each code to a distinct ladder status).
#
#   0  every check ran and passed
#   1  a check ran and failed
#   3  a check could not run here because the ENVIRONMENT does not offer the
#      capability it needs. Not a pass: nothing was proved. Not a failure:
#      nothing is known to be broken. Not a skip: the ladder must see it.
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_UNAVAILABLE = 3

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
            # They render from the payload this handler already has (B2): this
            # server opens the ledger exactly once, through payload_builder,
            # and always mode=ro.
            html = extra_tabs.inject(html, payload)
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


def build_parser() -> argparse.ArgumentParser:
    """The CLI surface, built apart from main() so it can be inspected
    without running a server. The self-test asserts the loopback default
    here, and that assertion needs no socket."""
    ap = argparse.ArgumentParser(description="read-only live view of ledger.db")
    ap.add_argument("--db", default=os.path.join(HERE, "ledger.db"))
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
    return ap


# ---------------------------------------------------------------- self-test
#
# Split in two, and the split is the whole point of this section.
#
# Everything this server PROMISES is a property of the handler, not of the
# kernel: loopback by default, 405 on every write verb, two paths and no
# more, the database never touched. A handler is a pure function - bytes in,
# bytes out - and BaseHTTPRequestHandler only ever reaches its socket through
# makefile() and sendall(). Hand it an in-memory object with those two
# methods and every one of those promises becomes decidable with no kernel
# involved, which means they are decidable in a sandbox that denies bind().
#
# Exactly one promise genuinely needs a socket: that the thing binds loopback
# and speaks HTTP to a real client. Where bind() is denied that check cannot
# run, and the honest report is UNAVAILABLE, with its own exit code. Calling
# it a pass would be a lie about coverage; calling it a failure would be a
# lie about the code; skipping it silently would hide both.


class _Sink(io.RawIOBase):
    """Reached only if the stdlib hands the handler a buffered wfile
    (it currently uses sendall directly). Kept so the seam does not
    depend on that detail staying true."""

    def __init__(self, exchange):
        self._exchange = exchange

    def writable(self):
        return True

    def write(self, b):
        self._exchange.sent += bytes(b)
        return len(b)


class _MemoryExchange:
    """A socket-shaped object holding one request and collecting the reply."""

    def __init__(self, raw: bytes):
        self._raw = raw
        self.sent = bytearray()

    def makefile(self, mode="rb", bufsize=-1):
        return io.BytesIO(self._raw) if "r" in mode else _Sink(self)

    def sendall(self, data):
        self.sent += data

    # The handler configures its connection before using it. None of that
    # means anything without a kernel, and none of it needs to.
    def setsockopt(self, *a):
        pass

    def settimeout(self, *a):
        pass

    def shutdown(self, *a):
        pass

    def close(self):
        pass


def _raw_request(method, path, headers=None, body=b"") -> bytes:
    lines = ["{} {} HTTP/1.0".format(method, path)]
    hdr = dict(headers or {})
    hdr.setdefault("Host", "127.0.0.1")
    if body:
        hdr.setdefault("Content-Length", str(len(body)))
    for k, v in hdr.items():
        lines.append("{}: {}".format(k, v))
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def _parse_response(raw: bytes):
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(None, 2) if lines and lines[0] else []
    status = int(parts[1]) if len(parts) > 1 else 0
    headers = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return status, headers, body


def _exchange(method, path, headers=None, body=b""):
    """Drive the REAL Handler class through one request. No socket, no port,
    no thread. stderr is captured because the handler logs every request and
    that access log is not part of what is under test."""
    ex = _MemoryExchange(_raw_request(method, path, headers, body))
    with contextlib.redirect_stderr(io.StringIO()):
        Handler(ex, ("127.0.0.1", 0), None)
    return _parse_response(bytes(ex.sent))


def bind_probe(host="127.0.0.1"):
    """None if this machine will let us bind loopback, otherwise the reason
    it will not. Binds an ephemeral port and drops it immediately: a probe
    that held a port would break the thing it is clearing the way for."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, 0))
        return None
    except PermissionError as e:
        return "socket bind denied ({})".format(e.strerror or e)
    except OSError as e:
        return "socket bind unavailable ({})".format(e.strerror or e)
    finally:
        s.close()


def _bind_free_checks(check, db) -> None:
    import inspect
    import _demo_ledger

    Handler.ledger = Ledger(db)
    Handler.refresh = 10

    status, hdr, body = _exchange("GET", "/")
    html = body.decode()
    check("serves the dashboard", status == 200)
    check("page is the real bundle", "DocketDashboard" in html)
    check("poller injected", "/payload.json" in html)
    check("live indicator injected", "live-dot" in html)
    check("no placeholders survive", "__DOCKET_" not in html)
    check("poller tokens replaced",
          "__ETAG__" not in html and "__REFRESH__" not in html)
    check("content-length matches the body",
          hdr.get("content-length") == str(len(body)))
    check("declines to be framed", hdr.get("x-frame-options") == "DENY")
    check("no store", hdr.get("cache-control") == "no-store")
    check("declines to be sniffed",
          hdr.get("x-content-type-options") == "nosniff")
    check("csp denies everything by default",
          (hdr.get("content-security-policy") or "")
          .startswith("default-src 'none'"))

    status, hdr, body = _exchange("GET", "/payload.json")
    etag = hdr.get("etag")
    p = json.loads(body)
    check("serves the payload", status == 200 and bool(etag))
    check("payload is the real thing",
          p["schema"] == payload_builder.SCHEMA_VERSION)

    # unchanged db -> 304, and no rebuild
    before = Handler.ledger.builds
    status, _, _ = _exchange("GET", "/payload.json", {"If-None-Match": etag})
    check("304 on unchanged ledger", status == 304)
    check("unchanged ledger does not rebuild", Handler.ledger.builds == before)

    # touch the ledger -> new etag, one rebuild. Mutate a field that is
    # actually in the payload on any schema (a run's outcome), so the etag
    # changes even on ledgers whose CONTRACT does not map summary. The
    # poke goes through _demo_ledger, which owns the fixtures:
    # payload_builder.py is the only dashboard component that opens a
    # database (B2), and that includes this file's tests.
    _oc = (payload_builder.CONTRACT.get("runs", {})
           .get("columns", {}).get("outcome", "outcome"))
    check("fixture outcome column exists on this schema",
          _demo_ledger.set_run_field(db, _oc, "failed"))
    status, hdr, _ = _exchange("GET", "/payload.json")
    check("changed ledger -> fresh etag", hdr.get("etag") != etag)
    check("changed ledger rebuilds once", Handler.ledger.builds == before + 1)

    # it must not be able to change anything
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        status, hdr, body = _exchange(method, "/payload.json", body=b"{}")
        check("{} refused".format(method), status == 405)
        check("{} refusal advertises what is allowed".format(method),
              hdr.get("allow") == "GET, HEAD")
        check("{} refusal says why".format(method), b"read-only" in body)

    # sent raw, unnormalized - a stronger test than urllib's, which collapses
    # the dot segments before the server ever sees them.
    status, _, _ = _exchange("GET", "/../../etc/passwd")
    check("no path traversal", status in (400, 404))
    status, _, _ = _exchange("GET", "/ledger.db")
    check("does not serve the database", status == 404)

    # the ledger must be untouched by all of that
    st = os.stat(db)
    _exchange("GET", "/")
    check("serving does not touch the db",
          os.stat(db).st_mtime_ns == st.st_mtime_ns)

    # loopback is the DEFAULT in both surfaces that can pick a host. This is
    # the property the old socket test asserted by reading server_address;
    # it was never a fact about the kernel, only about the defaults.
    check("serve() defaults to loopback",
          inspect.signature(serve).parameters["host"].default == "127.0.0.1")
    ns = build_parser().parse_args([])
    check("cli defaults to loopback", ns.host == "127.0.0.1")
    check("cli default port is 8787", ns.port == 8787)
    check("cli exposes --self-test",
          build_parser().parse_args(["--self-test"]).self_test)
    helptext = build_parser().format_help()
    check("cli --host help warns about the network",
          "127.0.0.1" in helptext and "authentication" in helptext)


def _bind_checks(check, db) -> None:
    """The one thing an in-memory handler cannot prove: that this binds a
    real loopback socket and answers a real client."""
    import urllib.error
    import urllib.request

    httpd = serve(db, port=0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:{}".format(port)
    try:
        check("bound to loopback only", httpd.server_address[0] == "127.0.0.1")
        with urllib.request.urlopen(base + "/") as r:
            check("a real client gets the dashboard",
                  r.status == 200 and "DocketDashboard" in r.read().decode())
        req = urllib.request.Request(base + "/payload.json", method="POST",
                                     data=b"{}")
        try:
            urllib.request.urlopen(req)
            check("a real client cannot write", False)
        except urllib.error.HTTPError as e:
            check("a real client cannot write", e.code == 405)
    finally:
        httpd.shutdown()


def _self_test() -> int:
    import tempfile
    from _demo_ledger import write_demo

    passed = failed = 0

    def check(n, c):
        nonlocal passed, failed
        if c:
            passed += 1
        else:
            failed += 1
            print("  FAIL  {}".format(n))

    db = write_demo(os.path.join(tempfile.mkdtemp(), "l.db"))

    _bind_free_checks(check, db)

    reason = bind_probe()
    unavailable = []
    if reason is None:
        _bind_checks(check, db)
    else:
        unavailable.append("binds loopback and serves a real client")

    print("serve self-test: {}/{}".format(passed, passed + failed))
    for name in unavailable:
        print("  UNAVAILABLE(environment: {})  {}".format(reason, name))
    if failed:
        return EXIT_FAIL
    if unavailable:
        print("serve self-test: {} bind-free checks PASS, {} check "
              "UNAVAILABLE(environment) - not a pass, not a failure. Run "
              "this where 127.0.0.1 can be bound to decide it."
              .format(passed, len(unavailable)))
        return EXIT_UNAVAILABLE
    return EXIT_OK


def main() -> int:
    a = build_parser().parse_args()

    if a.self_test:
        return _self_test()

    db = a.db
    if a.demo:
        import tempfile
        from _demo_ledger import write_demo
        db = write_demo(os.path.join(tempfile.mkdtemp(), "demo.db"))
        print("demo ledger (synthetic - not your data)", file=sys.stderr)
    elif not os.path.exists(db) or os.path.getsize(db) == 0:
        print(f"no usable ledger at {db} (missing or empty). "
              "try --demo, or point --db at it.", file=sys.stderr)
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
