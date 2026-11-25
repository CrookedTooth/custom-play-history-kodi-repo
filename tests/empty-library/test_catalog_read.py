import json
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'source' / 'service.music.playhistory' / 'service.music.playhistory'))
xbmc = types.ModuleType('xbmc')
xbmc.getCondVisibility = lambda condition: False
sys.modules['xbmc'] = xbmc
from resources.lib.catalog import rows_from_kodi, CatalogReadError, CatalogRefreshAborted


def page(start, end, total, songs=None):
    result = {'limits': {'start': start, 'end': end, 'total': total}}
    if songs is not None: result['songs'] = songs
    return json.dumps({'jsonrpc': '2.0', 'id': 'playhistory-catalog', 'result': result})


class CatalogReadTests(unittest.TestCase):
    def run_pages(self, pages):
        responses = iter(pages)
        xbmc.executeJSONRPC = lambda request: next(responses)
        return rows_from_kodi(lambda message: None)

    def test_explicit_zero_is_valid(self):
        self.assertEqual(self.run_pages([page(0, 0, 0)]), [])

    def test_valid_song(self):
        result = self.run_pages([page(0, 1, 1, [{'songid': 2, 'title': 'Song', 'artist': ['Artist'], 'duration': 5}])])
        self.assertEqual(result[0]['title'], 'Song')
        self.assertEqual(result[0]['duration_ms'], 5000)

    def test_json_rpc_error_never_becomes_empty(self):
        for response in ('{}', 'invalid', json.dumps({'error': {'code': -1}}), json.dumps({'result': {}})):
            with self.subTest(response=response), self.assertRaises(CatalogReadError): self.run_pages([response])

    def test_truncated_and_changing_pages_refused(self):
        responses = [page(0, 0, 2), page(0, 1, 2, []), page(1, 1, 1)]
        for response in responses:
            with self.subTest(response=response), self.assertRaises(CatalogReadError): self.run_pages([response])
        with self.assertRaises(CatalogReadError):
            self.run_pages([page(0, 1, 2, [{'title': 'one'}]), page(1, 2, 3, [{'title': 'two'}])])

    def test_read_exception_and_playback_abort(self):
        def broken(request): raise OSError('database unavailable')
        xbmc.executeJSONRPC = broken
        with self.assertRaises(CatalogReadError): rows_from_kodi(lambda message: None)
        with self.assertRaises(CatalogRefreshAborted): rows_from_kodi(lambda message: None, lambda: True)

    def test_scan_in_progress_refused(self):
        xbmc.getCondVisibility = lambda condition: True
        try:
            with self.assertRaises(CatalogReadError): self.run_pages([page(0, 0, 0)])
        finally:
            xbmc.getCondVisibility = lambda condition: False


if __name__ == '__main__': unittest.main()
