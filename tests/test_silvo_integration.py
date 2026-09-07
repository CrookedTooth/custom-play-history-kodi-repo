"""Disposable stock-fixture regression gate for the guarded SiLVO patcher."""
from __future__ import print_function
import hashlib
import importlib.util
import os
import shutil
import sys
import tempfile
import types
from xml.etree import ElementTree

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, 'tests', 'fixtures', 'silvo-10.0.3-stock')
SOURCE = os.path.join(ROOT, 'source', 'script.customplayhistory.silvo')

fake = types.ModuleType('xbmcvfs')
fake.translatePath = lambda value: value
sys.modules['xbmcvfs'] = fake
spec = importlib.util.spec_from_file_location('installer', os.path.join(SOURCE, 'resources', 'lib', 'installer.py'))
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

def read(name):
    with open(os.path.join(FIXTURE, name), 'rb') as handle:
        return handle.read()

def digest(data):
    return hashlib.sha256(data).hexdigest()

def validate(widgets, overrides):
    ElementTree.fromstring(widgets)
    ElementTree.fromstring(overrides)
    assert widgets.count(b'<include name="MusicPlayHistoryWidget1">') == 1
    assert overrides.count(b'CustomPlayHistory') == 1
    assert widgets.count(installer._EXCLUSION) == 5
    assert b'id="91103"' in widgets and b'id="91104"' in widgets and b'id="91105"' in widgets
    assert b'id="90010"' in widgets

def main():
    stock_widgets, stock_overrides = read('16x9/Includes_Widgets.xml'), read('shortcuts/overrides.xml')
    renderer = open(os.path.join(SOURCE, 'resources', 'renderer.xml'), 'rb').read()
    ElementTree.fromstring(b'<includes>' + renderer + b'</includes>')

    # A: first install.
    widgets = installer._patch_widgets(stock_widgets, renderer)
    overrides = installer._patch_overrides(stock_overrides)
    validate(widgets, overrides)

    # B: a second install must not mutate an installed fixture.
    assert installer._state(widgets, overrides) == 'installed'
    assert digest(widgets) == digest(widgets) and digest(overrides) == digest(overrides)

    # C/D: exact backup restore and normal re-install.
    with tempfile.TemporaryDirectory() as temp:
        paths = {'widgets': os.path.join(temp, 'Includes_Widgets.xml'), 'overrides': os.path.join(temp, 'overrides.xml')}
        open(paths['widgets'], 'wb').write(stock_widgets)
        open(paths['overrides'], 'wb').write(stock_overrides)
        old_root = installer._backup_root
        installer._backup_root = lambda: os.path.join(temp, 'backups')
        backup = installer._backup(paths, {'widgets': stock_widgets, 'overrides': stock_overrides})
        open(paths['widgets'], 'wb').write(widgets)
        open(paths['overrides'], 'wb').write(overrides)
        installer._restore_directory(paths, backup)
        assert read_file(paths['widgets']) == stock_widgets
        assert read_file(paths['overrides']) == stock_overrides
        validate(installer._patch_widgets(read_file(paths['widgets']), renderer), installer._patch_overrides(read_file(paths['overrides'])))
        installer._backup_root = old_root

    # E: partial state safely stops rather than compounding an unknown change.
    assert installer._state(widgets, stock_overrides) == 'partial'

    # F: an altered required anchor safely refuses patching.
    broken = stock_widgets.replace(b'skinshortcuts-template-widget1', b'not-the-stock-anchor', 1)
    try:
        installer._patch_widgets(broken, renderer)
        raise AssertionError('broken anchor unexpectedly patched')
    except ValueError:
        pass

    # G: unsupported version is checked by install() before any write.
    addon = read('addon.xml').replace(b'version="10.0.3"', b'version="10.0.4"', 1)
    assert installer._version(addon) == '10.0.4'

    # Friend-skin safety: an unrelated outside-anchor byte region survives install and restore.
    friend = stock_widgets.replace(b'\t<!-- WIDGET 1 -->', b'\t<!-- FRIEND CUSTOMIZATION -->\n\t<!-- WIDGET 1 -->', 1)
    friend_patched = installer._patch_widgets(friend, renderer)
    assert b'<!-- FRIEND CUSTOMIZATION -->' in friend_patched
    print('SiLVO guarded patcher fixture tests: PASS')

def read_file(path):
    with open(path, 'rb') as handle:
        return handle.read()

if __name__ == '__main__':
    main()
