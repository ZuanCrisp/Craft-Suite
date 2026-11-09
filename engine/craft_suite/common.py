"""Shared helpers: logging, error classes, git runner, HTTP with bounded retries.

Design rules enforced here:
  * No token is ever placed on a command line or printed.
  * Git credentials are passed through GIT_CONFIG_* environment variables and are
    scoped to ONE URL (the destination repository), never to upstream hosts.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Tuple

USER_AGENT = "craft-suite-sync"


# ----------------------------------------------------------------- logging --
def log(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"::warning::{msg}", flush=True)


def err(msg: str) -> None:
    print(f"::error::{msg}", flush=True)


class SyncError(Exception):
    """Error with a stable category (see docs/OPERATIONS.md)."""

    def __init__(self, category: str, message: str):
        super().__init__(f"{category}: {message}")
        self.category = category
        self.message = message


# --------------------------------------------------------------------- git --
def mask(value: str) -> None:
    """Ask GitHub Actions to mask a derived secret (e.g. base64 of a token)."""
    if value:
        print(f"::add-mask::{value}", flush=True)


def git_auth_env(url: str, token: Optional[str]) -> Dict[str, str]:
    """Environment that authenticates git ONLY for `url` (https destinations)."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Never let a developer's global config or credential helper leak into runs.
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    if token and url.lower().startswith("https://"):
        b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        mask(b64)
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = f"http.{url}.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {b64}"
    return env


def clean_env() -> Dict[str, str]:
    """Environment for unauthenticated git calls (upstream reads)."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    for k in list(env):
        if k.startswith("GIT_CONFIG_KEY_") or k.startswith("GIT_CONFIG_VALUE_") or k == "GIT_CONFIG_COUNT":
            del env[k]
    return env


def run_git(args: List[str], cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None,
            check: bool = True, input_bytes: Optional[bytes] = None,
            timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git"] + args, cwd=cwd, env=env if env is not None else clean_env(),
        input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
    )
    if check and proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"git {' '.join(args[:3])} failed (exit {proc.returncode}): {stderr[-800:]}")
    return proc


def git_out(args: List[str], cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> str:
    return run_git(args, cwd=cwd, env=env).stdout.decode("utf-8", "replace").strip()


# --------------------------------------------------------------------- misc --
def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json_atomic(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def write_text_atomic(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def read_json(path: str, default: Any = None) -> Any:
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------- http --
class HttpResult:
    def __init__(self, status: int, headers: Dict[str, str], body: bytes):
        self.status = status
        self.headers = headers
        self.body = body

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None


def _is_rate_limited(status: int, headers: Dict[str, str], body: bytes) -> bool:
    if status == 429:
        return True
    if status == 403:
        if headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers:
            return True
        low = body[:2000].decode("utf-8", "replace").lower()
        return any(k in low for k in ("rate limit", "secondary rate", "abuse detection", "too many requests"))
    return False


def _retry_delay(attempt: int, headers: Dict[str, str], rate_limited: bool) -> int:
    ra = headers.get("retry-after", "")
    if ra.isdigit():
        return max(1, int(ra))
    reset = headers.get("x-ratelimit-reset", "")
    if rate_limited and headers.get("x-ratelimit-remaining") == "0" and reset.isdigit():
        return min(max(1, int(reset) - int(time.time())), 900)
    if rate_limited:
        return min(60 * (2 ** (attempt - 1)), 600)
    return 2 ** (attempt - 1)


def http_request(method: str, url: str, token: Optional[str] = None, body: Any = None,
                 headers: Optional[Dict[str, str]] = None, ok: Iterable[int] = (200, 201, 204),
                 retries: int = 4, timeout: int = 120, raw_body: Any = None,
                 sleep=time.sleep) -> HttpResult:
    """HTTP call with bounded retries, Retry-After / rate-limit awareness.

    `ok` lists statuses returned to the caller without raising. Anything else raises
    RuntimeError after retries for transient classes (429/5xx/network) or immediately.
    """
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}
    if headers:
        hdrs.update(headers)
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    elif raw_body is not None:
        data = raw_body

    attempt = 1
    while True:
        try:
            if hasattr(data, "seek"):
                data.seek(0)
            req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = HttpResult(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
        except urllib.error.HTTPError as exc:
            result = HttpResult(exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read() or b"")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            if attempt >= retries:
                raise RuntimeError(f"{method} {_redact(url)} failed after {retries} attempts: {exc}")
            d = 2 ** (attempt - 1)
            warn(f"Network error on {method} {_redact(url)} (attempt {attempt}/{retries}); retry in {d}s")
            sleep(d)
            attempt += 1
            continue

        if result.status in set(ok):
            return result
        limited = _is_rate_limited(result.status, result.headers, result.body)
        transient = limited or 500 <= result.status < 600
        if transient and attempt < retries:
            d = _retry_delay(attempt, result.headers, limited)
            warn(f"HTTP {result.status} on {method} {_redact(url)}; retry {attempt}/{retries} in {d}s")
            sleep(d)
            attempt += 1
            continue
        snippet = result.body[:300].decode("utf-8", "replace").replace("\n", " ")
        raise RuntimeError(f"{method} {_redact(url)} returned HTTP {result.status}: {snippet}")


def _redact(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def paginate(url: str, token: Optional[str], per_page: int = 100, max_pages: int = 1000) -> List[Any]:
    """Collect every page of a list endpoint until an empty page is returned."""
    items: List[Any] = []
    sep = "&" if "?" in url else "?"
    for page in range(1, max_pages + 1):
        data = http_request("GET", f"{url}{sep}per_page={per_page}&page={page}", token=token).json()
        if not isinstance(data, list):
            raise RuntimeError(f"Expected a JSON array from {_redact(url)}")
        if not data:
            return items
        items.extend(data)
    raise RuntimeError(f"Safety stop: more than {max_pages} pages from {_redact(url)}")


def download_to(url: str, dest_path: str, retries: int = 4, timeout: int = 900) -> Tuple[int, str]:
    """Stream a public URL to disk; returns (size, sha256). No credentials are sent."""
    attempt = 1
    while True:
        try:
            h = hashlib.sha256()
            size = 0
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest_path, "wb") as out:
                for chunk in iter(lambda: resp.read(1024 * 1024), b""):
                    out.write(chunk)
                    h.update(chunk)
                    size += len(chunk)
            return size, h.hexdigest()
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            if attempt >= retries:
                raise RuntimeError(f"download failed after {retries} attempts: {exc}")
            d = 2 ** (attempt - 1)
            warn(f"Download error (attempt {attempt}/{retries}); retry in {d}s")
            time.sleep(d)
            attempt += 1
