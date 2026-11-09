# Operations - Craft Suite Sync

## What ends up in the destination
- Branch per app (`photocraft`, `vectorcraft`, ... and `craft` for Craft Anything): the upstream **default branch only**,
  renamed. Upstream workflows move to `.upstream-archive/workflows/` (and `dependabot.yml` to `.upstream-archive/github/`)
  in ONE child commit on top of the upstream tip; trees without such files are pushed untouched (exact upstream SHA).
- Tags: `<slug>--<tag>`. Annotated tags are pushed as the original tag object (signatures stay valid). A tag whose commit
  contains workflow files points at a sanitised child commit instead (original commit = parent; original tag data stored in
  `.upstream-archive/tag-metadata/`), so pushing a tag can never start an upstream workflow.
- Releases: `[App] <title>`, tag `<slug>--<tag>`, provenance block + upstream notes, all assets. Created as draft, assets
  verified (size + SHA-256), then published. Always `make_latest=false`.
- Per-app latest stable/prerelease: `state/release-catalog.json` and `CRAFT-SUITE-INDEX.md` (control branch).

## Idempotency
A run with no upstream changes performs no push, no release/asset API write and produces no state commit
(timestamps only change when content changes).

## Asset reconciliation
Skipped only when destination asset has the same size AND the same SHA-256 (API digest) or, if a side has no digest,
the identity recorded after the last verified transfer (asset id + updated_at + sha256). Anything else is re-downloaded,
digest-checked against upstream, then replaced (old asset deleted first, then upload; a short gap is possible on
published releases and is repaired by the next run). Zero-byte assets are skipped with a warning.

## Error categories (in job summary, state and result files)
`DEFAULT_BRANCH_UNKNOWN`, `BRANCH_FETCH_FAILED`, `WORKFLOW_SANITIZATION_FAILED`, `BRANCH_PUSH_FAILED`, `TAG_PUSH_FAILED`,
`TAG_MAPPING_CONFLICT`, `RELEASE_LIST_FAILED`, `RELEASE_TAG_MISSING`, `RELEASE_CONFLICT` (a destination release for the tag exists
but is not ours - never touched), `ASSET_DOWNLOAD_FAILED`, `ASSET_DIGEST_MISMATCH`, `ASSET_UPLOAD_FAILED`, `LFS_UNAVAILABLE`,
`LFS_TRANSFER_FAILED`, `AUTH_MISSING`, `DEST_MISMATCH`, `DEST_UNAVAILABLE`, `NO_RESULT`, `INTERNAL`, `MANIFEST`.
App status: `success`, `partial` (something failed but others worked), `failed`, `disabled`. A red leg = real problem; the run
is only green when every planned app succeeded.

## Recovery
- Failed app: fix the cause, run the workflow with `app=<slug>`. State is preserved; completed work is not repeated.
- Draft release left behind (upload failed): the next run finds it by its hidden marker, finishes the assets, publishes.
- Wrong/foreign release (`RELEASE_CONFLICT`): rename/delete it yourself; the sync will not.
- Stale items (deleted upstream): reported (`stale`) only. Pruning is **not implemented** by design in v1.

## Decisions taken where the spec was ambiguous
1. One workflow file per app (`.github/workflows/sync-<slug>.yml` on `craft`), `workflow_dispatch` only, rendered from the single
   template `workflow/app-sync.yml`. This was requested explicitly; it deviates from the original handoff ("no workflow per app").
   Runs of different apps may overlap; state commits retry and re-merge on conflict.
2. Controller lives on a separate branch (`craft-suite-control`), because `craft` is force-reset to follow upstream each run.
3. Release API calls use the workflow's `GITHUB_TOKEN` (events from it do not start workflows); the PAT is used for `git push` only.
4. Upstream @mentions in release notes are neutralised with a zero-width space (`neutralize_mentions`).

## Known limits / not covered
- Git LFS: branch tip objects are mirrored when git-lfs is available; LFS objects reachable only from tags are not mirrored.
- No pruning of tags, releases or assets. No release for upstream drafts.
- Destination "immutable releases" setting, if enabled, will conflict with asset replacement - keep it off for this repository.
- The 17 workflow files live on `craft`; they are refreshed only when **Sync Craft Anything** runs (or on re-install).
