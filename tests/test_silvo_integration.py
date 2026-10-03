"""Disposable stock-fixture regression gate for the guarded SiLVO patcher."""
from __future__ import print_function
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


def read_file(path):
    with open(path, 'rb') as handle:
        return handle.read()


def validate(widgets, overrides):
    ElementTree.fromstring(widgets)
    ElementTree.fromstring(overrides)
    assert widgets.count(b'<include name="MusicPlayHistoryWidget1">') == 1
    assert overrides.count(b'CustomPlayHistory') == 1
    assert widgets.count(installer._EXCLUSION) == 5


class Sandbox(object):
    def __init__(self):
        self.temp = tempfile.mkdtemp()
        skin = os.path.join(self.temp, 'skin')
        os.makedirs(os.path.join(skin, '16x9'))
        os.makedirs(os.path.join(skin, 'shortcuts'))
        self.paths = {'addon': os.path.join(skin, 'addon.xml'),
                      'widgets': os.path.join(skin, '16x9', 'Includes_Widgets.xml'),
                      'overrides': os.path.join(skin, 'shortcuts', 'overrides.xml')}
        self.write('addon', read('addon.xml'))
        self.write('widgets', read('16x9/Includes_Widgets.xml'))
        self.write('overrides', read('shortcuts/overrides.xml'))
        self.originals = {key: read_file(value) for key, value in self.paths.items()}
        self.old_paths, self.old_backup, self.old_renderer = installer._skin_paths, installer._backup_root, installer._renderer_path
        installer._skin_paths = lambda: self.paths
        installer._backup_root = lambda: os.path.join(self.temp, 'backups')
        installer._renderer_path = lambda: os.path.join(SOURCE, 'resources', 'renderer.xml')

    def write(self, key, data):
        with open(self.paths[key], 'wb') as handle:
            handle.write(data)

    def close(self):
        installer._skin_paths, installer._backup_root, installer._renderer_path = self.old_paths, self.old_backup, self.old_renderer
        shutil.rmtree(self.temp)


def with_sandbox(function):
    box = Sandbox()
    try:
        function(box)
    finally:
        box.close()


def assert_unchanged(box):
    for key in ('widgets', 'overrides'):
        assert read_file(box.paths[key]) == box.originals[key]


def test_success_and_restore(box):
    result = installer.install()
    assert result['ok'] and result['code'] == 'I002' and result['backup'] == 'YES'
    validate(read_file(box.paths['widgets']), read_file(box.paths['overrides']))
    assert installer._state(read_file(box.paths['widgets']), read_file(box.paths['overrides'])) == 'installed'
    restored = installer.restore()
    assert restored['ok'] and restored['code'] == 'I401'
    assert_unchanged(box)


def test_version_missing_and_partial(box):
    box.write('addon', box.originals['addon'].replace(b'version="10.0.3"', b'version="10.0.4"', 1))
    result = installer.install()
    assert not result['ok'] and result['code'] == 'E201'
    assert_unchanged(box)
    box.write('addon', box.originals['addon'])
    os.unlink(box.paths['widgets'])
    result = installer.install()
    assert not result['ok'] and result['code'] == 'E101'
    box.write('widgets', box.originals['widgets'])
    assert installer.install()['ok']
    box.write('overrides', box.originals['overrides'])
    result = installer.install()
    assert not result['ok'] and result['code'] == 'E301'


def test_anchor_failures(box):
    broken_widgets = box.originals['widgets'].replace(b'skinshortcuts-template-widget1', b'not-the-stock-anchor', 1)
    box.write('widgets', broken_widgets)
    result = installer.install()
    assert not result['ok'] and result['code'] == 'E104'
    assert read_file(box.paths['widgets']) == broken_widgets
    assert read_file(box.paths['overrides']) == box.originals['overrides']
    box.write('widgets', box.originals['widgets'])
    duplicate = b'\t\t\t<include>skinshortcuts-template-widget1</include>'
    box.write('widgets', box.originals['widgets'].replace(duplicate, duplicate + b'\n' + duplicate, 1))
    result = installer.install()
    assert not result['ok'] and result['code'] == 'E104'
    box.write('widgets', box.originals['widgets'])
    box.write('overrides', box.originals['overrides'].replace(b'\t<!-- Backgrounds -->', b'\t<!-- Other -->', 1))
    result = installer.install()
    assert not result['ok'] and result['code'] == 'E105'
    assert read_file(box.paths['widgets']) == box.originals['widgets']


def test_backup_and_write_rollback(box):
    original_backup = installer._backup
    installer._backup = lambda paths, originals: (_ for _ in ()).throw(IOError('backup unavailable'))
    try:
        result = installer.install()
        assert not result['ok'] and result['code'] == 'E202'
        assert_unchanged(box)
    finally:
        installer._backup = original_backup

    original_write, calls = installer._write, []
    def fail_second_skin_write(path, data):
        if path in (box.paths['widgets'], box.paths['overrides']):
            calls.append(path)
            if path == box.paths['overrides'] and calls.count(path) == 1:
                raise IOError('write denied')
        return original_write(path, data)
    installer._write = fail_second_skin_write
    try:
        result = installer.install()
        assert not result['ok'] and result['code'] == 'E208'
        assert_unchanged(box)
    finally:
        installer._write = original_write


def test_malformed_postwrite_and_unreadable(box):
    original_patch = installer._patch_widgets
    installer._patch_widgets = lambda data, renderer: b'<broken'
    try:
        result = installer.install()
        assert not result['ok'] and result['code'] == 'E107', result
        assert_unchanged(box)
    finally:
        installer._patch_widgets = original_patch

    original_read, reads = installer._read, [0]
    def corrupt_verification(path):
        value = original_read(path)
        if path == box.paths['widgets']:
            reads[0] += 1
            if reads[0] == 2:
                return value + b'\n<!-- altered -->'
        return value
    installer._read = corrupt_verification
    try:
        result = installer.install()
        assert not result['ok'] and result['code'] == 'E209'
        assert_unchanged(box)
    finally:
        installer._read = original_read

    installer._read = lambda path: (_ for _ in ()).throw(IOError('permission denied')) if path == box.paths['addon'] else original_read(path)
    try:
        result = installer.install()
        assert not result['ok'] and result['code'] == 'E102'
        assert_unchanged(box)
    finally:
        installer._read = original_read


def test_friend_safe_structure(box):
    friend = box.originals['widgets'].replace(b'\t<!-- WIDGET 1 -->', b'\t<!-- FRIEND CUSTOMIZATION -->\n\t<!-- WIDGET 1 -->', 1)
    box.write('widgets', friend)
    result = installer.install()
    assert result['ok']
    assert b'<!-- FRIEND CUSTOMIZATION -->' in read_file(box.paths['widgets'])


def main():
    for test in (test_success_and_restore, test_version_missing_and_partial, test_anchor_failures,
                 test_backup_and_write_rollback, test_malformed_postwrite_and_unreadable,
                 test_friend_safe_structure):
        with_sandbox(test)
    renderer = read_file(os.path.join(SOURCE, 'resources', 'renderer.xml'))
    ElementTree.fromstring(b'<includes>' + renderer + b'</includes>')
    print('SiLVO guarded patcher diagnostic regression tests: PASS')


if __name__ == '__main__':
    main()
