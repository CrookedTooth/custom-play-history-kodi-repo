"""Guarded one-shot setup orchestration. I/O is supplied by the Kodi adapter."""
from decision import decide, should_reload

PREREQUISITE_WARNING = ('Custom Play History requires Aeon Nox: SiLVO 10.0.3.\n\n'
                        'Please install and activate the required skin, then reinstall Custom Play History.\n\n'
                        'No skin changes have been made.')
KODI_WARNING = ('Custom Play History requires Kodi 21 Omega.\n\n'
                'No skin changes have been made.')
SUCCESS = ('Custom Play History installed successfully.\n\n'
           'Aeon Nox: SiLVO 10.0.3 must use CENTER main menu layout.\n\n'
           'Under Music main menu settings:\nWidget 1: Custom Play History\n'
           'Widget Style: List Extended\n\nLOW layout and Widget 2 are unsupported.')


def initialize(io):
    """Run at most once per observed package installation; never poll or change settings."""
    token = io.install_token()
    if not token:
        io.log('E992: unable to identify installation; setup refused')
        return 'token_error'
    if io.previous_token() == token:
        io.log('Setup already attempted for this installation; no action taken')
        return 'already_attempted'
    # Persist before any mutation; crash recovery is explicit reinstall, never a loop.
    if not io.record_token(token):
        io.log('E993: unable to save attempt state; setup refused')
        return 'state_error'
    action = decide(io.kodi_major(), io.skin_version(), io.active_skin(), 'absent')
    if action != 'guarded_install':
        io.dialog('Custom Play History', KODI_WARNING if action == 'warn_unsupported_kodi' else PREREQUISITE_WARNING)
        return action
    try:
        result = io.guarded_install()
    except Exception as exc:
        io.log('E998: guarded installer could not start: ' + str(exc))
        io.dialog('Custom Play History', 'Setup failed (E998). No success has been reported. See Kodi log.')
        return 'installer_error'
    if not result.get('ok'):
        io.dialog('Custom Play History', io.format_result(result))
        return 'guarded_failure'
    if should_reload(result):
        io.reload_skin()
        io.dialog('Custom Play History', SUCCESS)
        return 'installed'
    if result.get('code') == 'I001' and result.get('state') == 'installed':
        io.log('Existing integration verified; no changes or reload')
        return 'already_installed'
    io.log('E994: unexpected guarded installer result; setup refused')
    io.dialog('Custom Play History', 'Setup failed (E994). No success has been reported. See Kodi log.')
    return 'unexpected_result'
