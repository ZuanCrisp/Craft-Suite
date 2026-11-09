# Craft Suite Sync

Mirrors the 17 Craft App Suite repositories into `ZuanCrisp/Craft-Suite`: one branch per app (upstream default branch,
renamed), namespaced tags, releases with assets, per-app latest catalog. No builds, no upstream code or workflow is executed.

```
Install-CraftSuite.ps1   one-time installer (Windows)
config/apps.json         manifest: 17 apps + toggles + policies
engine/                  Python (stdlib only): git engine, release engine, catalog, CLI
workflow/                app-sync.yml: template rendered into one manual workflow per app
templates/               pointer README for the default branch
docs/                    SETUP.md, OPERATIONS.md, TEST-PLAN.md
tests/                   offline unit/e2e tests + mock GitHub API
VALIDATION-RESULTS.md    what was actually run, and what was not
```
Start with `docs/SETUP.md`.
