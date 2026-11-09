# Test plan

Legend: AUTO = automated in `tests/` (stdlib unittest, no network); LIVE = must be done in a disposable GitHub repository
before production (cannot be proven offline).

| # | Area | Check | Type |
|---|---|---|---|
| 1 | Manifest | 17 apps, unique slugs/branches, HTTPS URLs match `repository`, bad input rejected | AUTO |
| 2 | Toggles | `enabled` honoured; 17 per-app workflows rendered, manual trigger only, no schedule | AUTO |
| 3 | Default branch | non-main default (`trunk`) detected, symref parsed, API disagreement fails | AUTO (disagreement: code review only) |
| 4 | Branch | only default branch pushed, renamed, other upstream branches ignored | AUTO |
| 5 | Sanitisation | workflows/dependabot archived, original commit is parent, clean trees pushed untouched, deterministic SHA | AUTO |
| 6 | Tags | namespaced; annotated object preserved; moved tag updated; deleted tag only reported; no cross-app collision | AUTO |
| 7 | Independence | two apps -> no shared history | AUTO |
| 8 | Craft overlay | controller workflow installed, upstream workflows archived, idempotent | AUTO |
| 9 | Releases | draft->publish, `make_latest:"false"`, marker, title/tag prefix, notes preserved | AUTO (mock API) |
| 10 | Assets | changed asset same name+size replaced by digest; unchanged not re-uploaded; no-digest identity; mismatch never published | AUTO (mock API) |
| 11 | Failure isolation | missing tag blocks one release; one app fails, others succeed; `NO_RESULT` reported | AUTO |
| 12 | Recovery | upload failure leaves draft, next run completes | AUTO |
| 13 | Idempotency | rerun: zero API writes, zero pushes, byte-identical state/catalog/index | AUTO |
| 14 | Safety | wrong destination refused; dry run writes nothing; token not in output; auth header scoped to destination URL; workflow has only workflow_dispatch trigger | AUTO |
| 15 | Installer | `-Plan`; empty-folder and clean-clone rules; non-empty remote refused; adoption prompt; non-force pushes | LIVE (PowerShell not executed in the build sandbox) |
| 16 | Tag/release do not start workflows | push a sanitised tag and a tag whose commit has `on: push` workflows; create a release; confirm no run appears | LIVE |
| 17 | `GITHUB_TOKEN` release calls | create/publish a release and upload assets with `GITHUB_TOKEN`; confirm asset `digest` field is returned | LIVE |
| 18 | Real Actions run | matrix, artifacts, finalize commit, concurrency, step summary | LIVE |
| 19 | LFS | repo with LFS pointers on the default branch mirrors objects | LIVE (git-lfs not present in sandbox) |
| 20 | Scale | full backfill of the 17 real upstreams: duration, disk, API limits | LIVE |
| 21 | Concurrency | two app workflows started together both commit state without losing either result | LIVE |
| 22 | Licence/brand | decide handling of `docs/brand` in the Craft Anything mirror | MANUAL |

Run offline tests: `python3 -m unittest discover -s tests -v`
