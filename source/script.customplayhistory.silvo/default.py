# -*- coding: utf-8 -*-
"""Minimal, defensive entry point for the guarded SiLVO integration."""
from __future__ import absolute_import


def _text(value):
    try:
        return str(value)
    except Exception:
        return 'unavailable'


def _fallback(code, reason):
    """Use only Kodi's most basic dialog API after any launcher failure."""
    try:
        import xbmcgui
        xbmcgui.Dialog().ok(
            'Custom Play History SiLVO Integration',
            'INSTALLATION FAILED',
            'Code: %s' % code,
            'Reason: %s' % _text(reason)[:180])
    except Exception:
        # If xbmcgui itself cannot be created, Kodi is the only remaining UI.
        pass


def _show(installer, result):
    try:
        import xbmcgui
        xbmcgui.Dialog().ok('Custom Play History SiLVO Integration',
                            installer.format_result(result))
    except Exception as exc:
        _fallback('E997', 'Unable to display diagnostic: %s' % _text(exc))


def main():
    # Keep helper import inside the launcher boundary. This catches import-time
    # VFS, Python-package, or platform failures before Kodi emits a generic UI.
    try:
        from resources.lib import installer
    except Exception as exc:
        _fallback('E998', 'Installer could not start: %s' % _text(exc))
        return
    try:
        import xbmcgui
        dialog = xbmcgui.Dialog()
        if dialog.yesno('Custom Play History',
                        'Install or verify the SiLVO 10.0.3 integration?',
                        'Choose No to restore the latest pre-integration backup.'):
            result = installer.install()
        else:
            result = installer.restore()
    except Exception as exc:
        result = installer._result(False, 'E999',
                                   'Unexpected guarded-installer error: %s' % _text(exc))
    _show(installer, result)


main()
