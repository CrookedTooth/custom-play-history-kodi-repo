"""Kodi service entry point for the unpublished automatic setup candidate."""
import importlib.util
import os
import re
from xml.etree import ElementTree

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

import adapter


class KodiIO:
    def __init__(self):
        self.addon = xbmcaddon.Addon()
        self._installer = None

    def log(self, message):
        xbmc.log('[customplayhistory.setup] ' + message, xbmc.LOGINFO)

    def install_token(self):
        try:
            path = xbmcvfs.translatePath(self.addon.getAddonInfo('path'))
            stat = os.stat(os.path.join(path, 'addon.xml'))
            return self.addon.getAddonInfo('version') + ':' + str(stat.st_mtime_ns)
        except Exception as exc:
            self.log('Cannot read package identity: ' + str(exc))
            return ''

    def previous_token(self):
        return self.addon.getSetting('last_attempt')

    def record_token(self, token):
        try:
            self.addon.setSetting('last_attempt', token)
            return self.addon.getSetting('last_attempt') == token
        except Exception as exc:
            self.log('Cannot save attempt state: ' + str(exc))
            return False

    def kodi_major(self):
        value = xbmc.getInfoLabel('System.BuildVersion')
        match = re.match(r'\s*(\d+)', value or '')
        return int(match.group(1)) if match else None

    def skin_version(self):
        path = xbmcvfs.translatePath('special://home/addons/skin.aeon.nox.silvo/addon.xml')
        try:
            root = ElementTree.parse(path).getroot()
            return root.get('version') if root.get('id') == 'skin.aeon.nox.silvo' else None
        except (OSError, ElementTree.ParseError):
            return None

    def active_skin(self):
        return xbmc.getSkinDir()

    def guarded_install(self):
        root = xbmcvfs.translatePath('special://home/addons/script.customplayhistory.silvo')
        file_path = os.path.join(root, 'resources', 'lib', 'installer.py')
        spec = importlib.util.spec_from_file_location('cph_guarded_installer', file_path)
        if spec is None or spec.loader is None:
            raise RuntimeError('Guarded installer could not be loaded')
        self._installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self._installer)
        return self._installer.install()

    def format_result(self, result):
        return self._installer.format_result(result)

    def reload_skin(self):
        xbmc.executebuiltin('ReloadSkin()', True)

    def dialog(self, title, message):
        try:
            xbmcgui.Dialog().ok(title, message)
        except Exception as exc:
            self.log('E997: unable to show setup dialog: ' + str(exc))


if __name__ == '__main__':
    try:
        adapter.initialize(KodiIO())
    except Exception as exc:
        xbmc.log('[customplayhistory.setup] E999: setup failed: ' + str(exc), xbmc.LOGERROR)
