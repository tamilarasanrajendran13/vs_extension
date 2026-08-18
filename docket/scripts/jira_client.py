#!/usr/bin/env python3
"""
Docket - Jira client.

Jira Server / Data Center. Bearer PAT auth, REST API v2.

Stdlib only - http.client, no `requests`. In a locked-down shop a dependency is
a procurement conversation; this is not.

Env:
    JIRA_BASE_URL   https://jira.company.com  (a base path is fine: .../jira)
    JIRA_PAT        personal access token

Nothing secret is ever written to disk, a log, or the ledger.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import ssl
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, quote


class JiraError(RuntimeError):
    """Jira said no. Distinguishable from 'the network died'."""


def load_env_file(path: str | Path) -> None:
    """
    Read KEY=value lines into os.environ. The FILE wins over any pre-set
    process env var - the VS Code extension host inherits its environment
    from whenever VS Code was launched, so a system/terminal-exported value
    can be stale for the lifetime of that host process, while this file is
    read fresh every run. See CLAUDE.md section 3 ("file beats system env")
    and docket/.local/docket-runtime.env.example. Unconditionally overwrite
    os.environ for every key parsed out of the file.
    """
    p = Path(path)
    if not p.exists():
        return
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip("'\"")
            if k:
                os.environ[k] = v
    except Exception:
        pass  # a malformed env file must not take the pipeline down


class JiraClient:
    def __init__(self, base_url: str, token: str, verify_ssl: bool = True,
                 timeout: int = 30, max_retries: int = 3, backoff_factor: float = 2.0):
        if not base_url:
            raise ValueError("base_url is required")
        if not token:
            raise ValueError("token is required")

        self.token = token
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor

        parsed = urlparse(base_url.rstrip("/"))
        self.scheme = parsed.scheme or "https"
        self.host = parsed.hostname
        self.port = parsed.port
        self.base_path = parsed.path.rstrip("/")   # Jira may live under /jira
        if not self.host:
            raise ValueError(f"invalid base_url: {base_url!r}")

    # ---------------------------------------------------------------- http

    def _connect(self):
        if self.scheme == "https":
            ctx = None if self.verify_ssl else ssl._create_unverified_context()
            return http.client.HTTPSConnection(self.host, self.port,
                                               timeout=self.timeout, context=ctx)
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if extra:
            h.update(extra)
        return h

    def _request(self, method: str, path: str, headers: dict,
                 body: bytes | None = None) -> tuple[int, str]:
        if not path.startswith("/"):
            path = "/" + path
        full = (self.base_path + path) if self.base_path else path

        backoff = 1.0
        for attempt in range(1, self.max_retries + 1):
            conn = None
            try:
                conn = self._connect()
                conn.request(method, full, body=body, headers=headers)
                resp = conn.getresponse()
                status = resp.status
                data = resp.read().decode(errors="ignore")

                # 4xx is an answer, not a failure. Retrying a 401 just burns time
                # and can lock the account. Only retry what might actually change.
                if status in (429, 502, 503, 504) and attempt < self.max_retries:
                    time.sleep(backoff)
                    backoff *= self.backoff_factor
                    continue
                return status, data
            except Exception:
                if attempt == self.max_retries:
                    raise
                time.sleep(backoff)
                backoff *= self.backoff_factor
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
        return 0, ""

    # ---------------------------------------------------------------- api

    def get_issue(self, issue_key: str, expand_rendered: bool = True) -> dict:
        """
        renderedFields matters: Jira stores wiki markup, and the rendered view is
        often the only place a table-formatted AC block is readable.
        """
        path = f"/rest/api/2/issue/{quote(issue_key)}"
        if expand_rendered:
            path += "?expand=renderedFields"
        status, data = self._request("GET", path, self._headers())
        if status == 200:
            try:
                return json.loads(data)
            except Exception as e:
                raise JiraError(f"could not parse issue response: {e}") from e
        if status == 404:
            raise JiraError(f"{issue_key} not found (or no permission to see it)")
        if status in (401, 403):
            raise JiraError(f"HTTP {status} - check JIRA_PAT. Token expired or lacks access.")
        raise JiraError(f"HTTP {status} fetching {issue_key}: {data[:300]}")

    def search(self, jql: str, fields: list[str] | None = None,
               max_results: int = 50) -> list[dict]:
        """JQL. This is how the docket-ready trigger finds its own work."""
        q = quote(jql)
        path = f"/rest/api/2/search?jql={q}&maxResults={max_results}"
        if fields:
            path += "&fields=" + ",".join(fields)
        status, data = self._request("GET", path, self._headers())
        if status != 200:
            raise JiraError(f"HTTP {status} on JQL search: {data[:300]}")
        return json.loads(data).get("issues", [])

    def add_comment(self, issue_key: str, comment: str) -> bool:
        headers = self._headers({"Content-Type": "application/json"})
        body = json.dumps({"body": comment}).encode()
        status, _ = self._request(
            "POST", f"/rest/api/2/issue/{quote(issue_key)}/comment", headers, body)
        return status in (200, 201)

    def get_comments(self, issue_key: str) -> list[dict]:
        """
        Newest last. This is how the author's answers come back to us - people
        answer questions where the questions were asked, not by editing a ticket.
        """
        status, data = self._request(
            "GET", f"/rest/api/2/issue/{quote(issue_key)}/comment", self._headers())
        if status != 200:
            raise JiraError(f"HTTP {status} fetching comments for {issue_key}")
        return json.loads(data).get("comments", [])

    def get_attachments(self, issue_key: str) -> list[dict]:
        """Sample copybooks, fixtures, spec docs. The things nobody can 'answer'."""
        issue = self.get_issue(issue_key, expand_rendered=False)
        return (issue.get("fields") or {}).get("attachment") or []

    def download_attachment(self, att: dict, dest_dir: Path) -> Path:
        """
        Fetch one attachment to dest_dir. Returns the path written.

        Filenames come from Jira, i.e. from a human, i.e. from outside our trust
        boundary. A filename of "../../.ssh/authorized_keys" is a valid string
        that Jira will happily store, so we take the basename and nothing else.
        """
        import os as _os
        from urllib.parse import urlparse as _urlparse

        name = _os.path.basename(str(att.get("filename") or "attachment")).strip()
        name = name.replace("\\", "_").replace("/", "_") or "attachment"

        url = att.get("content") or ""
        path = _urlparse(url).path
        if not path:
            raise JiraError(f"attachment {name} has no content URL")

        status, data = self._request("GET", path, self._headers())
        if status != 200:
            raise JiraError(f"HTTP {status} downloading {name}")

        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = dest_dir / name
        out.write_bytes(data.encode("utf-8", errors="surrogateescape")
                        if isinstance(data, str) else data)
        return out

    def get_transitions(self, issue_key: str) -> list[dict]:
        status, data = self._request(
            "GET", f"/rest/api/2/issue/{quote(issue_key)}/transitions", self._headers())
        if status != 200:
            return []
        try:
            return json.loads(data).get("transitions", [])
        except Exception:
            return []

    def transition(self, issue_key: str, transition_id: str | None = None,
                   transition_name: str | None = None) -> bool:
        if transition_name and not transition_id:
            for t in self.get_transitions(issue_key):
                if t.get("name", "").lower() == transition_name.lower():
                    transition_id = t.get("id")
                    break
        if not transition_id:
            return False
        headers = self._headers({"Content-Type": "application/json"})
        body = json.dumps({"transition": {"id": str(transition_id)}}).encode()
        status, _ = self._request(
            "POST", f"/rest/api/2/issue/{quote(issue_key)}/transitions", headers, body)
        return status in (200, 204)

    def whoami(self) -> dict:
        """Cheapest possible auth check. Used by the preflight."""
        status, data = self._request("GET", "/rest/api/2/myself", self._headers())
        if status != 200:
            raise JiraError(f"HTTP {status} on /myself: {data[:200]}")
        return json.loads(data)


BASE_URL_VARS = ("JIRA_BASE_URL", "JIRA_URL")
TOKEN_VARS = ("JIRA_PAT", "JIRA_TOKEN", "JIRA_API_TOKEN")


def resolve_jira_env(env=None):
    """The ONE place Jira credentials are resolved. Accepts every historical
    alias; first match wins. Returns (base_url, token), either may be None.
    Callers needing the .local/docket-runtime.env file layer load that file
    into the env dict BEFORE calling this."""
    env = env if env is not None else os.environ
    base = next((env[v] for v in BASE_URL_VARS if env.get(v)), None)
    token = next((env[v] for v in TOKEN_VARS if env.get(v)), None)
    return base, token


def from_env(verify_ssl: bool = True, workbench: Path | None = None) -> JiraClient:
    """
    Build a client from the environment, loading <workbench>/.local/*.env first.

    The env file is the recommended home for these, not system environment
    variables: the VS Code extension host inherits its environment from whenever
    VS Code was LAUNCHED, so a var you export in a terminal is invisible to it,
    and a system var needs a full restart to take effect. This file is read at
    runtime, so it works the same whether the loop was spawned by VS Code or run
    from a terminal.

    The file always wins over a pre-set process env var - see load_env_file.
    """
    if workbench is None:
        workbench = Path(__file__).resolve().parent.parent
    load_env_file(Path(workbench) / ".local" / "docket-runtime.env")
    base, token = resolve_jira_env()
    if not base or not token:
        raise JiraError(
            "missing Jira env: "
            f"JIRA_BASE_URL={'set' if base else 'MISSING'}, "
            f"JIRA_PAT={'set' if token else 'MISSING'}. "
            "Export them, or put them in <workbench>/.local/docket-runtime.env"
        )
    return JiraClient(base, token, verify_ssl=verify_ssl)


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    results = []

    def ok(name: str, cond: bool) -> None:
        results.append((name, bool(cond)))

    ok("resolve_jira_env accepts every alias",
       resolve_jira_env({"JIRA_URL": "u", "JIRA_API_TOKEN": "t"}) == ("u", "t"))
    ok("primary names win over aliases",
       resolve_jira_env({"JIRA_BASE_URL": "a", "JIRA_URL": "b",
                         "JIRA_PAT": "p", "JIRA_TOKEN": "q"}) == ("a", "p"))
    ok("missing env resolves to (None, None)",
       resolve_jira_env({}) == (None, None))
    ok("defaults to os.environ when env is None",
       resolve_jira_env() == (os.environ.get("JIRA_BASE_URL") or os.environ.get("JIRA_URL"),
                              os.environ.get("JIRA_PAT") or os.environ.get("JIRA_TOKEN")
                              or os.environ.get("JIRA_API_TOKEN")))

    # load_env_file must let the FILE win over a pre-set process env var -
    # the extension host's env can be stale for the life of that process,
    # the file is read fresh every run. Save/restore the real env around it.
    _probe_key = "DOCKET_SELF_TEST_ENV_PRECEDENCE"
    _saved = os.environ.get(_probe_key, None)
    _had_saved = _probe_key in os.environ
    try:
        os.environ[_probe_key] = "from-process-env"
        with tempfile.TemporaryDirectory() as _tmpdir:
            _envfile = Path(_tmpdir) / "scratch.env"
            _envfile.write_text(f"{_probe_key}=from-file\n", encoding="utf-8")
            load_env_file(_envfile)
            ok("load_env_file: file value overrides a pre-set env var",
               os.environ.get(_probe_key) == "from-file")
    finally:
        if _had_saved:
            os.environ[_probe_key] = _saved
        else:
            os.environ.pop(_probe_key, None)

    w = max(len(n) for n, _ in results)
    for name, passed in results:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name.ljust(w)}")
    failed = [n for n, p in results if not p]
    print(f"\n  {len(results) - len(failed)}/{len(results)} passed" + (f"  FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Docket Jira client - env resolution self-test")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    ap.error("nothing to do - this module is a library; run with --self-test")
    return 2


if __name__ == "__main__":
    sys.exit(main())
