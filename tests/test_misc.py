import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))
from craft_suite.manifest import load_manifest, validate_manifest, plan_apps  # noqa
from craft_suite import catalog  # noqa
from craft_suite.cli import render_workflow  # noqa

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
M = load_manifest(os.path.join(ROOT, "config", "apps.json"))


class Manifest(unittest.TestCase):
    def test_shipped_manifest_valid_with_17_apps(self):
        self.assertEqual(validate_manifest(M, strict_count=True), [])
        self.assertEqual(len({a["slug"] for a in M["apps"]}), 17)
        self.assertEqual(M["destination"]["repository"], "ZuanCrisp/Craft-Suite")

    def test_rejects_bad_input(self):
        for mut, needle in [
            (lambda m: m["apps"][1].update(slug="photo--craft"), "invalid slug"),
            (lambda m: m["apps"][1].update(destination_branch="craft"), "duplicate destination_branch"),
            (lambda m: m["apps"][1].update(upstream_url="http://github.com/storytold/photocraft.git"), "HTTPS"),
            (lambda m: m["apps"][1].update(upstream_url="https://github.com/evil/other.git"), "does not match"),
            (lambda m: m["apps"][1].update(destination_branch="craft-suite-control"), "control branch"),
            (lambda m: m["release_policy"].update(make_latest=False), "make_latest"),
            (lambda m: m["automation"].update(mode="scheduled"), "manual-per-app"),
        ]:
            m = copy.deepcopy(M)
            mut(m)
            self.assertTrue(any(needle in p for p in validate_manifest(m)), needle)

    def test_plan_schedule_and_manual(self):
        m = copy.deepcopy(M)
        m["apps"][2]["auto_sync"] = False
        m["apps"][3]["enabled"] = False
        sched = plan_apps(m, "schedule", "", "")
        self.assertEqual(len(sched["apps"]), 15)
        self.assertEqual(plan_apps(m, "schedule", "", "false")["apps"], [])          # global switch off
        self.assertEqual(plan_apps(m, "schedule", "", "FALSE")["apps"], [])
        self.assertEqual(len(plan_apps(m, "workflow_dispatch", "all", "false")["apps"]), 15)  # manual works when schedule off
        one = plan_apps(m, "workflow_dispatch", m["apps"][2]["slug"], "false")           # explicit slug ignores auto_sync
        self.assertEqual([a["slug"] for a in one["apps"]], [m["apps"][2]["slug"]])
        self.assertEqual(plan_apps(m, "workflow_dispatch", m["apps"][3]["slug"], "")["apps"], [])  # disabled stays disabled


class Catalog(unittest.TestCase):
    def result(self, status="success", tag="v1"):
        rel = {"releases": [{"upstream_release_id": 1, "source_tag": tag, "dest_tag": "photocraft--" + tag, "action": "created",
                             "published_at": "2026-01-01T00:00:00Z", "assets": [], "dest_release_url": "u"}],
               "source_release_ids": [1], "counts": {"created": 1},
               "latest_stable": {"source_tag": tag, "destination_release_url": "u"}, "latest_prerelease": None}
        return {"slug": "photocraft", "status": status, "default_branch": "main", "src_head": "a" * 40,
                "branch": {"destination_head_sha": "b" * 40}, "tags": {"created": 1}, "releases": rel, "errors": []}

    def test_noop_run_changes_nothing_including_timestamps(self):
        s1 = catalog.merge_state({}, [self.result()], M)
        s2 = catalog.merge_state(json.loads(json.dumps(s1)), [self.result()], M)
        # second run reports 'unchanged' actions in reality; content-equal otherwise
        self.assertEqual(json.dumps(s1, sort_keys=True), json.dumps(s2, sort_keys=True))

    def test_failed_phase_keeps_last_known_catalog(self):
        s1 = catalog.merge_state({}, [self.result()], M)
        bad = {"slug": "photocraft", "status": "failed", "errors": [{"category": "RELEASE_LIST_FAILED", "message": "x"}],
               "releases": None}
        s2 = catalog.merge_state(s1, [bad], M)
        self.assertEqual(s2["apps"]["photocraft"]["latest_stable"]["source_tag"], "v1")
        self.assertEqual(s2["apps"]["photocraft"]["status"], "failed")

    def test_release_deleted_upstream_is_flagged_not_removed(self):
        s1 = catalog.merge_state({}, [self.result()], M)
        r = self.result()
        r["releases"] = {"releases": [], "source_release_ids": [], "counts": {}, "latest_stable": None, "latest_prerelease": None}
        s2 = catalog.merge_state(s1, [r], M)
        self.assertTrue(s2["apps"]["photocraft"]["releases"][0]["stale"])

    def test_catalog_and_index_render(self):
        s = catalog.merge_state({}, [self.result()], M)
        c = catalog.build_catalog(s)
        self.assertEqual(c["apps"]["photocraft"]["latest_stable"]["source_tag"], "v1")
        self.assertIn("PhotoCraft", catalog.render_index(s, "ZuanCrisp/Craft-Suite"))


class WorkflowSafety(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "workflow", "app-sync.yml"), encoding="utf-8") as fh:
            self.tpl = fh.read()
        self.rendered = {a["slug"]: render_workflow(self.tpl, M, a) for a in M["apps"]}

    def test_one_workflow_per_app_and_no_unreplaced_tokens(self):
        self.assertEqual(len(self.rendered), 17)
        for slug, text in self.rendered.items():
            for token in ("__SLUG__", "__APP_NAME__", "__CONTROL_BRANCH__"):
                self.assertNotIn(token, text, slug)
            self.assertIn(f"name: Sync {[a['name'] for a in M['apps'] if a['slug'] == slug][0]}", text)
            self.assertIn(f"--slug \"$SLUG\"", text)
            self.assertIn(f"SLUG: {slug}", text)

    def test_overlay_for_craft_contains_all_17_workflows(self):
        os.environ["CRAFT_SUITE_ROOT"] = ROOT
        from craft_suite import cli
        files = cli.overlay_files(M)
        wf = sorted(k for k in files if k.startswith(".github/workflows/"))
        self.assertEqual(len(wf), 17)
        self.assertIn(".github/workflows/sync-photocraft.yml", wf)
        self.assertIn("CRAFT-SUITE.md", files)

    def test_triggers_are_manual_only(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        for slug, text in self.rendered.items():
            doc = yaml.safe_load(text)
            triggers = doc.get(True) or doc.get("on")           # YAML 1.1 parses `on` as True
            self.assertEqual(sorted(triggers), ["workflow_dispatch"], slug)
            self.assertEqual(doc["permissions"], {"contents": "write"})
            self.assertEqual(doc["concurrency"]["group"], f"craft-suite-sync-{slug}")
            self.assertFalse(doc["concurrency"]["cancel-in-progress"])

    def test_no_upstream_execution_or_secret_echo(self):
        for slug, text in self.rendered.items():
            low = text.lower()
            for bad in ("schedule:", "pull_request_target", "cargo ", "npm ", "echo ${{ secrets", "persist-credentials: true"):
                self.assertNotIn(bad, low, slug)
            self.assertEqual(text.count("persist-credentials: false"), 1)


if __name__ == "__main__":
    unittest.main()
