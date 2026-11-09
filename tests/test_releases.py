import os
import sys
import unittest

os.environ["CRAFT_SUITE_PACE"] = "0"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))
sys.path.insert(0, os.path.dirname(__file__))
from mock_github import MockGitHub  # noqa
from craft_suite.releases import ReleaseSync, MARKER_RE  # noqa

APP = {"slug": "photocraft", "name": "PhotoCraft", "repository": "src/app", "sync_prereleases": True}
POLICY = {"include_prereleases": True, "verify_asset_sha256": True, "neutralize_mentions": True}


class FakeGit:
    def __init__(self, tags):
        self.tags = set(tags)
        self.tag_targets = {t: "c" * 40 for t in tags}

    def tag_available(self, dest_tag):
        return dest_tag in {f"photocraft--{t}" for t in self.tags}


class Releases(unittest.TestCase):
    def setUp(self):
        self.gh = MockGitHub()
        self.gh.add_source_release(1, "v1.0.0", [("a.zip", b"AAAA", True), ("b.zip", b"BBBB", True)],
                                   body="Thanks @someone for fixing #12", published="2026-01-01T00:00:00Z")
        self.gh.add_source_release(2, "v2.0.0-rc1", [("c.zip", b"CCCC", False)], prerelease=True,
                                   published="2026-02-01T00:00:00Z")

    def tearDown(self):
        self.gh.stop()

    def sync(self, tags=("v1.0.0", "v2.0.0-rc1"), prev=None, dry=False):
        rs = ReleaseSync(APP, POLICY, self.gh.base, None, self.gh.base, "dst/repo", "tok", prev, FakeGit(tags), dry)
        return rs.run()

    def test_create_as_draft_then_publish_never_latest(self):
        r = self.sync()
        posts = [c for c in self.gh.calls if c[0] == "POST" and c[1] == "/repos/dst/repo/releases"]
        self.assertEqual(len(posts), 2)
        self.assertTrue(all(c[2]["draft"] is True for c in posts))
        patches = [c for c in self.gh.calls if c[0] == "PATCH"]
        self.assertEqual(len(patches), 2)
        self.assertTrue(all(c[2]["draft"] is False and c[2]["make_latest"] == "false" for c in patches))
        for rel in self.gh.dest.values():
            self.assertFalse(rel["draft"])
            self.assertIsNotNone(MARKER_RE.search(rel["body"]))
            self.assertTrue(rel["name"].startswith("[PhotoCraft] "))
            self.assertTrue(rel["tag_name"].startswith("photocraft--"))
        self.assertEqual(r["counts"], {"created": 2})
        self.assertEqual(r["latest_stable"]["destination_tag"], "photocraft--v1.0.0")
        self.assertEqual(r["latest_prerelease"]["source_tag"], "v2.0.0-rc1")

    def test_mentions_neutralised_and_original_notes_kept(self):
        self.sync()
        body = [r for r in self.gh.dest.values() if r["tag_name"] == "photocraft--v1.0.0"][0]["body"]
        self.assertIn("@\u200bsomeone", body)
        self.assertIn("for fixing #12", body)

    def test_rerun_is_idempotent_zero_mutations(self):
        r1 = self.sync()
        n = len(self.gh.mutations())
        prev = {"releases": r1["releases"]}
        r2 = self.sync(prev=prev)
        self.assertEqual(len(self.gh.mutations()), n)
        self.assertEqual(r2["counts"], {"unchanged": 2})

    def test_changed_asset_same_name_same_size_is_replaced_by_digest(self):
        r1 = self.sync()
        # upstream replaces a.zip with different bytes of the SAME size and name
        rel = self.gh.src_releases[0]
        self.gh.files["1-a.zip"] = b"ZZZZ"
        import hashlib
        rel["assets"][0]["digest"] = "sha256:" + hashlib.sha256(b"ZZZZ").hexdigest()
        r2 = self.sync(prev={"releases": r1["releases"]})
        a = [a for a in r2["releases"][0]["assets"] if a["name"] == "a.zip"][0]
        self.assertEqual(a["status"], "transferred")
        dest = [r for r in self.gh.dest.values() if r["tag_name"] == "photocraft--v1.0.0"][0]
        data = {x["name"]: self.gh.asset_data[x["id"]] for x in dest["assets"]}
        self.assertEqual(data["a.zip"], b"ZZZZ")
        self.assertEqual(data["b.zip"], b"BBBB")          # untouched asset not re-uploaded
        self.assertEqual([c[0] for c in self.gh.calls[-8:] if c[0] == "DELETE"], ["DELETE"])

    def test_asset_without_upstream_digest_uses_recorded_identity(self):
        r1 = self.sync()
        n = len(self.gh.mutations())
        self.sync(prev={"releases": r1["releases"]})
        self.assertEqual(len(self.gh.mutations()), n)       # c.zip has no digest but was recorded
        # no recorded state => conservative re-transfer
        self.sync(prev=None)
        self.assertGreater(len(self.gh.mutations()), n)

    def test_missing_tag_blocks_only_that_release(self):
        r = self.sync(tags=("v1.0.0",))
        self.assertEqual(r["counts"].get("created"), 1)
        self.assertEqual(r["counts"].get("failed"), 1)
        self.assertEqual(r["errors"][0]["category"], "RELEASE_TAG_MISSING")

    def test_digest_mismatch_never_publishes(self):
        self.gh.files["1-a.zip"] = b"EVIL"                  # served bytes != declared digest
        r = self.sync()
        stable = [e for e in r["releases"] if e["source_tag"] == "v1.0.0"][0]
        self.assertEqual(stable["action"], "failed")
        dest = [x for x in self.gh.dest.values() if x["tag_name"] == "photocraft--v1.0.0"][0]
        self.assertTrue(dest["draft"])                       # stays invisible draft
        self.assertEqual(r["counts"].get("created"), 1)      # the other release is unaffected
        self.assertIn("ASSET_DIGEST_MISMATCH", [e["category"] for e in r["errors"]])

    def test_foreign_release_is_not_touched(self):
        self.gh.dest[5] = {"id": 5, "tag_name": "photocraft--v1.0.0", "name": "mine", "body": "handmade", "draft": False,
                           "prerelease": False, "assets": [], "html_url": "x"}
        r = self.sync()
        self.assertEqual(self.gh.dest[5]["body"], "handmade")
        self.assertIn("RELEASE_CONFLICT", [e["category"] for e in r["errors"]])

    def test_dry_run_has_no_mutations(self):
        r = self.sync(dry=True)
        self.assertEqual(self.gh.mutations(), [])
        self.assertEqual(r["counts"], {"planned": 2})

    def test_upload_failure_leaves_draft_and_is_recoverable(self):
        self.gh.fail_upload_names.add("b.zip")
        r = self.sync()
        self.assertIn("failed", r["counts"])
        self.gh.fail_upload_names.clear()
        r2 = self.sync(prev={"releases": r["releases"]})
        self.assertNotIn("failed", r2["counts"])
        dest = [x for x in self.gh.dest.values() if x["tag_name"] == "photocraft--v1.0.0"][0]
        self.assertFalse(dest["draft"])
        self.assertEqual(sorted(a["name"] for a in dest["assets"]), ["a.zip", "b.zip"])

    def test_upstream_deleted_release_never_deletes_destination(self):
        self.sync()
        self.gh.src_releases.pop(1)
        self.sync()
        self.assertEqual(len(self.gh.dest), 2)
        self.assertEqual([c for c in self.gh.calls if c[0] == "DELETE" and "releases/" in c[1] and "assets" not in c[1]], [])


if __name__ == "__main__":
    unittest.main()
