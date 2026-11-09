"""End-to-end through the real CLI (subprocess) against local fixtures + the mock GitHub API.

`https://github.com/<owner>/<repo>.git` is redirected to local bare repos with git's own
url.insteadOf, so the production manifest (HTTPS URLs) is exercised unchanged.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from mock_github import MockGitHub  # noqa
import gitfix  # noqa
from gitfix import g  # noqa

PKG = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class E2E(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.root = os.path.join(self.td, "control")
        for d in ("engine", "config", "workflow", "templates"):
            shutil.copytree(os.path.join(PKG, d), os.path.join(self.root, d),
                            ignore=shutil.ignore_patterns("__pycache__"))
        os.makedirs(os.path.join(self.td, "up", "storytold"))
        work = os.path.join(self.td, "work")
        gitfix.make_upstream(work)
        subprocess.run(["git", "clone", "-q", "--bare", work, os.path.join(self.td, "up", "storytold", "photocraft.git")], check=True)
        self.dest = gitfix.bare(os.path.join(self.td, "dest.git"))
        home = os.path.join(self.td, "home")
        os.makedirs(home)
        with open(os.path.join(home, ".gitconfig"), "w") as fh:
            fh.write(f'[url "file://{self.td}/up/"]\n\tinsteadOf = https://github.com/\n')
        self.gh = MockGitHub()
        self.gh.src_default_branch["storytold/photocraft"] = "trunk"
        self.gh.add_source_release(1, "v1.0.0", [("a.zip", b"AAAA", True)])
        self.gh.add_source_release(2, "v2.0.0", [("b.zip", b"BBBB", False)], published="2026-02-01T00:00:00Z")
        self.env = dict(os.environ, HOME=home, CRAFT_SUITE_ROOT=self.root, DEST_GIT_URL=self.dest,
                        SRC_API_URL=self.gh.base, DEST_API_URL=self.gh.base, CRAFT_SUITE_DEST_REPO="dst/repo",
                        CRAFT_SUITE_PACE="0", GITHUB_TOKEN="api-token-xyz", GH_SYNC_TOKEN="SUPERSECRET123")

    def tearDown(self):
        self.gh.stop()

    def cli(self, *args, env=None):
        return subprocess.run([sys.executable, os.path.join(self.root, "engine", "craft_sync.py"), *args],
                              env=env or self.env, capture_output=True, text=True)

    def sync(self, slug="photocraft", env=None):
        out = os.path.join(self.td, f"result-{slug}.json")
        p = self.cli("sync-app", "--slug", slug, "--out", out, env=env)
        return p, json.load(open(out, encoding="utf-8"))

    def test_full_cycle_and_idempotent_rerun(self):
        p, r = self.sync()
        self.assertEqual(r["status"], "success", p.stdout + p.stderr)
        self.assertEqual(r["default_branch"], "trunk")
        self.assertIn("refs/heads/photocraft", g(self.dest, "for-each-ref", "--format=%(refname)"))
        self.assertEqual(sorted(g(self.dest, "tag").splitlines()), ["photocraft--v1.0.0", "photocraft--v2.0.0"])
        self.assertEqual(len(self.gh.dest), 2)
        self.assertNotIn("SUPERSECRET123", p.stdout + p.stderr)
        self.assertNotIn("api-token-xyz", p.stdout + p.stderr)
        # finalize -> state/catalog/index
        res = os.path.join(self.td, "res"); os.makedirs(res)
        shutil.copy(os.path.join(self.td, "result-photocraft.json"), os.path.join(res, "result-photocraft.json"))
        fin = self.cli("finalize", "--results", res)
        self.assertEqual(fin.returncode, 0, fin.stdout + fin.stderr)
        cat = json.load(open(os.path.join(self.root, "state", "release-catalog.json")))
        self.assertEqual(cat["apps"]["photocraft"]["latest_stable"]["source_tag"], "v2.0.0")
        self.assertTrue(os.path.exists(os.path.join(self.root, "CRAFT-SUITE-INDEX.md")))
        snap = {f: open(os.path.join(self.root, f), "rb").read() for f in
                ("state/sync-state.json", "state/release-catalog.json", "CRAFT-SUITE-INDEX.md")}
        before_mut = len(self.gh.mutations())
        refs_before = g(self.dest, "for-each-ref", "--format=%(refname) %(objectname)")
        # second run: nothing pushed, no API writes, state files byte-identical
        p2, r2 = self.sync()
        self.assertEqual(r2["status"], "success", p2.stdout + p2.stderr)
        self.assertEqual(len(self.gh.mutations()), before_mut)
        self.assertEqual(refs_before, g(self.dest, "for-each-ref", "--format=%(refname) %(objectname)"))
        shutil.copy(os.path.join(self.td, "result-photocraft.json"), os.path.join(res, "result-photocraft.json"))
        self.cli("finalize", "--results", res)
        for f, data in snap.items():
            self.assertEqual(open(os.path.join(self.root, f), "rb").read(), data, f)

    def test_wrong_destination_repository_is_refused(self):
        env = dict(self.env, GITHUB_REPOSITORY="someone/else")
        env.pop("CRAFT_SUITE_DEST_REPO")
        p, r = self.sync(env=env)
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["errors"][0]["category"], "DEST_MISMATCH")
        self.assertEqual(g(self.dest, "for-each-ref"), "")

    def test_one_app_failing_does_not_block_other_and_aggregate_is_honest(self):
        p, bad = self.sync("vectorcraft")           # upstream repo does not exist locally
        self.assertEqual(bad["status"], "failed")
        self.assertNotEqual(p.returncode, 0)
        ok_p, ok = self.sync("photocraft")
        self.assertEqual(ok["status"], "success")
        res = os.path.join(self.td, "res"); os.makedirs(res)
        for s in ("vectorcraft", "photocraft"):
            shutil.copy(os.path.join(self.td, f"result-{s}.json"), os.path.join(res, f"result-{s}.json"))
        env = dict(self.env, PLANNED_MATRIX=json.dumps([{"slug": "photocraft"}, {"slug": "vectorcraft"}, {"slug": "filmcraft"}]))
        self.assertEqual(self.cli("finalize", "--results", res, env=env).returncode, 0)
        st = json.load(open(os.path.join(self.root, "state", "sync-state.json")))["apps"]
        self.assertEqual((st["photocraft"]["status"], st["vectorcraft"]["status"], st["filmcraft"]["status"]),
                         ("success", "failed", "failed"))
        self.assertEqual(st["filmcraft"]["errors"][0]["category"], "NO_RESULT")

    def test_dry_run_writes_nothing(self):
        env = dict(self.env, DRY_RUN="true")
        p, r = self.sync(env=env)
        self.assertEqual(r["status"], "success", p.stdout + p.stderr)
        self.assertEqual(g(self.dest, "for-each-ref"), "")
        self.assertEqual(self.gh.mutations(), [])


if __name__ == "__main__":
    unittest.main()
