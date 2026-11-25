"""Unpublished decision core for a guarded automatic integration launcher.

This module deliberately does not perform I/O, start services, or modify a skin.
The Kodi lifecycle and UI adapter are not yet implemented or validated.
"""

SUPPORTED_SKIN = 'skin.aeon.nox.silvo'
SUPPORTED_SKIN_VERSION = '10.0.3'


def decide(kodi_major, installed_skin_version, active_skin, skin_state):
    """Return an action without treating a previous success flag as skin evidence."""
    if kodi_major != 21:
        return 'warn_unsupported_kodi'
    if not installed_skin_version:
        return 'warn_skin_missing'
    if installed_skin_version != SUPPORTED_SKIN_VERSION:
        return 'warn_skin_version'
    if active_skin != SUPPORTED_SKIN:
        return 'warn_skin_inactive'
    if skin_state == 'installed':
        return 'already_installed'
    if skin_state == 'absent':
        return 'guarded_install'
    return 'refuse_unknown_state'


def should_reload(result):
    """Reload only after the guarded installer reports a successful mutation."""
    return bool(result.get('ok') and result.get('code') == 'I002' and
                result.get('changed') == 'YES')
