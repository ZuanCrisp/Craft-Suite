# Craft Suite (mirror)

This repository mirrors the open-source Craft App Suite. Each application lives on its own
branch; this default branch (`craft`) mirrors Craft Anything.

| Application | Branch | Upstream |
|---|---|---|
__APP_TABLE__

- To sync one application: Actions > "Sync <App>" > Run workflow (manual only; nothing runs automatically).
- Releases and tags are mirrored with the application slug as prefix, e.g. `photocraft--v1.0.0`.
- Per-application latest stable/prerelease: see `CRAFT-SUITE-INDEX.md` on the `__CONTROL_BRANCH__` branch.
- Upstream GitHub Actions workflows are archived under `.upstream-archive/` and are not executed.
- Licenses, NOTICE and attribution files are the upstream originals.

Maintained by Craft Suite Sync (branch `__CONTROL_BRANCH__`).
