"""Git engine: default-branch discovery, workflow sanitisation (pure plumbing),
branch sync and tag sync for ONE application.

Nothing here checks out, builds or executes upstream content. Everything happens in
a throw-away bare repository using plumbing commands, so results are deterministic:
the same upstream state always produces the same destination commit SHAs.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from .common import (SyncError, clean_env, git_auth_env, git_out, http_request, log, run_git, warn)

BOT_NAME = os.environ.get("CRAFT_SUITE_GIT_NAME", "Craft Suite Sync")
BOT_EMAIL = os.environ.get("CRAFT_SUITE_GIT_EMAIL", "craft-suite-sync@users.noreply.github.com")

WORKFLOW_DIR = ".github/workflows"
DEPENDABOT_FILES = (".github/dependabot.yml", ".github/dependabot.yaml")
ARCHIVE_WORKFLOWS = ".upstream-archive/workflows/"
ARCHIVE_GITHUB = ".upstream-archive/github/"
TAG_METADATA_DIR = ".upstream-archive/tag-metadata/"
PUSH_CHUNK = 100


# ------------------------------------------------------------ default branch --
def parse_symref(ls_remote_output: str) -> Optional[str]:
    for line in ls_remote_output.splitlines():
        m = re.match(r"^ref:\s+refs/heads/(.+?)\s+HEAD$", line.strip())
        if m:
            return m.group(1)
    return None


def discover_default_branch(upstream_url: str, repository: str, api_url: Optional[str],
                            api_token: Optional[str]) -> str:
    """ls-remote --symref first; GitHub API as fallback and as a cross-check.

    Never assumes main/master. Disagreement between the two sources fails the app safely.
    """
    primary: Optional[str] = None
    try:
        proc = run_git(["ls-remote", "--symref", upstream_url, "HEAD"], check=False, timeout=120)
        if proc.returncode == 0:
            primary = parse_symref(proc.stdout.decode("utf-8", "replace"))
    except Exception as exc:  # network/timeouts
        warn(f"ls-remote failed for {repository}: {exc}")

    api_branch: Optional[str] = None
    if api_url:
        try:
            res = http_request("GET", f"{api_url.rstrip('/')}/repos/{repository}", token=api_token, retries=2)
            api_branch = (res.json() or {}).get("default_branch")
        except Exception as exc:
            if primary is None:
                warn(f"API default-branch lookup failed for {repository}: {exc}")

    if primary and api_branch and primary != api_branch:
        raise SyncError("DEFAULT_BRANCH_UNKNOWN",
                        f"ls-remote reports '{primary}' but the API reports '{api_branch}'; refusing to guess")
    branch = primary or api_branch
    if not branch:
        raise SyncError("DEFAULT_BRANCH_UNKNOWN", f"cannot determine the default branch of {repository}")
    return branch


# --------------------------------------------------------------- sanitising --
def _parse_ls_tree_z(raw: bytes) -> List[Tuple[str, str, str, str]]:
    entries = []
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        meta, _, path = rec.partition(b"\t")
        mode, typ, sha = meta.decode().split(" ")
        entries.append((mode, typ, sha, path.decode("utf-8", "surrogateescape")))
    return entries


def sanitizable_entries(repo: str, commit: str) -> List[Tuple[str, str, str, str]]:
    raw = run_git(["ls-tree", "-r", "-z", "--full-tree", commit, "--", WORKFLOW_DIR] + list(DEPENDABOT_FILES),
                  cwd=repo).stdout
    return _parse_ls_tree_z(raw)


def archive_path(path: str) -> str:
    if path.startswith(WORKFLOW_DIR + "/"):
        return ARCHIVE_WORKFLOWS + path[len(WORKFLOW_DIR) + 1:]
    if path == WORKFLOW_DIR:  # a symlink standing in for the directory
        return ".upstream-archive/workflows-symlink"
    return ARCHIVE_GITHUB + path.rsplit("/", 1)[-1]


def sanitized_commit(repo: str, commit: str, message: str,
                     extra_files: Optional[Dict[str, bytes]] = None) -> Tuple[str, int]:
    """Return (sha, archived_file_count).

    If the commit has no workflow/dependabot files and there are no extra files, the
    original commit is returned unchanged. Otherwise a child commit (parent = original)
    is created in which upstream workflows are moved to .upstream-archive/ and the
    extra files are added. Dates/identity are fixed => deterministic SHA.
    """
    extra_files = extra_files or {}
    entries = sanitizable_entries(repo, commit)
    if not entries and not extra_files:
        return commit, 0

    with tempfile.TemporaryDirectory(prefix="cs-idx-") as td:
        env = clean_env()
        env["GIT_INDEX_FILE"] = os.path.join(td, "index")
        run_git(["read-tree", commit], cwd=repo, env=env)
        zeros = "0" * len(entries[0][2]) if entries else "0" * 40
        buf = bytearray()
        for mode, _typ, sha, path in entries:
            buf += f"0 {zeros}\t{path}\0".encode("utf-8", "surrogateescape")
        for mode, _typ, sha, path in entries:
            buf += f"{mode} {sha}\t{archive_path(path)}\0".encode("utf-8", "surrogateescape")
        for path, data in sorted(extra_files.items()):
            blob = run_git(["hash-object", "-w", "--stdin"], cwd=repo, input_bytes=data).stdout.decode().strip()
            buf += f"100644 {blob}\t{path}\0".encode()
        run_git(["update-index", "-z", "--index-info"], cwd=repo, env=env, input_bytes=bytes(buf))
        tree = run_git(["write-tree"], cwd=repo, env=env).stdout.decode().strip()

    date = git_out(["show", "-s", "--format=%cI", commit], cwd=repo)
    cenv = clean_env()
    cenv.update({"GIT_AUTHOR_NAME": BOT_NAME, "GIT_AUTHOR_EMAIL": BOT_EMAIL, "GIT_AUTHOR_DATE": date,
                 "GIT_COMMITTER_NAME": BOT_NAME, "GIT_COMMITTER_EMAIL": BOT_EMAIL, "GIT_COMMITTER_DATE": date})
    new = run_git(["commit-tree", tree, "-p", commit, "-m", message], cwd=repo, env=cenv).stdout.decode().strip()
    return new, len(entries)


# ------------------------------------------------------------ remote helpers --
def ls_remote(url: str, env: Dict[str, str], patterns: List[str]) -> Dict[str, str]:
    proc = run_git(["ls-remote", url] + patterns, env=env, check=False, timeout=180)
    if proc.returncode != 0:
        raise SyncError("DEST_UNAVAILABLE", proc.stderr.decode("utf-8", "replace").strip()[-300:])
    refs: Dict[str, str] = {}
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        sha, _, ref = line.partition("\t")
        if ref and not ref.endswith("^{}"):
            refs[ref] = sha
    return refs


def push_specs(repo: str, url: str, env: Dict[str, str], specs: List[str]) -> None:
    """Push explicit '+sha:ref' refspecs in chunks. Never --mirror, never --all."""
    for i in range(0, len(specs), PUSH_CHUNK):
        run_git(["push", "--no-verify", url] + specs[i:i + PUSH_CHUNK], cwd=repo, env=env, timeout=3600)


def check_ref_format(ref: str) -> bool:
    return run_git(["check-ref-format", ref], check=False).returncode == 0


# ------------------------------------------------------------------ app sync --
class AppGitSync:
    def __init__(self, app: Dict[str, Any], repo: str, dest_url: str, dest_token: Optional[str],
                 dry_run: bool, overlay_files: Optional[Dict[str, bytes]] = None):
        self.app = app
        self.slug = app["slug"]
        self.repo = repo
        self.dest_url = dest_url
        self.dest_env = git_auth_env(dest_url, dest_token)
        self.dry_run = dry_run
        self.overlay = overlay_files if self.slug == "craft" else None
        self.default_branch = ""
        self.src_head = ""
        self.dest_tags: Dict[str, str] = {}      # dest tag name -> sha currently on destination
        self.tag_targets: Dict[str, str] = {}    # source tag name -> original commit sha
        self.wanted_tags: Dict[str, str] = {}
        self.ns = f"refs/craft-suite/{self.slug}"

    # -- fetch -------------------------------------------------------------
    def init_workspace(self) -> None:
        run_git(["init", "--bare", "--quiet", self.repo])

    def fetch(self, branch: str, with_tags: bool) -> None:
        self.default_branch = branch
        specs = [f"+refs/heads/{branch}:{self.ns}/heads/{branch}"]
        if with_tags:
            specs.append(f"+refs/tags/*:{self.ns}/tags/*")
        try:
            run_git(["fetch", "--no-tags", "--force", self.app["upstream_url"]] + specs,
                    cwd=self.repo, env=clean_env(), timeout=7200)
        except RuntimeError as exc:
            raise SyncError("BRANCH_FETCH_FAILED", str(exc))
        self.src_head = git_out(["rev-parse", "--verify", f"{self.ns}/heads/{branch}"], cwd=self.repo)

    # -- branch ------------------------------------------------------------
    def sync_branch(self) -> Dict[str, Any]:
        branch = self.app["destination_branch"]
        msg = ("Archive upstream workflows and install Craft Suite Sync [skip ci]" if self.overlay
               else "Archive upstream workflows for manual activation [skip ci]")
        try:
            new, archived = sanitized_commit(self.repo, self.src_head, msg, self.overlay)
        except Exception as exc:
            raise SyncError("WORKFLOW_SANITIZATION_FAILED", str(exc))
        current = ls_remote(self.dest_url, self.dest_env, [f"refs/heads/{branch}"]).get(f"refs/heads/{branch}")
        out = {"destination_head_sha": new, "archived_files": archived, "status": "unchanged"}
        if current == new:
            return out
        out["status"] = "planned" if self.dry_run else ("created" if current is None else "updated")
        if self.dry_run:
            return out
        run_git(["update-ref", f"{self.ns}/push/head", new], cwd=self.repo)
        try:
            self._lfs_before_push(out, f"{self.ns}/push/head")
            push_specs(self.repo, self.dest_url, self.dest_env, [f"+{new}:refs/heads/{branch}"])
        except SyncError:
            raise
        except Exception as exc:
            raise SyncError("BRANCH_PUSH_FAILED", str(exc))
        return out

    def _lfs_before_push(self, out: Dict[str, Any], push_ref: str) -> None:
        attrs = run_git(["cat-file", "-p", f"{self.src_head}:.gitattributes"], cwd=self.repo, check=False)
        uses_lfs = attrs.returncode == 0 and b"filter=lfs" in attrs.stdout
        out["lfs"] = "not_used"
        if not uses_lfs:
            return
        if run_git(["lfs", "version"], check=False).returncode != 0:
            out["lfs"] = "unavailable"
            raise SyncError("LFS_UNAVAILABLE", "upstream uses Git LFS but git-lfs is not installed on the runner")
        try:
            # Scoped to THIS app's refs only: never --all.
            run_git(["lfs", "fetch", self.app["upstream_url"], f"{self.ns}/heads/{self.default_branch}"],
                    cwd=self.repo, env=clean_env(), timeout=7200)
            run_git(["lfs", "push", self.dest_url, push_ref], cwd=self.repo, env=self.dest_env, timeout=7200)
            out["lfs"] = "mirrored"
        except RuntimeError as exc:
            out["lfs"] = "failed"
            raise SyncError("LFS_TRANSFER_FAILED", str(exc))

    # -- tags --------------------------------------------------------------
    def _source_tags(self) -> List[Tuple[str, str, str]]:
        raw = git_out(["for-each-ref", "--format=%(refname)%09%(objectname)%09%(objecttype)", f"{self.ns}/tags"],
                      cwd=self.repo)
        tags = []
        for line in raw.splitlines():
            ref, sha, typ = line.split("\t")
            tags.append((ref[len(f"{self.ns}/tags/"):], sha, typ))
        return sorted(tags)

    def _peel(self, shas: List[str]) -> Dict[str, Tuple[str, str]]:
        if not shas:
            return {}
        inp = "".join(f"{s}^{{}}\n" for s in shas).encode()
        out = run_git(["cat-file", "--batch-check=%(objecttype) %(objectname)"], cwd=self.repo,
                      input_bytes=inp).stdout.decode().splitlines()
        return {s: tuple(line.split(" ", 1)) for s, line in zip(shas, out)}  # type: ignore[misc]

    def sync_tags(self) -> Dict[str, Any]:
        prefix = f"{self.slug}--"
        res = {"processed": 0, "created": 0, "updated": 0, "unchanged": 0, "sanitized": 0,
               "invalid": [], "stale": [], "failed": 0}
        dest_map = ls_remote(self.dest_url, self.dest_env, [f"refs/tags/{prefix}*"])
        self.dest_tags = {ref[len("refs/tags/"):]: sha for ref, sha in dest_map.items()}
        tags = self._source_tags()
        peeled = self._peel([sha for _, sha, _ in tags])
        wanted: Dict[str, str] = {}
        to_push: List[str] = []
        for name, sha, typ in tags:
            res["processed"] += 1
            dest_name = prefix + name
            if not check_ref_format(f"refs/tags/{dest_name}"):
                res["invalid"].append(name)
                continue
            ptype, psha = peeled.get(sha, (typ, sha))
            expected = sha
            if ptype == "commit":
                self.tag_targets[name] = psha
                if sanitizable_entries(self.repo, psha):
                    meta = self._tag_metadata(name, dest_name, sha, typ, psha)
                    meta_path = TAG_METADATA_DIR + hashlib.sha256(name.encode()).hexdigest() + ".txt"
                    expected, _ = sanitized_commit(self.repo, psha, f"Archive upstream workflows for tag {name} [skip ci]",
                                                   {meta_path: meta})
                    res["sanitized"] += 1
            wanted[dest_name] = expected
            cur = self.dest_tags.get(dest_name)
            if cur == expected:
                res["unchanged"] += 1
            else:
                res["created" if cur is None else "updated"] += 1
                to_push.append(f"+{expected}:refs/tags/{dest_name}")
        res["stale"] = sorted(n for n in self.dest_tags if n not in wanted)  # report-only, never deleted
        if to_push and not self.dry_run:
            try:
                push_specs(self.repo, self.dest_url, self.dest_env, to_push)
            except Exception as exc:
                res["failed"] = len(to_push)
                raise SyncError("TAG_PUSH_FAILED", str(exc))
        self.wanted_tags = wanted
        if not self.dry_run:
            self.dest_tags.update(wanted)
        return res

    def load_dest_tags(self) -> None:
        """Used when tag sync is disabled but releases still need to know what exists."""
        prefix = f"{self.slug}--"
        m = ls_remote(self.dest_url, self.dest_env, [f"refs/tags/{prefix}*"])
        self.dest_tags = {ref[len("refs/tags/"):]: sha for ref, sha in m.items()}

    def tag_available(self, dest_name: str) -> bool:
        return dest_name in self.dest_tags or (self.dry_run and dest_name in self.wanted_tags)

    def _tag_metadata(self, name: str, dest_name: str, obj: str, typ: str, commit: str) -> bytes:
        lines = [f"Original tag name: {name}", f"Destination tag name: {dest_name}",
                 f"Original tag object: {obj}", f"Original target commit: {commit}",
                 f"Original object type: {typ}", ""]
        if typ == "tag":
            lines.append("--- Original annotated tag object body ---")
            lines.append(git_out(["cat-file", "-p", obj], cwd=self.repo))
        else:
            lines.append("Lightweight tag; the original target commit is recorded above.")
        return ("\n".join(lines) + "\n").encode("utf-8", "surrogateescape")
