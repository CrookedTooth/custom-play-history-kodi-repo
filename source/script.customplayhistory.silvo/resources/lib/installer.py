# -*- coding: utf-8 -*-
"""Guarded, reversible Aeon Nox: SiLVO 10.0.3 integration installer.

This module edits only verified, single-occurrence XML anchors. It never edits
Skin Shortcuts generated output.
"""
from __future__ import absolute_import

import hashlib
import json
import os
import time
from xml.etree import ElementTree

import xbmcvfs

SKIN_ID = 'skin.aeon.nox.silvo'
SKIN_VERSION = '10.0.3'
WIDGET_IDENTITY = 'CustomPlayHistory'
MARKER = 'MusicPlayHistoryWidget1'
_RENDERER_MARKER = b'<include name="MusicPlayHistoryWidget1">'

_WIDGET = (b'\t<!-- Custom Play History widget registration. -->\n'
           b'\t<widget label="Custom Play History" type="songs" target="music" '
           b'path="plugin://service.music.playhistory/?view=tracks&amp;limit=50">CustomPlayHistory</widget>\n')
_WIDGET_GROUPINGS = b'\t\t<content>widgets</content>\n'
_CUSTOM_VISIBLE = (b'String.IsEqual(Container(9000).ListItem.Property(submenuVisibility),music) + '
                   b'String.IsEqual(Container(9000).ListItem.Property(widgetName),Custom Play History)')
_GENERIC_VISIBLE = b'![' + _CUSTOM_VISIBLE + b']'
_ANIMATION_CONDITIONS = (
    b'String.IsEqual(Skin.String(MainMenu.Layout),vertical) + !String.Contains(Container(9000).ListItem.Property(widgetStyle),Extended)',
    b'String.IsEqual(Skin.String(MainMenu.Layout),vertical) + String.IsEqual(Container(9000).ListItem.Property(widgetStyle),Extended Compact Panel)',
    b'String.IsEqual(Skin.String(MainMenu.Layout),vertical) + String.IsEqual(Container(9000).ListItem.Property(widgetStyle),Extended Panel)',
    b'String.IsEqual(Skin.String(MainMenu.Layout),vertical) + String.Contains(Container(9000).ListItem.Property(widgetStyle),Extended) + !String.Contains(Container(9000).ListItem.Property(widgetStyle),List) + ControlGroup(90010).HasFocus',
    b'String.IsEqual(Skin.String(MainMenu.Layout),vertical) + String.Contains(Container(9000).ListItem.Property(widgetStyle),Extended) + !String.Contains(Container(9000).ListItem.Property(widgetStyle),List) + String.Contains(Container(9000).ListItem.Property(widgetStyle.2),Extended) + ControlGroup(90020).HasFocus',
)
_EXCLUSION = b' + !String.IsEqual(Container(9000).ListItem.Property(widgetName),Custom Play History)'


def _translated(path):
    return xbmcvfs.translatePath(path)


def _read(path):
    with open(path, 'rb') as handle:
        return handle.read()


def _write(path, data):
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    temporary = path + '.customplayhistory.tmp'
    with open(temporary, 'wb') as handle:
        handle.write(data)
        handle.flush()
    os.replace(temporary, path)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _parse(data, name):
    try:
        return ElementTree.fromstring(data)
    except Exception as exc:
        raise ValueError('%s is not valid XML: %s' % (name, exc))


def _one(data, anchor, label):
    count = data.count(anchor)
    if count != 1:
        raise ValueError('%s anchor count is %d; expected exactly one' % (label, count))


def _skin_paths():
    skin = _translated('special://home/addons/%s' % SKIN_ID)
    return {'addon': os.path.join(skin, 'addon.xml'),
            'widgets': os.path.join(skin, '16x9', 'Includes_Widgets.xml'),
            'overrides': os.path.join(skin, 'shortcuts', 'overrides.xml')}


def _backup_root():
    return _translated('special://profile/addon_data/script.customplayhistory.silvo/backups')


def _renderer_path():
    addon = _translated('special://home/addons/script.customplayhistory.silvo')
    return os.path.join(addon, 'resources', 'renderer.xml')


def _version(addon_data):
    root = _parse(addon_data, 'skin addon.xml')
    if root.attrib.get('id') != SKIN_ID:
        raise ValueError('installed addon.xml is not %s' % SKIN_ID)
    return root.attrib.get('version', '')


def _state(widgets, overrides):
    renderer = _RENDERER_MARKER in widgets
    registration = WIDGET_IDENTITY.encode('utf-8') in overrides
    if renderer and registration:
        if widgets.count(_RENDERER_MARKER) != 1 or overrides.count(WIDGET_IDENTITY.encode('utf-8')) != 1:
            return 'partial'
        return 'installed'
    if renderer or registration:
        return 'partial'
    return 'absent'


def _patch_overrides(data):
    _parse(data, 'overrides.xml')
    if WIDGET_IDENTITY.encode('utf-8') in data:
        raise ValueError('Custom Play History registration already exists unexpectedly')
    _one(data, b'\t<!-- Backgrounds -->', 'overrides widget insertion')
    _one(data, b'\t<widget-groupings>', 'overrides widget-groupings')
    result = data.replace(b'\t<!-- Backgrounds -->', _WIDGET + b'\t<!-- Backgrounds -->', 1)
    result = result.replace(b'\t<widget-groupings>\n', b'\t<widget-groupings>\n' + _WIDGET_GROUPINGS, 1)
    _parse(result, 'patched overrides.xml')
    if result.count(WIDGET_IDENTITY.encode('utf-8')) != 1 or result.count(_WIDGET_GROUPINGS) != 1:
        raise ValueError('patched overrides semantic verification failed')
    return result


def _patch_widgets(data, renderer):
    _parse(data, 'Includes_Widgets.xml')
    if _RENDERER_MARKER in data:
        raise ValueError('Custom Play History renderer already exists unexpectedly')
    generic = b'\t\t\t<include>skinshortcuts-template-widget1</include>'
    _one(data, generic, 'Widget 1 generic renderer')
    for condition in _ANIMATION_CONDITIONS:
        _one(data, b'condition="' + condition + b'"', 'Widget 1 style animation')
    _one(data, b'\t<!-- WIDGET 2 -->', 'renderer insertion before Widget 2')
    wrapped = (b'\t\t\t<control type="group">\n\t\t\t\t<visible>' + _GENERIC_VISIBLE + b'</visible>\n' + generic + b'\n\t\t\t</control>\n'
               b'\t\t\t<control type="group">\n\t\t\t\t<visible>' + _CUSTOM_VISIBLE + b'</visible>\n\t\t\t\t<include>MusicPlayHistoryWidget1</include>\n\t\t\t</control>')
    result = data.replace(generic, wrapped, 1)
    for condition in _ANIMATION_CONDITIONS:
        result = result.replace(b'condition="' + condition + b'"', b'condition="' + condition + _EXCLUSION + b'"', 1)
    result = result.replace(b'\t<!-- WIDGET 2 -->', renderer + b'\n\t<!-- WIDGET 2 -->', 1)
    _parse(result, 'patched Includes_Widgets.xml')
    if result.count(_RENDERER_MARKER) != 1:
        raise ValueError('patched renderer semantic verification failed')
    if result.count(_EXCLUSION) != 5 or result.count(_GENERIC_VISIBLE) != 1 or result.count(_CUSTOM_VISIBLE) != 2:
        raise ValueError('patched Widget 1 dispatch semantic verification failed')
    return result


def _backup(paths, originals):
    stamp = time.strftime('%Y%m%d-%H%M%S')
    root = os.path.join(_backup_root(), '%s-%s-%s' % (SKIN_ID, SKIN_VERSION, stamp))
    suffix = 0
    while os.path.exists(root):
        suffix += 1
        root = os.path.join(_backup_root(), '%s-%s-%s-%d' % (SKIN_ID, SKIN_VERSION, stamp, suffix))
    os.makedirs(root)
    manifest = {'skin_id': SKIN_ID, 'skin_version': SKIN_VERSION, 'created': stamp, 'files': {}}
    for key in ('widgets', 'overrides'):
        filename = os.path.basename(paths[key])
        _write(os.path.join(root, filename), originals[key])
        manifest['files'][filename] = {'sha256': _sha(originals[key])}
    _write(os.path.join(root, 'manifest.json'), json.dumps(manifest, sort_keys=True, indent=2).encode('utf-8'))
    return root


def _restore_directory(paths, directory):
    manifest_path = os.path.join(directory, 'manifest.json')
    if not os.path.isfile(manifest_path):
        raise ValueError('backup manifest is missing')
    manifest = json.loads(_read(manifest_path).decode('utf-8'))
    for key in ('widgets', 'overrides'):
        filename = os.path.basename(paths[key])
        original = _read(os.path.join(directory, filename))
        if _sha(original) != manifest['files'][filename]['sha256']:
            raise ValueError('backup hash does not match for %s' % filename)
        _parse(original, filename)
        _write(paths[key], original)


def install():
    paths = _skin_paths()
    if not all(os.path.isfile(paths[key]) for key in ('addon', 'widgets', 'overrides')):
        return 'Aeon Nox: SiLVO files were not found. No files changed.'
    addon, widgets, overrides = (_read(paths[key]) for key in ('addon', 'widgets', 'overrides'))
    version = _version(addon)
    if version != SKIN_VERSION:
        return 'Aeon Nox: SiLVO %s is unsupported; only 10.0.3 is supported. No files changed.' % version
    state = _state(widgets, overrides)
    if state == 'installed':
        return 'Custom Play History integration is already installed; no files changed.'
    if state == 'partial':
        return 'Partial or unexpected Custom Play History integration detected. No files changed.'
    renderer = _read(_renderer_path())
    _parse(b'<includes>' + renderer + b'</includes>', 'renderer payload')
    originals = {'widgets': widgets, 'overrides': overrides}
    try:
        patched = {'widgets': _patch_widgets(widgets, renderer), 'overrides': _patch_overrides(overrides)}
        backup = _backup(paths, originals)
        _write(paths['widgets'], patched['widgets'])
        _write(paths['overrides'], patched['overrides'])
        if _read(paths['widgets']) != patched['widgets'] or _read(paths['overrides']) != patched['overrides']:
            raise ValueError('written skin files did not verify byte-for-byte')
        return 'Installed. Backup: %s. Reload Skin or restart Kodi to activate it.' % backup
    except Exception as exc:
        try:
            if 'backup' in locals():
                _restore_directory(paths, backup)
            else:
                _write(paths['widgets'], originals['widgets'])
                _write(paths['overrides'], originals['overrides'])
        except Exception:
            pass
        return 'Installation stopped safely: %s' % exc


def restore():
    paths = _skin_paths()
    root = _backup_root()
    if not os.path.isdir(root):
        return 'No Custom Play History integration backup exists; no files changed.'
    candidates = [os.path.join(root, name) for name in os.listdir(root) if os.path.isdir(os.path.join(root, name))]
    if not candidates:
        return 'No Custom Play History integration backup exists; no files changed.'
    latest = max(candidates, key=os.path.getmtime)
    try:
        _restore_directory(paths, latest)
        return 'Restored exact pre-integration skin files from: %s. Reload Skin or restart Kodi.' % latest
    except Exception as exc:
        return 'Restore stopped safely: %s' % exc
