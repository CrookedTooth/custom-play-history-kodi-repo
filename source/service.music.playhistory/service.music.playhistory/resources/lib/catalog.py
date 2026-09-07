# -*- coding: utf-8 -*-
"""Explicit one-time/full-library catalog enrichment for album denominators."""
from __future__ import absolute_import

import json
import xbmc

from .maintenance import CatalogRefreshAborted


SONG_PROPERTIES=['album','albumartist','artist','art','disc','duration','file','musicbrainzalbumid','musicbrainztrackid','track','year']

def rows_from_kodi(logger, should_abort=None):
    rows=[]; start=0
    while True:
        if should_abort and should_abort():
            raise CatalogRefreshAborted('media playback started during catalog read')
        request={'jsonrpc':'2.0','id':'playhistory-catalog','method':'AudioLibrary.GetSongs','params':{'properties':SONG_PROPERTIES,'limits':{'start':start,'end':start+500}}}
        result=json.loads(xbmc.executeJSONRPC(json.dumps(request))).get('result',{})
        songs=result.get('songs',[])
        for index, song in enumerate(songs):
            if should_abort and index % 100 == 0 and should_abort():
                raise CatalogRefreshAborted('media playback started during catalog page read')
            rows.append({'kodi_dbid':song.get('songid'),'kodi_album_dbid':song.get('albumid'),'title':song.get('title') or song.get('label'),'artist':' / '.join(song.get('artist') or []),'album_artist':' / '.join(song.get('albumartist') or []),'album':song.get('album'),'track':song.get('track'),'disc':song.get('disc'),'year':song.get('year'),'duration_ms':int(float(song.get('duration') or 0) * 1000) or None,'file':song.get('file'),'musicbrainz_recording_id':song.get('musicbrainztrackid'),'musicbrainz_release_id':song.get('musicbrainzalbumid'),'artwork_json':json.dumps(song.get('art') or {})})
        limits=result.get('limits',{})
        if limits.get('end',0)>=limits.get('total',0) or not songs: break
        start=limits.get('end',start+len(songs))
    logger('catalog enrichment read %d Kodi songs' % len(rows))
    return rows
