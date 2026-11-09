# Setup - Craft Suite Sync (manual, one workflow per app)

Destination: `ZuanCrisp/Craft-Suite` (change in `config/apps.json` -> `destination.repository`).
Nothing runs automatically: every app has its own workflow that you start by hand.

## 1. Prerequisites
- Git on Windows. Optional: GitHub CLI `gh` (`gh auth login`).
- The destination repository created **empty** (no README, LICENSE, .gitignore).
- A fine-grained PAT limited to the destination repository: Contents: Read and write, Workflows: Read and write.

## 2. Install
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
cd E:\Vibe\Craft-Suite                      # empty folder (or clean clone)
& "E:\Vibe\craft-suite-sync\Install-CraftSuite.ps1" -Plan      # read-only rehearsal
& "E:\Vibe\craft-suite-sync\Install-CraftSuite.ps1" -WorkDir "E:\Vibe\Craft-Suite" -GitUserName "ZuanCrisp" -GitUserEmail "<verified email>" -SetSecret -FirstRun none
```
It pushes two branches without force: `craft` (seed with the 17 per-app workflows; pushed first so it is the default)
and `craft-suite-control` (manifest, engine, docs, tests, state).
A non-empty destination is refused unless `-AdoptExisting` is given.

## 3. Secrets
| Name | Required | Purpose |
|---|---|---|
| `GH_SYNC_TOKEN` | yes (real sync) | PAT used only for `git push` to this repository; never sent to upstream hosts |
| `UPSTREAM_API_TOKEN` | no | optional read token for upstream API calls |

Settings > Actions > General > Workflow permissions: allow the workflow's own `permissions: contents: write`.

## 4. Sync an app (repeat per app, in any order)
Actions > **Sync PhotoCraft** (or any other) > Run workflow. Tick `dry_run` for a plan-only run first.
Optional: **Sync ALL apps (manual)** runs every app with `enabled` and `auto_sync` = true, two at a time, from one click
(manual only, never scheduled). For the first backfill the per-app workflows are easier to follow and re-run.
Start with **Sync Craft Anything**: it turns `craft` into the real Craft Anything mirror and re-installs all 17
workflow files on it. Results appear on the control branch: `CRAFT-SUITE-INDEX.md`, `state/`.

## 5. Configuration (on branch `craft-suite-control`, file `config/apps.json`)
- `enabled` false = the app has no workflow file and is never synced.
- `sync_source_default_branch`, `sync_tags`, `sync_releases`, `sync_prereleases` = per-app scopes.
- `auto_sync` = false excludes the app from **Sync ALL** only; its own per-app workflow always works.
Adding/removing an app or changing the workflow template: update the control branch
(`Install-CraftSuite.ps1 -UpdateControl`), then run **Sync Craft Anything** once so the files on `craft` are re-rendered.

## 6. Pause / stop
Nothing is scheduled, so simply do not run anything. Disable a workflow in the Actions tab if you want to be sure.
