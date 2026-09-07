# -*- coding: utf-8 -*-
"""Manual entry point for the guarded SiLVO integration."""
from __future__ import absolute_import

import xbmcgui
from resources.lib import installer

dialog = xbmcgui.Dialog()
if dialog.yesno('Custom Play History', 'Install or verify the SiLVO 10.0.3 integration?',
                'Choose No to restore the latest pre-integration backup.'):
    result = installer.install()
else:
    result = installer.restore()
dialog.ok('Custom Play History', result)
