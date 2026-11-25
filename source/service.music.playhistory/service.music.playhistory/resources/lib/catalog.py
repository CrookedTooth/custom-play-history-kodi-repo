# -*- coding: utf-8 -*-
"""Explicit one-time/full-library catalog enrichment for album denominators."""
from __future__ import absolute_import

import json
import xbmc

from .maintenance import CatalogRefreshAborted


SONG_PROPERTIES=['album','albumartist','artist','art','disc','duration','file','musicbrainzalbumid','musicbrainztrackid','track','year']

class CatalogReadError(RuntimeError):
    """A library read failed or returned an incomplete, unverifiable result."""

def rows_from_kodi(logger, should_abort=None):
    rows=[]; start=0; expected_total=None
    while True:
        if xbmc.getCondVisibility('Library.IsScanningMusic'):
            raise CatalogReadError('music library scan is in progress')
        if should_abort and should_abort():
            raise CatalogRefreshAborted('media playback started during catalog read')
        request={'jsonrpc':'2.0','id':'playhistory-catalog','method':'AudioLibrary.GetSongs','params':{'properties':SONG_PROPERTIES,'limits':{'start':start,'end':start+500}}}
        try:
            response=json.loads(xbmc.executeJSONRPC(json.dumps(request)))
            if not isinstance(response, dict) or response.get('error') or not isinstance(response.get('result'), dict):
                raise CatalogReadError('AudioLibrary.GetSongs returned an error or missing result')
            result=response['result']
            limits=result.get('limits')
            if not isinstance(limits, dict) or any(type(limits.get(k)) is not int for k in ('start','end','total')):
                raise CatalogReadError('AudioLibrary.GetSongs returned invalid limits')
            page_start, end, total=limits['start'], limits['end'], limits['total']
            songs=result.get('songs', [])
            if not isinstance(songs, list) or any(not isinstance(song, dict) for song in songs):
                raise CatalogReadError('AudioLibrary.GetSongs returned invalid songs')
            if (page_start != start or total < 0 or end < start or end > total or
                    end != start + len(songs) or (expected_total is not None and total != expected_total) or
                    (not songs and end < total)):
                raise CatalogReadError('AudioLibrary.GetSongs returned incomplete or inconsistent pagination')
            expected_total=total
        except CatalogReadError:
            raise
        except Exception as exc:
            raise CatalogReadError('AudioLibrary.GetSongs read failed: %s' % exc)
        for index, song in enumerate(songs):
            if should_abort and index % 100 == 0 and should_abort():
                raise CatalogRefreshAborted('media playback started during catalog page read')
            rows.append({'kodi_dbid':song.get('songid'),'kodi_album_dbid':song.get('albumid'),'title':song.get('title') or song.get('label'),'artist':' / '.join(song.get('artist') or []),'album_artist':' / '.join(song.get('albumartist') or []),'album':song.get('album'),'track':song.get('track'),'disc':song.get('disc'),'year':song.get('year'),'duration_ms':int(float(song.get('duration') or 0) * 1000) or None,'file':song.get('file'),'musicbrainz_recording_id':song.get('musicbrainztrackid'),'musicbrainz_release_id':song.get('musicbrainzalbumid'),'artwork_json':json.dumps(song.get('art') or {})})
        if xbmc.getCondVisibility('Library.IsScanningMusic'):
            raise CatalogReadError('music library scan began during catalog read')
        if end == total: break
        start=end
    logger('catalog enrichment read %d Kodi songs' % len(rows))
    return rows
