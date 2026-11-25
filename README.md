# Custom Play History for Kodi

## Requirements

- Kodi 21 Omega.
- Aeon Nox: SiLVO 10.0.3 installed and active.
- CENTER main menu layout, Music Widget 1, and Widget Style: List Extended.

LOW layout and Widget 2 are unsupported. The simplified installation was verified with Kodi 21.3 on Windows. Android and Fire OS installation has not yet been validated.

## Installation

1. Add `https://crookedtooth.github.io/custom-play-history-kodi-repo/` as a Kodi file source.
2. Choose **Install from zip file** and install the latest `repository.customplayhistory` ZIP.
3. Open **Install from repository → Kunsemiller Repository**.
4. Under **Services**, install **Custom Play History** only.
5. Select **Custom Play History** as Music Widget 1.
6. Set Widget Style to **List Extended**.

Music Play History and SiLVO Integration are internal dependencies, installed automatically. Do not install them separately. Normal installation requires no manual Run operation, skin reload, or Kodi restart. The installer does not install or activate SiLVO or change your layout or widget choices.

The SiLVO integration is guarded, backs up the modified skin files and retains its restore function. An existing user's tracking service and listening history remain owned by the original service add-on. Repository-based upgrade and same-version prerequisite-recovery testing remain outstanding; do not assume those cases are verified.
