"""Command line entry points used by the workflow (and by the tests)."""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys
import tempfile
import traceback
from typing import Any, Dict, List, Optional

from . import catalog
from .common import SyncError, err, log, read_json, warn, write_json_atomic, write_text_atomic
from .gitsync import AppGitSync, discover_default_branch
from .manifest import get_app, load_manifest, plan_apps, validate_manifest
from .releases import ReleaseSync

ROOT = os.environ.get("CRAFT_SUITE_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MANIFEST_PATH = os.path.join(ROOT, "config", "apps.json")
STATE_DIR = os.path.join(ROOT, "state")
STATE_PATH = os.path.join(STATE_DIR, "sync-state.json")


def truthy(v: Optional[str]) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes")


def render_workflow(template: str, manifest: Dict[str, Any], app: Dict[str, Any]) -> str:
    return (template.replace("__SLUG__", app["slug"]).replace("__APP_NAME__", app["name"])
            .replace("__CONTROL_BRANCH__", manifest["destination"]["control_branch"]))


def render_pointer(template: str, manifest: Dict[str, Any]) -> str:
    rows = "\n".join(f"| {a['name']} | `{a['destination_branch']}` | https://github.com/{a['repository']} |"
                     for a in manifest["apps"])
    return (template.replace("__APP_TABLE__", rows)
            .replace("__CONTROL_BRANCH__", manifest["destination"]["control_branch"])
            .replace("__REPOSITORY__", manifest["destination"]["repository"]))


def overlay_files(manifest: Dict[str, Any]) -> Dict[str, bytes]:
    with open(os.path.join(ROOT, "workflow", "app-sync.yml"), "r", encoding="utf-8") as fh:
        tpl = fh.read()
    with open(os.path.join(ROOT, "templates", "CRAFT-SUITE.md"), "r", encoding="utf-8") as fh:
        md = render_pointer(fh.read(), manifest)
    files = {f".github/workflows/sync-{a['slug']}.yml": render_workflow(tpl, manifest, a).encode("utf-8")
             for a in manifest["apps"] if a["enabled"]}
    files["CRAFT-SUITE.md"] = md.encode("utf-8")
    return files


def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")
    log(f"{name}={value}")


# ------------------------------------------------------------------ commands --
def cmd_validate(args) -> int:
    m = load_manifest(args.manifest or MANIFEST_PATH)
    problems = validate_manifest(m, strict_count=args.strict_count)
    for p in problems:
        err(p)
    log("manifest OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_plan(args) -> int:
    m = load_manifest(MANIFEST_PATH)
    problems = validate_manifest(m)
    if problems:
        for p in problems:
            err(p)
        return 1
    plan = plan_apps(m, os.environ.get("EVENT_NAME", ""), os.environ.get("INPUT_APP", ""),
                     os.environ.get("AUTO_SYNC_VAR", ""))
    matrix = [{"slug": a["slug"], "name": a["name"]} for a in plan["apps"]]
    if plan["reason"]:
        log(plan["reason"])
    set_output("count", str(len(matrix)))
    set_output("matrix", json.dumps(matrix, separators=(",", ":")))
    return 0


def _new_result(app: Dict[str, Any]) -> Dict[str, Any]:
    return {"slug": app["slug"], "status": "success", "default_branch": None, "src_head": None,
            "branch": None, "tags": None, "releases": None, "errors": [], "warnings": []}


def cmd_sync_app(args) -> int:
    m = load_manifest(MANIFEST_PATH)
    app = get_app(m, args.slug)
    dry = args.dry_run or truthy(os.environ.get("DRY_RUN"))
    result = _new_result(app)
    result["dry_run"] = dry
    work = tempfile.mkdtemp(prefix=f"cs-{app['slug']}-")
    try:
        if not app["enabled"]:
            result["status"] = "disabled"
            return _finish(result, args.out)
        problems = validate_manifest(m)
        if problems:
            raise SyncError("MANIFEST", "; ".join(problems))

        dest_repo = os.environ.get("CRAFT_SUITE_DEST_REPO") or m["destination"]["repository"]
        gh_repo = os.environ.get("GITHUB_REPOSITORY")
        if gh_repo and not os.environ.get("CRAFT_SUITE_DEST_REPO") and gh_repo.lower() != dest_repo.lower():
            raise SyncError("DEST_MISMATCH", f"running in {gh_repo} but the manifest targets {dest_repo}")
        dest_url = os.environ.get("DEST_GIT_URL") or f"https://github.com/{dest_repo}.git"
        push_token = os.environ.get("GH_SYNC_TOKEN") or None
        if not dry and dest_url.startswith("https://") and not push_token:
            raise SyncError("AUTH_MISSING", "secret GH_SYNC_TOKEN is required to push (see docs/SETUP.md)")
        api_token = os.environ.get("GITHUB_TOKEN") or push_token
        src_api = os.environ.get("SRC_API_URL", "https://api.github.com")
        dest_api = os.environ.get("DEST_API_URL") or os.environ.get("GITHUB_API_URL") or "https://api.github.com"
        src_token = os.environ.get("UPSTREAM_API_TOKEN") or os.environ.get("GITHUB_TOKEN") or None  # never the PAT

        g = AppGitSync(app, os.path.join(work, "repo.git"), dest_url, push_token, dry,
                       overlay_files(m) if app["slug"] == "craft" else None)
        g.init_workspace()
        try:
            branch = discover_default_branch(app["upstream_url"], app["repository"], src_api, src_token)
            g.fetch(branch, app["sync_tags"])
        except SyncError as exc:
            result["status"] = "failed"
            result["errors"].append({"category": exc.category, "message": exc.message})
            return _finish(result, args.out)
        result["default_branch"], result["src_head"] = branch, g.src_head

        if app["sync_source_default_branch"]:
            try:
                result["branch"] = g.sync_branch()
            except SyncError as exc:
                result["errors"].append({"category": exc.category, "message": exc.message})
        if app["sync_tags"]:
            try:
                result["tags"] = g.sync_tags()
                if result["tags"]["invalid"]:
                    result["errors"].append({"category": "TAG_MAPPING_CONFLICT",
                                             "message": f"{len(result['tags']['invalid'])} tag name(s) cannot be mapped"})
                result["tags"]["stale"] = result["tags"]["stale"][:50]
                result["tags"]["invalid"] = result["tags"]["invalid"][:50]
            except SyncError as exc:
                result["errors"].append({"category": exc.category, "message": exc.message})
        if app["sync_releases"]:
            try:
                if not app["sync_tags"]:
                    g.load_dest_tags()
                prev = (read_json(STATE_PATH, {}) or {}).get("apps", {}).get(app["slug"])
                rs = ReleaseSync(app, m["release_policy"], src_api, src_token, dest_api, dest_repo,
                                 api_token, prev, g, dry)
                result["releases"] = rs.run()
                result["errors"].extend(result["releases"].pop("errors"))
            except SyncError as exc:
                result["errors"].append({"category": exc.category, "message": exc.message})
        if result["errors"]:
            result["status"] = "partial"
    except SyncError as exc:
        result["status"] = "failed"
        result["errors"].append({"category": exc.category, "message": exc.message})
    except Exception as exc:  # last resort: still write a result file so finalize can report it
        traceback.print_exc()
        result["status"] = "failed"
        result["errors"].append({"category": "INTERNAL", "message": str(exc)[:500]})
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return _finish(result, args.out)


def _finish(result: Dict[str, Any], out: str) -> int:
    for e in result["errors"]:
        err(f"[{result['slug']}] {e['category']}: {e['message']}")
    write_json_atomic(out, result)
    log(f"[{result['slug']}] status={result['status']}")
    return 0 if result["status"] in ("success", "disabled") else 1


def cmd_finalize(args) -> int:
    m = load_manifest(MANIFEST_PATH)
    results: List[Dict[str, Any]] = []
    seen = set()
    for p in sorted(glob.glob(os.path.join(args.results, "result-*.json"))):
        r = read_json(p)
        if r:
            results.append(r)
            seen.add(r["slug"])
    planned = json.loads(os.environ.get("PLANNED_MATRIX") or "[]")
    for item in planned:                     # a leg that died before writing its result
        if item["slug"] not in seen:
            results.append({**_new_result(get_app(m, item["slug"])), "status": "failed",
                            "errors": [{"category": "NO_RESULT", "message": "the sync job produced no result"}]})
    results = [r for r in results if not r.get("dry_run")]
    prev = read_json(STATE_PATH, {}) or {}
    state = catalog.merge_state(prev, results, m)
    repo = m["destination"]["repository"]
    write_json_atomic(STATE_PATH, state)
    write_json_atomic(os.path.join(STATE_DIR, "release-catalog.json"), catalog.build_catalog(state))
    write_text_atomic(os.path.join(ROOT, "CRAFT-SUITE-INDEX.md"), catalog.render_index(state, repo))
    _summary(results)
    return 0


def _summary(results: List[Dict[str, Any]]) -> None:
    lines = ["| App | Status | Branch | Tags (new/upd/ok) | Releases | Errors |", "|---|---|---|---|---|---|"]
    for r in results:
        t = r.get("tags") or {}
        rc = ((r.get("releases") or {}).get("counts")) or {}
        errs = ", ".join(sorted({e["category"] for e in r["errors"]})) or "-"
        lines.append(f"| {r['slug']} | {r['status']} | {(r.get('branch') or {}).get('status', '-')} | "
                     f"{t.get('created', 0)}/{t.get('updated', 0)}/{t.get('unchanged', 0)} | "
                     f"{', '.join(f'{k}={v}' for k, v in sorted(rc.items())) or '-'} | {errs} |")
    text = "\n".join(lines) + "\n"
    log(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("## Craft Suite Sync\n\n" + text)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="craft_sync")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate")
    v.add_argument("--manifest")
    v.add_argument("--strict-count", action="store_true")
    sub.add_parser("plan")
    s = sub.add_parser("sync-app")
    s.add_argument("--slug", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--dry-run", action="store_true")
    f = sub.add_parser("finalize")
    f.add_argument("--results", required=True)
    args = ap.parse_args(argv)
    return {"validate": cmd_validate, "plan": cmd_plan, "sync-app": cmd_sync_app, "finalize": cmd_finalize}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
