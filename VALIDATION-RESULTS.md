# Validation results

Environment: Linux sandbox, Python 3.12.3, git 2.43.0, no network, no PowerShell, no git-lfs.
Command: `python3 -m unittest discover -s tests -v` -> **Ran 38 tests, OK** (about 11 s).

## Passed (actually executed)
- Git engine, 10 tests: default branch `trunk` detected (not assumed); only the default branch is pushed and renamed;
  workflow/dependabot files archived with the original commit as parent; annotated tag object preserved byte-for-byte;
  second run pushes nothing; moved tag updated, deleted tag only reported (never pruned); two apps with identical tag names
  do not collide and have independent histories; Craft overlay installs the controller workflow and is idempotent;
  dry run changes nothing; auth header is scoped to the destination URL only.
- Release engine against a mock GitHub API, 12 tests (incl. HTTP 504 on the download endpoint -> fallback to the API asset endpoint): draft -> publish with `make_latest:"false"`; hidden identity marker;
  title/tag prefixes; @mentions neutralised; rerun = zero API writes; same-name same-size changed asset replaced by digest;
  asset without upstream digest handled via recorded identity (and conservatively re-transferred without state);
  missing tag blocks one release only; digest mismatch never publishes; foreign release untouched; dry run = zero writes;
  upload failure leaves a draft and the next run completes it; upstream deletion never deletes destination.
- Manifest/plan/catalog/workflow, 12 tests: 17 valid apps; invalid input rejected; schedule/global switch/manual dispatch/
  explicit slug logic; no-op run yields byte-identical state; failed phase keeps last known catalog; stale flagged not removed;
  each of the 17 rendered per-app workflows has only the `workflow_dispatch` trigger, its own concurrency group, no schedule, no persisted credentials, no build commands; the Craft overlay carries the 17 per-app workflows plus `sync-all.yml` (manual only, max 2 parallel, per-app concurrency groups).
- End-to-end through the real CLI, 4 tests: full cycle + idempotent rerun with byte-identical state/catalog/index; wrong
  destination refused; failing app does not block another and aggregate status is honest (`NO_RESULT` reported); dry run writes
  nothing; neither token appears in output.
- Installer git choreography (orphan branches, non-force pushes, default branch first) rehearsed with plain git against a local
  bare remote. Parentheses/braces/brackets of `Install-CraftSuite.ps1` balance.

## Bugs found and fixed
Found by the user's real Actions run: upstream asset downloads returned HTTP 504 and the retry window was only ~7 s. Now 5 attempts with 5/20/60/120 s backoff, then a fallback to the API asset endpoint (token only to the API host, dropped on the CDN redirect). The fallback is tested against a mock only; whether it cures the real 504 is unconfirmed.

Found by the user's real run: `$refs = Get-RemoteRefs` returned `$null` on an empty remote and PowerShell StrictMode failed on `.Count`; fixed with `@(...)` (user's edit, kept).

## Bug found and fixed while testing
Run-specific verbs (`created`/`unchanged`, tag counters) were persisted in state, so every rerun would have produced a commit.
Only durable fields are persisted now (covered by the byte-identical rerun test).

## NOT verified - do not treat as tested
- `Install-CraftSuite.ps1` has never been executed (no PowerShell here). Run `-Plan` first on your machine and report any error.
- Everything that needs real GitHub: Actions runtime, matrix/artifacts, `GITHUB_TOKEN` release/upload permissions, the `digest`
  field of assets, that sanitised tags and releases do not trigger upstream workflows, repo-level settings, scheduling.
- Git LFS (git-lfs not installed in the sandbox), very large repositories, real backfill duration and rate limits.
- Default-branch API cross-check disagreement path (reviewed, not exercised).
See `docs/TEST-PLAN.md` rows marked LIVE.
