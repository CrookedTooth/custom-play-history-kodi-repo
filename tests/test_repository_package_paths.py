"""Verify the published layout Kodi derives from repository metadata."""
from __future__ import print_function
import hashlib
import os
from xml.etree import ElementTree

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLISH = os.path.join(ROOT, 'publish')


def sha(path):
    with open(path, 'rb') as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def main():
    # Kodi CAddonInfoBuilder derives datadir/<addon-id>/<addon-id>-<version>.zip.
    metadata = ElementTree.parse(os.path.join(PUBLISH, 'addons.xml')).getroot()
    expected = {
        'service.music.playhistory': '0.1.0',
        'script.customplayhistory.silvo': '0.1.2',
    }
    for element in metadata.findall('addon'):
        addon_id, version = element.attrib['id'], element.attrib['version']
        if addon_id not in expected:
            continue
        assert version == expected[addon_id]
        filename = '%s-%s.zip' % (addon_id, version)
        root_copy = os.path.join(PUBLISH, filename)
        kodi_copy = os.path.join(PUBLISH, addon_id, filename)
        assert os.path.isfile(root_copy), root_copy
        assert os.path.isfile(kodi_copy), kodi_copy
        assert sha(root_copy) == sha(kodi_copy), addon_id
    print('Kodi repository derived package-path tests: PASS')


if __name__ == '__main__':
    main()
