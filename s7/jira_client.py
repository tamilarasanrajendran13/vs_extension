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

import http.client
import json
import os
import ssl
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, quote


class JiraError(RuntimeError):
    """Jira said no. Distinguishable from 'the network died'."""


def load_env_file(path: str | Path) -> None:
    """
    Read KEY=value lines into os.environ. Existing values win - an explicitly
    exported token must never be silently overridden by a stale file.
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
            if k and k not in os.environ:
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


def from_env(verify_ssl: bool = True, workbench: Path | None = None) -> JiraClient:
    """Build a client from the environment. Loads .local/*.env if present."""
    if workbench:
        load_env_file(Path(workbench) / ".local" / "docket-runtime.env")
    base = os.environ.get("JIRA_BASE_URL")
    token = os.environ.get("JIRA_PAT") or os.environ.get("JIRA_TOKEN")
    if not base or not token:
        raise JiraError(
            "missing Jira env: "
            f"JIRA_BASE_URL={'set' if base else 'MISSING'}, "
            f"JIRA_PAT={'set' if token else 'MISSING'}. "
            "Export them, or put them in <workbench>/.local/docket-runtime.env"
        )
    return JiraClient(base, token, verify_ssl=verify_ssl)
