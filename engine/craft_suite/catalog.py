"""State merge, per-app release catalog and the human-readable index.

Idempotency rule: timestamps change ONLY when real content changed, so a no-op run
produces no diff and therefore no commit on the control branch.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from .releases import now_utc

SUCCESS_STATES = ("success",)


def _strip_ts(d: Dict[str, Any]) -> Dict[str, Any]:
    d = copy.deepcopy(d)
    d.pop("last_success_at", None)
    return d


def _stable_entry(e: Dict[str, Any]) -> Dict[str, Any]:
    """Run-specific verbs (created/updated/unchanged/transferred/skipped) must not be persisted:
    they would make every run look like a change. Persist only durable state."""
    e = copy.deepcopy(e)
    if e.get("action") in ("created", "updated", "unchanged", "planned"):
        e["action"] = "synced"
    for a in e.get("assets", []):
        if a.get("status") in ("transferred", "skipped", "planned"):
            a["status"] = "synced"
        a.pop("digest_verified", None)
    return e


def _stable_tags(t: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not t:
        return None
    return {k: t[k] for k in ("processed", "sanitized", "invalid", "stale") if k in t}


def merge_app(prev: Optional[Dict[str, Any]], result: Dict[str, Any], app: Dict[str, Any]) -> Dict[str, Any]:
    prev = prev or {}
    new: Dict[str, Any] = {
        "slug": app["slug"], "name": app["name"], "repository": app["repository"],
        "destination_branch": app["destination_branch"], "enabled": app["enabled"], "auto_sync": app["auto_sync"],
        "status": result["status"],
        "errors": result.get("errors", []),
        "upstream_default_branch": result.get("default_branch") or prev.get("upstream_default_branch"),
        "upstream_head_sha": result.get("src_head") or prev.get("upstream_head_sha"),
        "destination_head_sha": (result.get("branch") or {}).get("destination_head_sha")
        or prev.get("destination_head_sha"),
        "lfs": (result.get("branch") or {}).get("lfs") or prev.get("lfs"),
        "tags": _stable_tags(result.get("tags")) or prev.get("tags"),
    }
    rel = result.get("releases")
    if rel is None:                         # release phase did not run / failed: keep the last known catalog
        new["releases"] = prev.get("releases", [])
        new["latest_stable"] = prev.get("latest_stable")
        new["latest_prerelease"] = prev.get("latest_prerelease")
    else:
        prev_by_id = {r["upstream_release_id"]: r for r in prev.get("releases", [])}
        merged: List[Dict[str, Any]] = []
        for e in rel["releases"]:
            old = prev_by_id.get(e["upstream_release_id"])
            if e["action"] in ("failed",) and old:
                e = dict(old, action="failed", error=e.get("error"))
            elif e["action"] == "blocked" and old:
                e = dict(old, action="blocked", error=e.get("error"))
            e.pop("stale", None)
            merged.append(_stable_entry(e))
        live = {e["upstream_release_id"] for e in merged}
        gone = set(rel.get("source_release_ids", []))
        for old in prev.get("releases", []):       # deleted upstream: keep, flag, never delete
            if old["upstream_release_id"] not in live and old["upstream_release_id"] not in gone:
                merged.append(dict(old, stale=True))
        merged.sort(key=lambda r: (r.get("published_at") or "", r["upstream_release_id"]), reverse=True)
        new["releases"] = merged
        new["latest_stable"] = rel.get("latest_stable")
        new["latest_prerelease"] = rel.get("latest_prerelease")
    if _strip_ts(prev) != _strip_ts(new):
        new["last_success_at"] = now_utc() if result["status"] == "success" else prev.get("last_success_at")
    else:
        new["last_success_at"] = prev.get("last_success_at")
    return new


def merge_state(prev_state: Dict[str, Any], results: List[Dict[str, Any]], manifest: Dict[str, Any]) -> Dict[str, Any]:
    apps_prev = (prev_state or {}).get("apps", {})
    apps_new = dict(apps_prev)
    by_slug = {a["slug"]: a for a in manifest["apps"]}
    for res in results:
        apps_new[res["slug"]] = merge_app(apps_prev.get(res["slug"]), res, by_slug[res["slug"]])
    for slug, a in by_slug.items():          # reflect manifest toggles even for apps that did not run
        if slug in apps_new:
            apps_new[slug]["enabled"], apps_new[slug]["auto_sync"] = a["enabled"], a["auto_sync"]
            if not a["enabled"]:
                apps_new[slug]["status"] = "disabled"
    ordered = {s: apps_new[s] for s in (a["slug"] for a in manifest["apps"]) if s in apps_new}
    state = {"schema_version": 1, "destination": manifest["destination"]["repository"], "apps": ordered}
    if _strip_gen(prev_state) != _strip_gen(state):
        state["generated_at"] = now_utc()
    else:
        state["generated_at"] = (prev_state or {}).get("generated_at")
    return state


def _strip_gen(s: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    s = copy.deepcopy(s or {})
    s.pop("generated_at", None)
    for a in s.get("apps", {}).values():
        a.pop("last_success_at", None)
    return s


def build_catalog(state: Dict[str, Any]) -> Dict[str, Any]:
    apps = {}
    for slug, a in state["apps"].items():
        apps[slug] = {
            "name": a["name"], "repository": a["repository"], "destination_branch": a["destination_branch"],
            "status": a["status"], "upstream_default_branch": a.get("upstream_default_branch"),
            "latest_stable": a.get("latest_stable"), "latest_prerelease": a.get("latest_prerelease"),
            "release_count": len([r for r in a.get("releases", []) if not r.get("stale")]),
        }
    return {"schema_version": 1, "generated_at": state.get("generated_at"), "apps": apps}


def render_index(state: Dict[str, Any], repo: str) -> str:
    def rel_link(x: Optional[Dict[str, Any]]) -> str:
        if not x:
            return "-"
        url = x.get("destination_release_url")
        label = x["source_tag"]
        return f"[{label}]({url})" if url else label

    lines = ["# Craft Suite sync index", "",
             "Generated by Craft Suite Sync. Do not edit by hand.", "",
             "| App | Branch | Upstream branch | Latest stable | Latest prerelease | Releases | Status |",
             "|---|---|---|---|---|---|---|"]
    for slug, a in state["apps"].items():
        branch = f"[`{a['destination_branch']}`](https://github.com/{repo}/tree/{a['destination_branch']})"
        n = len([r for r in a.get("releases", []) if not r.get("stale")])
        lines.append(f"| [{a['name']}](https://github.com/{a['repository']}) | {branch} | "
                     f"`{a.get('upstream_default_branch') or '?'}` | {rel_link(a.get('latest_stable'))} | "
                     f"{rel_link(a.get('latest_prerelease'))} | {n} | {a['status']} |")
    lines += ["", f"Last change: {state.get('generated_at') or 'never'} (UTC)", ""]
    return "\n".join(lines)
