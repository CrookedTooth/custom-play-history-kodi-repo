# -*- coding: utf-8 -*-
"""Bounded ranking rebuilds and read-only cache access."""
from __future__ import absolute_import

import json
import os
import sqlite3
import time

import xbmcgui
import xbmcvfs


TOP_LIMIT = 50


def _art(value):
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}


class RankingEngine(object):
    def __init__(self, database, logger):
        self.database = database
        self.log = logger
        self._last_check = 0.0
        self._publish_widget_revision()

    def _publish_widget_revision(self):
        """Expose the committed ranking revision to dynamic widget content."""
        row = self.database.connection.execute(
            "SELECT value FROM schema_meta WHERE key='rankings_updated_at'").fetchone()
        revision = row[0] if row and row[0] else '0'
        xbmcgui.Window(10000).setProperty('MusicPlayHistory.WidgetRevision', revision)

    def maybe_rebuild_when_idle(self, is_playing):
        now = time.monotonic()
        if is_playing or now - self._last_check < 5.0:
            return False
        self._last_check = now
        row = self.database.connection.execute("SELECT value FROM schema_meta WHERE key='rankings_dirty'").fetchone()
        if row and row[0] == '1':
            self.rebuild()
            return True
        return False

    def rebuild(self):
        started = time.monotonic()
        tracks = self._tracks()
        albums = self._albums()
        artists = self.artists()
        now = int(time.time())
        with self.database.connection:
            self.database.connection.execute("DELETE FROM track_rank_cache")
            self.database.connection.execute("DELETE FROM album_rank_cache")
            if self.database.schema_version() >= 13:
                self.database.connection.execute("DELETE FROM artist_rank_cache")
            for item in tracks:
                self.database.connection.execute("INSERT INTO track_rank_cache(track_key,rank_position,total_plays,updated_at,payload_json) VALUES(?,?,?,?,?)", (item['identity'], item['rank'], item['plays'], now, json.dumps(item, ensure_ascii=False)))
            for item in albums:
                self.database.connection.execute("INSERT INTO album_rank_cache(album_key,rank_position,score,breadth_percent,raw_plays,distinct_tracks,played_tracks,updated_at,payload_json) VALUES(?,?,?,?,?,?,?,?,?)", (item['identity'], item['rank'], item['score'], item['coverage'], item['plays'], item['trackcount'], item['playedtracks'], now, json.dumps(item, ensure_ascii=False)))
            if self.database.schema_version() >= 13:
                for item in artists:
                    self.database.connection.execute("INSERT INTO artist_rank_cache(artist_key,rank_position,total_plays,updated_at,payload_json) VALUES(?,?,?,?,?)", (item['identity'], item['rank'], item['plays'], now, json.dumps(item, ensure_ascii=False)))
            self.database.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('rankings_dirty','0')")
            self.database.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('rankings_updated_at',?)", (str(now),))
        self._publish_widget_revision()
        self.log("ranking cache rebuilt: %d tracks, %d albums, %d artists in %.3fs" % (len(tracks), len(albums), len(artists), time.monotonic() - started))

    def _tracks(self):
        if self.database.schema_version() >= 13:
            rows = self.database.connection.execute("""SELECT t.track_key,t.display_title,t.display_artist,
                t.display_album,t.preferred_path,t.preferred_artwork_json,t.disc,t.track_number,
                sum(p.play_count) AS plays,max(p.occurred_at) AS lastplayed
                FROM metadata_event_projection p JOIN metadata_track t
                  ON t.track_key=p.metadata_track_key
                GROUP BY t.track_key ORDER BY plays DESC,lastplayed DESC,t.track_key LIMIT ?""",
                (TOP_LIMIT,)).fetchall()
            result=[]
            for row in rows:
                result.append({'identity':row['track_key'],'title':row['display_title'] or '',
                    'artist':row['display_artist'] or '', 'album':row['display_album'] or '',
                    'disc':row['disc'] or 0,'track':row['track_number'] or 0,
                    'path':row['preferred_path'] or '', 'art':_art(row['preferred_artwork_json']), 'plays':row['plays'],
                    'baselineplays':0,'liveplays':row['plays'],'lastplayed':row['lastplayed'] or 0,
                    'recording':''})
            for rank,item in enumerate(result,1): item['rank']=rank
            return result
        generation = self.database.authoritative_catalog_generation_id()
        if generation is None:
            rows = self.database.connection.execute("""SELECT s.logical_song_key, s.display_artist,s.display_title,s.preferred_path,s.preferred_artwork_json,
            t.imported_plays,t.observed_plays,t.total_plays,t.last_played_at,
            i.album,i.disc,i.track_number
            FROM logical_song s JOIN logical_song_totals t USING(logical_song_key)
            LEFT JOIN track_identity i ON i.identity_key=s.preferred_track_key
            WHERE t.total_plays>0""").fetchall()
        else:
            rows = self.database.connection.execute("""SELECT s.logical_song_key,s.display_artist,s.display_title,s.preferred_path,s.preferred_artwork_json,
                t.imported_plays,t.observed_plays,t.total_plays,t.last_played_at,i.album,i.disc,i.track_number
                FROM catalog_logical_song s JOIN catalog_logical_totals t
                  ON t.generation_id=s.generation_id AND t.logical_song_key=s.logical_song_key
                LEFT JOIN track_identity i ON i.identity_key=s.preferred_track_key
                WHERE s.generation_id=? AND t.total_plays>0""", (generation,)).fetchall()
        result=[]
        for row in rows:
            result.append({'identity':row['logical_song_key'],'title':row['display_title'] or '', 'artist':row['display_artist'] or '', 'album':row['album'] or '', 'disc':row['disc'] or 0, 'track':row['track_number'] or 0, 'path':row['preferred_path'] or '', 'art':_art(row['preferred_artwork_json']), 'plays':row['total_plays'], 'baselineplays':row['imported_plays'], 'liveplays':row['observed_plays'], 'lastplayed':row['last_played_at'] or 0, 'recording':''})
        result.sort(key=lambda item:(-item['plays'],-item['lastplayed'],item['identity']))
        result = result[:TOP_LIMIT]
        for rank,item in enumerate(result,1): item['rank']=rank
        return result

    def _albums(self):
        if self.database.schema_version() >= 13:
            rows = self.database.connection.execute("""SELECT a.album_key,a.display_album,a.display_artist,
                a.year,a.kodi_album_dbid,a.preferred_artwork_json,
                count(*) AS album_plays,max(o.qualified_at) AS lastplayed
                FROM metadata_album_occurrence o JOIN metadata_album a ON a.album_key=o.metadata_album_key
                WHERE o.qualified_at IS NOT NULL GROUP BY a.album_key
                ORDER BY album_plays DESC,lastplayed DESC,a.album_key LIMIT ?""", (TOP_LIMIT,)).fetchall()
            result=[]
            for row in rows:
                result.append({'identity':row['album_key'],'album':row['display_album'] or '',
                    'artist':row['display_artist'] or '','year':row['year'] or '',
                    'albumid':row['kodi_album_dbid'] or 0,'art':_art(row['preferred_artwork_json']),
                    'plays':int(row['album_plays'] or 0),'playedtracks':0,'trackcount':0,
                    'coverage':0.0,'score':float(row['album_plays'] or 0),
                    'lastplayed':row['lastplayed'] or 0})
            for rank,item in enumerate(result,1): item['rank']=rank
            return result
        generation = self.database.authoritative_catalog_generation_id()
        if generation is None:
            rows=self.database.connection.execute("""SELECT o.logical_album_key,a.title,a.artist,a.year,a.kodi_album_dbid,a.artwork_json,
            count(*) AS album_plays,max(o.qualified_at) AS lastplayed,max(o.distinct_tracks) AS playedtracks,
            (SELECT count(DISTINCT s.logical_song_key) FROM logical_album_current_song s
             WHERE s.logical_album_key=o.logical_album_key) AS trackcount
            FROM album_play_occurrence o JOIN logical_album_current a USING(logical_album_key)
            WHERE o.qualified_at IS NOT NULL
            GROUP BY o.logical_album_key""").fetchall()
        else:
            rows=self.database.connection.execute("""SELECT o.logical_album_key,a.title,a.artist,a.year,a.kodi_album_dbid,a.artwork_json,
                count(*) AS album_plays,max(o.qualified_at) AS lastplayed,max(o.distinct_tracks) AS playedtracks,
                (SELECT count(DISTINCT s.logical_song_key) FROM catalog_logical_album_song s
                 WHERE s.generation_id=? AND s.logical_album_key=o.logical_album_key) AS trackcount
                FROM album_play_occurrence o JOIN catalog_logical_album a
                  ON a.logical_album_key=o.logical_album_key AND a.generation_id=?
                WHERE o.qualified_at IS NOT NULL GROUP BY o.logical_album_key""", (generation,generation)).fetchall()
        result=[]
        for row in rows:
            trackcount=int(row['trackcount'] or 0)
            playedtracks=int(row['playedtracks'] or 0)
            plays=int(row['album_plays'] or 0)
            result.append({'identity':row['logical_album_key'],'album':row['title'] or '',
                'artist':row['artist'] or '','year':row['year'] or '',
                'albumid':row['kodi_album_dbid'] or 0,'art':_art(row['artwork_json']),
                'plays':plays,'playedtracks':playedtracks,'trackcount':trackcount,
                'coverage':(100.0*playedtracks/trackcount) if trackcount else 0.0,
                # Retained for existing list-item consumers; it is no longer a
                # per-track normalized score and has exactly album-play meaning.
                'score':float(plays),'lastplayed':row['lastplayed'] or 0})
        result.sort(key=lambda item:(-item['plays'],-item['lastplayed'],item['identity']))
        for rank,item in enumerate(result[:TOP_LIMIT],1): item['rank']=rank
        return result[:TOP_LIMIT]

    def artists(self):
        """Top artists for a future widget/API; no full-library catalog read."""
        if self.database.schema_version() < 13:
            return []
        rows = self.database.connection.execute("""SELECT a.artist_key,a.display_artist,
            sum(p.play_count) AS plays,max(p.occurred_at) AS lastplayed
            FROM metadata_event_projection p JOIN metadata_artist a ON a.artist_key=p.metadata_artist_key
            GROUP BY a.artist_key ORDER BY plays DESC,lastplayed DESC,a.artist_key LIMIT ?""",
            (TOP_LIMIT,)).fetchall()
        return [{'identity':row['artist_key'],'artist':row['display_artist'] or '',
                 'plays':row['plays'],'lastplayed':row['lastplayed'] or 0,'rank':index}
                for index,row in enumerate(rows,1)]


class CacheReader(object):
    """Read-only endpoint access. It cannot run migrations or rebuild rankings."""
    def __init__(self, addon_id):
        directory=xbmcvfs.translatePath("special://profile/addon_data/%s/" % addon_id)
        path=os.path.join(directory,'playhistory.db').replace('\\','/')
        self.connection=sqlite3.connect('file:///'+path+'?mode=ro',uri=True)
        self.connection.row_factory=sqlite3.Row

    def rows(self, view, limit):
        table={'tracks':'track_rank_cache','albums':'album_rank_cache','artists':'artist_rank_cache'}.get(view,'track_rank_cache')
        result=self.connection.execute("SELECT payload_json FROM %s ORDER BY rank_position ASC LIMIT ?" % table,(max(0,min(int(limit),TOP_LIMIT)),)).fetchall()
        return [json.loads(row['payload_json']) for row in result if row['payload_json']]

    def close(self): self.connection.close()
