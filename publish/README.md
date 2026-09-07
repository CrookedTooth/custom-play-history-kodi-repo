# Custom Play History for Kodi

This repository contains two Kodi 21 Omega add-ons:

- `service.music.playhistory` — records qualifying music plays locally and exposes Most Played Tracks, Albums, and Artists.
- `script.customplayhistory.silvo` — an optional, guarded companion for Aeon Nox: SiLVO **10.0.3**. It adds the `Custom Play History` Music Widget 1 entry and the three-panel renderer.

The service is portable: a new installation starts with empty local history and neutral source-root settings. It does not package a database, listening history, library data, artwork cache, or machine-specific paths.

## SiLVO companion safety

The companion supports only `skin.aeon.nox.silvo` 10.0.3. It verifies the installed skin version and exact XML anchors before editing two source files:

- `16x9/Includes_Widgets.xml`
- `shortcuts/overrides.xml`

It backs up the exact originals under its own Kodi addon-data directory, is idempotent, and can restore the latest backup. It never edits generated Skin Shortcuts files. On an unsupported version, a partial installation, a missing anchor, or an unwritable skin directory, it stops without modifying the skin.

After installing the companion, run it from Programs. Then reload the skin or restart Kodi. Select `Custom Play History` for Music Widget 1 in SiLVO’s Main Menu Customizer.

## Install from this repository

Install [repository.customplayhistory-0.1.0.zip](https://crookedtooth.github.io/custom-play-history-kodi-repo/repository.customplayhistory-0.1.0.zip) using Kodi’s **Install from zip file** command. Then choose **Install from repository → Custom Play History Repository** and install the service. The optional SiLVO integration is available from the same repository.

A new service installation begins with empty history. A track qualifies after 80% of its duration, capped at four minutes. An album qualifies after at least three distinct tracks and 40% of its tracks.

## Publishing later

The optional SiLVO integration supports exactly SiLVO 10.0.3. Re-run its guarded installer after a skin update. Its Restore option returns the two modified skin files to the exact backed-up versions.
