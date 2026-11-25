import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'source' / 'service.music.playhistory' / 'service.music.playhistory'))
sys.modules.setdefault('xbmc', types.ModuleType('xbmc'))
sys.modules.setdefault('xbmcvfs', types.ModuleType('xbmcvfs'))
from resources.lib.database import HistoryDatabase


class CatalogDatabaseTests(unittest.TestCase):
    def test_out_of_library_play_without_catalog_is_recorded(self):
        with tempfile.TemporaryDirectory(prefix='test-db-', dir=str(Path(__file__).parent)) as directory:
            db = HistoryDatabase('test', lambda message: None, str(Path(directory) / 'history.db'))
            db.initialize()
            metadata = {'title': 'Unscanned Song', 'artist': 'Artist', 'album': 'Album',
                        'file': 'file:///unscanned/song.flac', 'duration_ms': 4000}
            self.assertIsNone(db.authoritative_catalog_generation_id())
            self.assertTrue(db.record_live_play('out-of-library', metadata, 4, 3.2, 1700000000))
            self.assertEqual(db.connection.execute('select count(*) from play_event').fetchone()[0], 1)
            db.close()

    def test_empty_candidate_does_not_replace_trusted_catalog_or_history(self):
        with tempfile.TemporaryDirectory(prefix='test-db-', dir=str(Path(__file__).parent)) as directory:
            path = str(Path(directory) / 'history.db')
            db = HistoryDatabase('test', lambda message: None, path)
            db.initialize()
            row = {'kodi_dbid': 1, 'kodi_album_dbid': 2, 'title': 'Song',
                   'artist': 'Artist', 'album_artist': 'Artist', 'album': 'Album',
                   'track': 1, 'disc': 1, 'duration_ms': 3000, 'file': 'file:///test/song.flac'}
            generation, metrics = db.replace_album_catalog_staged([row])
            self.assertTrue(db.record_live_play('disposable-event', row, 3, 2, 1700000000))
            before = [tuple(x) for x in db.connection.execute('select * from play_event')]
            with self.assertRaisesRegex(RuntimeError, 'candidate catalog is incomplete'):
                db.replace_album_catalog_staged([])
            self.assertEqual(db.authoritative_catalog_generation_id(), generation)
            self.assertEqual([tuple(x) for x in db.connection.execute('select * from play_event')], before)
            self.assertEqual(db.connection.execute('pragma quick_check').fetchone()[0], 'ok')
            db.close()
            db = HistoryDatabase('test', lambda message: None, path)
            db.initialize()
            self.assertEqual(db.schema_version(), 13)
            self.assertEqual([tuple(x) for x in db.connection.execute('select * from play_event')], before)
            db.close()


if __name__ == '__main__': unittest.main()
