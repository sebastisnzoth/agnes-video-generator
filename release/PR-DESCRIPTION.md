# chore(release): prepare v7.2.0 — music-video release notes, APP_VERSION bump, feature docs

## Summary

Prepares the **v7.2.0** release. The v7.1 (`song → music video`) and v7.2 (`singer/performer reference image + storyboard`) increments were merged into `master` (PR #2, PR #3) without a version bump or release notes, so `APP_VERSION` still reported `7.0.5` and `docs/public/release-notes/` stopped at v7.0.5. This PR closes that gap.

- **`core/config.py`** — `APP_VERSION` `7.0.5` → `7.2.0`. The constant is exposed through `GET /api/config` and shown in the failure-diagnostics panel, so it must match the released version (`docs/dev/release_process.md`).
- **New `docs/public/release-notes/release_notes_v7.2.0.md`** — written to the `docs/dev/release_process.md` §5 template, fully English, with the required `## Overview` / `## Usage` / `## What's New` structure. It consolidates v7.1 + v7.2 into a single minor release and says so explicitly, since no release was cut between them. The `## What's New` anchor is what `release.yml` and `.github/scripts/update_dockerhub_overview.py` extract for the GitHub Release body, the Docker Hub overview and the npm README.
- **Docs gap in `docs/public/features.md`** — the Music Video section covered formats, 10-second segmentation, the original-song soundtrack and automatic lyrics, but not the singer/performer modes nor the `storyboard.json` artifact. Both are now documented, and the mode table row mentions the consistent performer.
- **Docs gap in `docs/public/features.zh.md`** — Music Video was missing entirely (no mode-table row, no section). Added, mirroring the English page including the singer modes and storyboard.
- **`docs/public/api.md`** — `POST /api/tasks/music-video` was absent from the task-creation endpoint table. Added, with its `singer_mode` / `singer_prompt` / `singer_photo` form fields.

## Not included

- **No `v7.2.0` tag is pushed here.** Per `docs/dev/release_process.md` step 5, the release notes need your explicit approval first; the tag (which is what triggers the CI release: images, npm, GitHub Release) is cut only after that.

## Verification

- `python3 -m py_compile core/config.py` — passes.
- The only runtime change is the version constant; no code path, endpoint or frontend bundle is touched (`static/` is unchanged, so no `npm run build` needed).
- `awk '/^## What.s New/{f=1} f' docs/public/release-notes/release_notes_v7.2.0.md` — resolves the section, i.e. the same anchor CI uses for the GitHub Release body / Docker Hub / npm prepend.
- `grep -rn "7\.0\.5"` across the repo — only the historical `release_notes_v7.0.5.md` and the new note's "From v7.0.5" upgrade line, as expected.
- `ruff`, `pytest` and `npm run build` were **not** run in this sandbox (the environment is PEP-668 managed and the project's test/build dependencies are not installed); CI covers them. The single Python change is a string constant, so lint is unaffected.

## After merging

1. Review the release notes wording (this is the approval gate).
2. `git tag v7.2.0 && git push origin v7.2.0` → CI builds and publishes the images, the npm package and the GitHub Release; CI also bumps the image tag in `docker-compose.yml`.
