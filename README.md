# Custom Play History for Kodi

By **Jeff Kunsemiller** · Available through **Kunsemiller Repository**

<img src="https://crookedtooth.github.io/custom-play-history-kodi-repo/service.customplayhistory.setup/icon.png" width="128" alt="Custom Play History"> <img src="https://crookedtooth.github.io/custom-play-history-kodi-repo/repository.customplayhistory/icon.png" width="128" alt="Kunsemiller Repository">

See your **Most Played Albums, Most Played Artists and Most Played Tracks** together on the Music home screen.

## Before you install

- Kodi 21 Omega.
- Aeon Nox: SiLVO **10.0.3**, already installed and active.
- SiLVO main-menu layout set to **CENTER**.

## Install

1. Open **Settings → File manager → Add source**. Enter `https://crookedtooth.github.io/custom-play-history-kodi-repo/` and name it **Kunsemiller Repository**.
2. Open **Settings → Add-ons → Install from zip file**. Enable Unknown sources if Kodi requires it. Open Kunsemiller Repository and install **repository.customplayhistory-0.1.4.zip**.
3. Open **Install from repository → Kunsemiller Repository → Services**. Install **Custom Play History**—the **only** add-on you need to choose. Music Play History and SiLVO Integration install automatically as dependencies.
4. Allow automatic setup and the skin refresh to complete. No manual Run operation, manual skin reload, separate dependency installation, or Kodi restart is required during normal installation.
5. Open **Skin settings → Setup the Aeon Nox main menu → Music**. Set **Widget 1** to **Custom Play History** and **Widget Style** to **List Extended**. Return to the Music home screen.

**Important:** CENTER layout, Widget 1 and List Extended are required. LOW layout and Widget 2 are unsupported. SiLVO is not automatically installed or activated. Rankings can initially be empty until qualifying listening history accumulates.

## Existing users and validation

Existing Music Play History users do not need to reinstall a working integration just to obtain the simplified installer. The service ID and ownership of the listening-history database are unchanged.

The simplified installation was demonstrated on Windows Kodi 21.3. Android and Fire OS installation of the simplified installer has not been independently validated; the previous individual components have operated on the project owner's Shield and a friend's Fire Stick. A complete repository-based upgrade test and recovery after installing the setup add-on without the SiLVO prerequisites remain unverified. Install only after satisfying the prerequisites above.

## Advanced: integration safety

The SiLVO integration checks the supported version and XML anchors, backs up the modified files and retains a restore function. The individual components remain available for existing users; the normal installation above does not require running them manually.
