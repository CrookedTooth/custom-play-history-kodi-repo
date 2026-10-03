"""Portable launcher regression tests using a minimal fake Kodi GUI API."""
from __future__ import print_function
import os
import runpy
import shutil
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(ROOT, 'source', 'script.customplayhistory.silvo', 'default.py')


class Dialog(object):
    def __init__(self, calls, answer=True):
        self.calls = calls
        self.answer = answer

    def yesno(self, *args):
        self.calls.append(('yesno', args))
        return self.answer

    def ok(self, *args):
        self.calls.append(('ok', args))


def run_launcher(installer_source=None, answer=True):
    temp = tempfile.mkdtemp()
    old_path, old_gui = list(sys.path), sys.modules.get('xbmcgui')
    old_resources = {name: module for name, module in sys.modules.items()
                     if name == 'resources' or name.startswith('resources.')}
    calls = []
    try:
        shutil.copyfile(LAUNCHER, os.path.join(temp, 'default.py'))
        os.makedirs(os.path.join(temp, 'resources', 'lib'))
        open(os.path.join(temp, 'resources', '__init__.py'), 'w').close()
        open(os.path.join(temp, 'resources', 'lib', '__init__.py'), 'w').close()
        if installer_source is not None:
            with open(os.path.join(temp, 'resources', 'lib', 'installer.py'), 'w') as handle:
                handle.write(installer_source)
        gui = types.ModuleType('xbmcgui')
        gui.Dialog = lambda: Dialog(calls, answer)
        sys.modules['xbmcgui'] = gui
        for name in list(old_resources):
            sys.modules.pop(name, None)
        sys.path.insert(0, temp)
        runpy.run_path(os.path.join(temp, 'default.py'), run_name='__main__')
        return calls
    finally:
        sys.path[:] = old_path
        if old_gui is None:
            sys.modules.pop('xbmcgui', None)
        else:
            sys.modules['xbmcgui'] = old_gui
        for name in list(sys.modules):
            if name == 'resources' or name.startswith('resources.'):
                sys.modules.pop(name, None)
        sys.modules.update(old_resources)
        shutil.rmtree(temp)


INSTALLER = (
    'def install():\n'
    '    return {"ok": False, "code": "E104", "reason": "anchor missing", "state": "absent", "changed": "NO", "backup": "NO"}\n'
    'def restore():\n'
    '    return {"ok": True, "code": "I401", "reason": "restored", "state": "RESTORED", "changed": "YES", "backup": "YES"}\n'
    'def _result(ok, code, reason):\n'
    '    return {"ok": ok, "code": code, "reason": reason, "state": "UNKNOWN", "changed": "UNKNOWN", "backup": "UNKNOWN"}\n'
    'def format_result(result):\n'
    '    return "Installation failed\\n%s\\n%s" % (result["code"], result["reason"])\n')


def main():
    calls = run_launcher(None)
    assert calls[-1][0] == 'ok' and 'Code: E998' in calls[-1][1]
    calls = run_launcher(INSTALLER)
    assert calls[-1][0] == 'ok' and 'E104' in calls[-1][1][1]
    calls = run_launcher(INSTALLER.replace('return "Installation failed\\n%s\\n%s" % (result["code"], result["reason"])',
                                           'raise RuntimeError("dialog formatting failed")'))
    assert calls[-1][0] == 'ok' and 'Code: E997' in calls[-1][1]
    calls = run_launcher(INSTALLER.replace('return {"ok": False, "code": "E104", "reason": "anchor missing", "state": "absent", "changed": "NO", "backup": "NO"}',
                                           'raise RuntimeError("install failed")'))
    assert calls[-1][0] == 'ok' and 'E999' in calls[-1][1][1]
    print('SiLVO defensive launcher tests: PASS')


if __name__ == '__main__':
    main()
