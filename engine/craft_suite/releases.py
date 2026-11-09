"""Release engine: mirror published releases + assets of ONE application.

Key behaviours
  * identity = (app slug, upstream release id) stored as a hidden marker in the body;
    titles/tags alone are never trusted
  * every create/update sends make_latest="false" (the single repository-wide Latest
    badge must not be fought over by 17 apps; the catalog is the per-app truth)
  * new releases are created as DRAFT, assets are uploaded and verified, then the release
    is published: a half-uploaded release is never visible as a finished release
  * assets are reconciled by size + SHA-256 (API digest when present, otherwise the digest
    we computed and recorded the last time), NOT by filename
  * nothing is pruned; stale releases/assets are only reported
  * one failing release never stops the others
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .common import SyncError, download_to, http_request, log, paginate, warn

PACE = float(os.environ.get("CRAFT_SUITE_PACE", "0.5"))
MARKER_RE = re.compile(r"<!--\s*craft-suite:v1\s+app=([a-z0-9-]+)\s+src=(\S+)\s+release_id=(\d+)\s*-->")
SYNC_LINE_RE = re.compile(r"^\| Synced \(UTC\) \| .* \|$", re.M)
MENTION_RE = re.compile(r"(?<![\w`/])@([A-Za-z0-9][A-Za-z0-9-]{0,38}(?:/[A-Za-z0-9_.-]+)?)")


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hexdigest(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return value.split(":", 1)[1].lower() if value.lower().startswith("sha256:") else None


def _cell(v: Any) -> str:
    s = str(v).replace("`", "'").replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    return f"`{s}`"


def build_body(app: Dict[str, Any], rel: Dict[str, Any], dest_tag: str, commit: str,
               neutralize: bool, synced_at: str) -> str:
    original = rel.get("body") or ""
    if neutralize:
        original = MENTION_RE.sub("@\u200b\\1", original)
    head = [
        f"<!-- craft-suite:v1 app={app['slug']} src={app['repository']} release_id={rel['id']} -->",
        f"> Mirrored by Craft Suite Sync from the upstream {app['name']} release. "
        "The release notes below are the upstream originals.",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Application | {_cell(app['name'])} ({_cell(app['slug'])}) |",
        f"| Upstream repository | https://github.com/{app['repository']} |",
        f"| Upstream release | {rel.get('html_url', '')} |",
        f"| Upstream release ID | {_cell(rel['id'])} |",
        f"| Source tag | {_cell(rel['tag_name'])} |",
        f"| Destination tag | {_cell(dest_tag)} |",
        f"| Tag target commit | {_cell(commit or 'unknown')} |",
        f"| Upstream published | {_cell(rel.get('published_at') or rel.get('created_at') or 'unknown')} |",
        f"| Synced (UTC) | {_cell(synced_at)} |",
        "",
        "---",
        "",
    ]
    return "\n".join(head) + original


def _normalize_body(body: str) -> str:
    return SYNC_LINE_RE.sub("| Synced (UTC) | - |", (body or "").replace("\r\n", "\n")).rstrip()


def compute_latest(releases: List[Dict[str, Any]], dest_map: Dict[int, Dict[str, Any]], prerelease: bool,
                   prefix: str) -> Optional[Dict[str, Any]]:
    pool = [r for r in releases if bool(r.get("prerelease")) == prerelease]
    if not pool:
        return None
    best = max(pool, key=lambda r: (r.get("published_at") or r.get("created_at") or "", r["id"]))
    d = dest_map.get(best["id"], {})
    return {
        "source_tag": best["tag_name"],
        "destination_tag": prefix + best["tag_name"],
        "upstream_release_id": best["id"],
        "published_at": best.get("published_at") or best.get("created_at"),
        "upstream_release_url": best.get("html_url"),
        "destination_release_url": d.get("dest_release_url"),
    }


class ReleaseSync:
    def __init__(self, app: Dict[str, Any], policy: Dict[str, Any], src_api: str, src_token: Optional[str],
                 dest_api: str, dest_repo: str, dest_token: Optional[str], prev_app: Optional[Dict[str, Any]],
                 git, dry_run: bool):
        self.app, self.policy = app, policy
        self.src_api, self.src_token = src_api.rstrip("/"), src_token
        self.dest_api, self.dest_repo, self.dest_token = dest_api.rstrip("/"), dest_repo, dest_token
        self.git, self.dry_run = git, dry_run
        self.prefix = f"{app['slug']}--"
        prev_rel = ((prev_app or {}).get("releases") or [])
        self.prev_by_id = {int(r["upstream_release_id"]): r for r in prev_rel if "upstream_release_id" in r}
        self.workdir = tempfile.mkdtemp(prefix="cs-assets-")
        self.errors: List[Dict[str, str]] = []

    # -- API helpers -------------------------------------------------------
    def _dest(self, method: str, path: str, body: Any = None, ok=(200, 201, 204)):
        res = http_request(method, f"{self.dest_api}{path}", token=self.dest_token, body=body, ok=ok)
        if method != "GET":
            time.sleep(PACE)
        return res

    def fetch_source(self) -> List[Dict[str, Any]]:
        try:
            raw = paginate(f"{self.src_api}/repos/{self.app['repository']}/releases", self.src_token)
        except RuntimeError as exc:
            raise SyncError("RELEASE_LIST_FAILED", str(exc))
        out = []
        for r in raw:
            if r.get("draft"):
                continue
            if r.get("prerelease") and not (self.app["sync_prereleases"] and self.policy.get("include_prereleases", True)):
                continue
            out.append(r)
        return out

    def index_dest(self):
        try:
            rels = paginate(f"{self.dest_api}/repos/{self.dest_repo}/releases", self.dest_token)
        except RuntimeError as exc:
            raise SyncError("RELEASE_LIST_FAILED", f"destination: {exc}")
        by_id, by_tag = {}, {}
        for r in rels:
            m = MARKER_RE.search(r.get("body") or "")
            if m and m.group(1) == self.app["slug"] and m.group(2).lower() == self.app["repository"].lower():
                by_id[int(m.group(3))] = r
            by_tag[r["tag_name"]] = r
        return by_id, by_tag

    # -- main --------------------------------------------------------------
    def run(self) -> Dict[str, Any]:
        try:
            source = self.fetch_source()
            by_id, by_tag = self.index_dest()
            entries = []
            for rel in sorted(source, key=lambda r: (r.get("published_at") or r.get("created_at") or "", r["id"])):
                try:
                    entries.append(self._process(rel, by_id, by_tag))
                except SyncError as exc:
                    self._fail(exc.category, f"{rel.get('tag_name')}: {exc.message}")
                    entries.append(self._stub(rel, "failed", exc.category))
                except Exception as exc:  # isolate: next release still runs
                    self._fail("ASSET_UPLOAD_FAILED", f"{rel.get('tag_name')}: {exc}")
                    entries.append(self._stub(rel, "failed", "ASSET_UPLOAD_FAILED"))
            dest_map = {e["upstream_release_id"]: e for e in entries if e.get("dest_release_url")}
            counts: Dict[str, int] = {}
            for e in entries:
                counts[e["action"]] = counts.get(e["action"], 0) + 1
            return {
                "ok": True, "releases": entries, "counts": counts,
                "latest_stable": compute_latest(source, dest_map, False, self.prefix),
                "latest_prerelease": compute_latest(source, dest_map, True, self.prefix)
                if self.app["sync_prereleases"] else None,
                "source_release_ids": [r["id"] for r in source],
                "errors": self.errors,
            }
        finally:
            shutil.rmtree(self.workdir, ignore_errors=True)

    def _fail(self, category: str, message: str) -> None:
        self.errors.append({"category": category, "message": message})

    def _stub(self, rel: Dict[str, Any], action: str, error: Optional[str]) -> Dict[str, Any]:
        return {"upstream_release_id": rel["id"], "source_tag": rel.get("tag_name"),
                "dest_tag": self.prefix + str(rel.get("tag_name")), "action": action, "error": error,
                "prerelease": bool(rel.get("prerelease")), "published_at": rel.get("published_at"),
                "upstream_release_url": rel.get("html_url"), "assets": []}

    # -- one release -------------------------------------------------------
    def _process(self, rel: Dict[str, Any], by_id, by_tag) -> Dict[str, Any]:
        tag = rel.get("tag_name") or ""
        if not tag:
            raise SyncError("RELEASE_TAG_MISSING", "release has no tag_name")
        dest_tag = self.prefix + tag
        if not self.git.tag_available(dest_tag):
            raise SyncError("RELEASE_TAG_MISSING", f"destination tag {dest_tag} does not exist; release blocked")
        commit = self.git.tag_targets.get(tag, "")
        title = f"[{self.app['name']}] {rel.get('name') or tag}"
        existing = by_id.get(rel["id"])
        if existing is None:
            cand = by_tag.get(dest_tag)
            if cand is not None:
                m = MARKER_RE.search(cand.get("body") or "")
                if m and m.group(1) == self.app["slug"]:
                    existing = cand            # upstream deleted + re-created the release (new id)
                else:
                    raise SyncError("RELEASE_CONFLICT",
                                    f"a destination release for {dest_tag} exists but is not managed by this system")
        prerelease = bool(rel.get("prerelease"))
        neutral = bool(self.policy.get("neutralize_mentions", True))
        entry = self._stub(rel, "unchanged", None)
        entry["title"] = title
        prev = self.prev_by_id.get(rel["id"], {})
        prev_assets = {a["name"]: a for a in prev.get("assets", [])}

        # ---- release object
        was_draft = False
        if existing is None:
            entry["action"] = "created" if not self.dry_run else "planned"
            if self.dry_run:
                release = {"id": 0, "assets": [], "html_url": None, "upload_url": ""}
            else:
                body = build_body(self.app, rel, dest_tag, commit, neutral, now_utc())
                release = self._dest("POST", f"/repos/{self.dest_repo}/releases", {
                    "tag_name": dest_tag, "name": title, "body": body, "draft": True,
                    "prerelease": prerelease, "generate_release_notes": False}).json()
            was_draft = True
        else:
            release = existing
            was_draft = bool(existing.get("draft"))
            want = build_body(self.app, rel, dest_tag, commit, neutral, now_utc())
            changed = (_normalize_body(want) != _normalize_body(existing.get("body") or "")
                       or existing.get("name") != title or bool(existing.get("prerelease")) != prerelease)
            if changed:
                entry["action"] = "updated" if not self.dry_run else "planned"
                if not self.dry_run:
                    release = self._dest("PATCH", f"/repos/{self.dest_repo}/releases/{existing['id']}", {
                        "name": title, "body": want, "prerelease": prerelease, "make_latest": "false"}).json()
        entry["dest_release_id"] = release.get("id")
        entry["dest_release_url"] = release.get("html_url")

        # ---- assets
        dest_assets = {a["name"]: a for a in release.get("assets", [])}
        ok = True
        for sa in rel.get("assets", []):
            res = self._asset(sa, dest_assets.get(sa["name"]), release, prev_assets.get(sa["name"]))
            entry["assets"].append(res)
            if res["status"] == "failed":
                ok = False
                self._fail(res.get("error") or "ASSET_UPLOAD_FAILED", f"{tag}: {sa['name']}")
            elif res["status"] in ("transferred", "planned") and entry["action"] == "unchanged":
                entry["action"] = "updated" if not self.dry_run else "planned"
        source_names = {a["name"] for a in rel.get("assets", [])}
        entry["stale_assets"] = sorted(n for n in prev_assets if n in dest_assets and n not in source_names)

        # ---- publish only when everything is in place
        if not ok:
            entry["action"] = "failed"
            entry["error"] = "ASSET_UPLOAD_FAILED"
            return entry
        if was_draft and not self.dry_run:
            pub = self._dest("PATCH", f"/repos/{self.dest_repo}/releases/{release['id']}", {
                "draft": False, "prerelease": prerelease, "make_latest": "false"}).json()
            entry["dest_release_url"] = pub.get("html_url")
            if entry["action"] == "unchanged":
                entry["action"] = "updated"
        return entry

    # -- one asset ---------------------------------------------------------
    def _unchanged(self, sa: Dict[str, Any], da: Optional[Dict[str, Any]], prev: Optional[Dict[str, Any]]) -> Optional[str]:
        """Return the known sha256 when the destination asset is provably identical, else None."""
        if not da or da.get("size") != sa.get("size"):
            return None
        sdig, ddig = hexdigest(sa.get("digest")), hexdigest(da.get("digest"))
        if sdig and ddig:
            return sdig if sdig == ddig else None
        owned = prev and prev.get("dest_asset_id") == da.get("id") and prev.get("sha256")
        if sdig and owned:
            return sdig if prev["sha256"] == sdig else None
        if not sdig and owned and prev.get("source_asset_id") == sa.get("id") \
                and prev.get("source_updated_at") == sa.get("updated_at"):
            return prev["sha256"]
        return None

    def _safe_upload_url(self, release: Dict[str, Any], name: str) -> str:
        url = (release.get("upload_url") or "").split("{")[0]
        host = urllib.parse.urlsplit(url).netloc.lower()
        allowed = {urllib.parse.urlsplit(self.dest_api).netloc.lower(), "uploads.github.com"}
        if host not in allowed:
            raise RuntimeError(f"refusing to send credentials to unexpected upload host '{host}'")
        return f"{url}?name={urllib.parse.quote(name)}"

    def _asset(self, sa: Dict[str, Any], da: Optional[Dict[str, Any]], release: Dict[str, Any],
               prev: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        rec = {"name": sa["name"], "source_asset_id": sa.get("id"), "source_updated_at": sa.get("updated_at"),
               "size": sa.get("size"), "sha256": hexdigest(sa.get("digest")),
               "dest_asset_id": (da or {}).get("id"), "status": "skipped"}
        if sa.get("state") not in (None, "uploaded"):
            rec["status"] = "skipped_not_uploaded"
            return rec
        if not sa.get("size"):
            warn(f"Skipping empty asset {sa['name']} (GitHub cannot host zero-byte release assets)")
            rec["status"] = "skipped_empty"
            return rec
        known = self._unchanged(sa, da, prev)
        if known:
            rec["sha256"] = known
            return rec
        if self.dry_run:
            rec["status"] = "planned"
            return rec
        path = os.path.join(self.workdir, str(sa.get("id") or "asset"))
        try:
            size, sha = download_to(sa["browser_download_url"], path)
            if size != sa["size"]:
                raise SyncError("ASSET_DOWNLOAD_FAILED", f"size {size} != expected {sa['size']}")
            sdig = hexdigest(sa.get("digest"))
            if self.policy.get("verify_asset_sha256", True) and sdig and sdig != sha:
                raise SyncError("ASSET_DIGEST_MISMATCH", "downloaded content does not match the upstream digest")
            if da:
                self._dest("DELETE", f"/repos/{self.dest_repo}/releases/assets/{da['id']}", ok=(204, 404))
            with open(path, "rb") as fh:
                up = http_request("POST", self._safe_upload_url(release, sa["name"]), token=self.dest_token,
                                  raw_body=fh, ok=(201,), timeout=3600, headers={
                                      "Content-Type": sa.get("content_type") or "application/octet-stream",
                                      "Content-Length": str(size)}).json()
            time.sleep(PACE)
            got = hexdigest(up.get("digest"))
            if up.get("size") != size or (got and got != sha):
                self._dest("DELETE", f"/repos/{self.dest_repo}/releases/assets/{up['id']}", ok=(204, 404))
                raise SyncError("ASSET_DIGEST_MISMATCH", "destination asset metadata does not match what was uploaded")
            rec.update({"status": "transferred", "sha256": sha, "dest_asset_id": up["id"],
                        "digest_verified": bool(got)})
        except SyncError as exc:
            rec.update({"status": "failed", "error": exc.category})
        except Exception as exc:
            warn(f"{sa['name']}: {exc}")
            rec.update({"status": "failed", "error": "ASSET_UPLOAD_FAILED"})
        finally:
            if os.path.exists(path):
                os.remove(path)
        return rec
