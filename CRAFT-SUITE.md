# Craft Suite (mirror)

This repository mirrors the open-source Craft App Suite. Each application lives on its own
branch; this default branch (`craft`) mirrors Craft Anything.

| Application | Branch | Upstream |
|---|---|---|
| Craft Anything | `craft` | https://github.com/storytold/craft-anything |
| PhotoCraft | `photocraft` | https://github.com/storytold/photocraft |
| VectorCraft | `vectorcraft` | https://github.com/storytold/vectorcraft |
| FilmCraft | `filmcraft` | https://github.com/storytold/filmcraft |
| LightCraft | `lightcraft` | https://github.com/storytold/lightcraft |
| PdfCraft | `pdfcraft` | https://github.com/storytold/pdfcraft |
| EffectCraft | `effectcraft` | https://github.com/storytold/effectcraft |
| DesignCraft | `designcraft` | https://github.com/storytold/designcraft |
| FigCraft | `figcraft` | https://github.com/storytold/figcraft |
| WordCraft | `wordcraft` | https://github.com/storytold/wordcraft |
| SoundCraft | `soundcraft` | https://github.com/storytold/soundcraft |
| DeckCraft | `deckcraft` | https://github.com/storytold/deckcraft |
| CADCraft | `cadcraft` | https://github.com/storytold/cadcraft |
| GridCraft | `gridcraft` | https://github.com/storytold/gridcraft |
| TextureCraft | `texturecraft` | https://github.com/storytold/texturecraft |
| MaterialCraft | `materialcraft` | https://github.com/storytold/materialcraft |
| SolidCraft | `solidcraft` | https://github.com/tangency-labs/solidcraft |

- To sync one application: Actions > "Sync <App>" > Run workflow (manual only; nothing runs automatically).
- Releases and tags are mirrored with the application slug as prefix, e.g. `photocraft--v1.0.0`.
- Per-application latest stable/prerelease: see `CRAFT-SUITE-INDEX.md` on the `craft-suite-control` branch.
- Upstream GitHub Actions workflows are archived under `.upstream-archive/` and are not executed.
- Licenses, NOTICE and attribution files are the upstream originals.

Maintained by Craft Suite Sync (branch `craft-suite-control`).
