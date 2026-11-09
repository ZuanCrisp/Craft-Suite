"""Manifest loading/validation and the schedule/manual planning logic."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from .common import SyncError

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")          # no '--', no trailing '-'
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
CRON_RE = re.compile(r"^([0-9*/,-]+)\s+([0-9*/,-]+)\s+([0-9*/,-]+)\s+([0-9*/,-]+)\s+([0-9*/,-]+)$")
BOOL_APP_KEYS = ("enabled", "auto_sync", "sync_source_default_branch", "sync_tags",
                 "sync_releases", "sync_prereleases")
EXPECTED_APP_COUNT = 17


def load_manifest(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def validate_manifest(m: Dict[str, Any], strict_count: bool = False) -> List[str]:
    """Return a list of problems (empty = valid)."""
    problems: List[str] = []
    if m.get("schema_version") != 1:
        problems.append("schema_version must be 1")
    dest = m.get("destination") or {}
    if not REPO_RE.match(str(dest.get("repository", ""))):
        problems.append("destination.repository must look like OWNER/REPOSITORY")
    if not dest.get("default_branch"):
        problems.append("destination.default_branch is required")
    if not dest.get("control_branch"):
        problems.append("destination.control_branch is required")
    if dest.get("control_branch") == dest.get("default_branch"):
        problems.append("control_branch must differ from default_branch")
    if (m.get("automation") or {}).get("mode") != "manual-per-app":
        problems.append('automation.mode must be "manual-per-app" (no scheduled sync)')
    policy = m.get("release_policy") or {}
    if policy.get("make_latest") != "false":
        problems.append('release_policy.make_latest must be the string "false"')
    if policy.get("include_drafts") not in (False, None):
        problems.append("release_policy.include_drafts must be false")

    apps = m.get("apps")
    if not isinstance(apps, list) or not apps:
        problems.append("apps must be a non-empty list")
        return problems
    if strict_count and len(apps) != EXPECTED_APP_COUNT:
        problems.append(f"expected exactly {EXPECTED_APP_COUNT} apps, found {len(apps)}")
    slugs, branches = set(), set()
    reserved = {dest.get("control_branch")}
    for i, a in enumerate(apps):
        tag = f"apps[{i}]"
        slug = str(a.get("slug", ""))
        if not SLUG_RE.match(slug):
            problems.append(f"{tag}: invalid slug '{slug}'")
        if slug in slugs:
            problems.append(f"{tag}: duplicate slug '{slug}'")
        slugs.add(slug)
        br = str(a.get("destination_branch", ""))
        if not br:
            problems.append(f"{tag}: destination_branch missing")
        if br in branches:
            problems.append(f"{tag}: duplicate destination_branch '{br}'")
        if br in reserved:
            problems.append(f"{tag}: destination_branch '{br}' collides with the control branch")
        branches.add(br)
        repo = str(a.get("repository", ""))
        if not REPO_RE.match(repo):
            problems.append(f"{tag}: invalid repository '{repo}'")
        url = str(a.get("upstream_url", ""))
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.netloc:
            problems.append(f"{tag}: upstream_url must be HTTPS")
        elif repo and parts.path.strip("/").removesuffix(".git").lower() != repo.lower():
            problems.append(f"{tag}: upstream_url does not match repository '{repo}'")
        if not a.get("name"):
            problems.append(f"{tag}: name missing")
        for k in BOOL_APP_KEYS:
            if not isinstance(a.get(k), bool):
                problems.append(f"{tag}: '{k}' must be true or false")
    if dest.get("default_branch") not in branches:
        problems.append("destination.default_branch must be the destination_branch of one app")
    return problems


def get_app(m: Dict[str, Any], slug: str) -> Dict[str, Any]:
    for a in m["apps"]:
        if a["slug"] == slug:
            return a
    raise SyncError("MANIFEST", f"unknown app slug '{slug}'")


def tag_prefix(app: Dict[str, Any]) -> str:
    return f"{app['slug']}--"


def plan_apps(m: Dict[str, Any], event: str, input_app: str, auto_sync_var: str) -> Dict[str, Any]:
    """Decide which apps run.

    schedule          : blocked by the global variable CRAFT_SUITE_AUTO_SYNC=false;
                        otherwise enabled AND auto_sync apps.
    workflow_dispatch : always allowed. 'all' (or empty) = enabled AND auto_sync apps;
                        an explicit slug = that app if enabled (auto_sync is ignored).
    """
    event = (event or "").strip()
    sel = (input_app or "all").strip().lower()
    apps = m["apps"]
    reason = ""
    if event == "schedule":
        if (auto_sync_var or "").strip().lower() == "false":
            return {"apps": [], "reason": "CRAFT_SUITE_AUTO_SYNC is false: scheduled sync disabled"}
        chosen = [a for a in apps if a["enabled"] and a["auto_sync"]]
    else:
        if sel in ("", "all"):
            chosen = [a for a in apps if a["enabled"] and a["auto_sync"]]
        else:
            match = [a for a in apps if a["slug"] == sel]
            if not match:
                raise SyncError("MANIFEST", f"unknown app slug '{sel}'")
            if not match[0]["enabled"]:
                return {"apps": [], "reason": f"app '{sel}' is disabled (enabled=false) in the manifest"}
            chosen = match
    if not chosen:
        reason = "no apps are eligible for this run"
    return {"apps": chosen, "reason": reason}
