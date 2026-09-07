# -*- coding: utf-8 -*-
"""Read-only cached ranking directory endpoints."""
from __future__ import absolute_import

import sys
import time
import json
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
try:
    from urllib.parse import parse_qs
except ImportError:
    from urlparse import parse_qs
import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin

from resources.lib.ranking import CacheReader
from resources.lib.database import HistoryDatabase
from resources.lib.continuity import InboxProcessor, ManifestError
from resources.lib.source_root import configured_source_root

ADDON_ID='service.music.playhistory'

def value(params,name,default=''):
    return params.get(name,[default])[0]

def compact_last_played_date(value):
    try:
        timestamp=int(value)
        if timestamp <= 0:
            return ''
        date=time.localtime(timestamp)
    except (TypeError, ValueError, OverflowError):
        return ''
    return '%d/%d/%02d' % (date.tm_mon,date.tm_mday,date.tm_year % 100)

def track_item(item):
    li=xbmcgui.ListItem(label=item['title'],label2=item['artist'])
    li.setInfo('music',{'title':item['title'],'artist':item['artist'],'album':item['album'],'tracknumber':item['track'],'discnumber':item['disc']})
    li.setArt(item.get('art') or {})
    li.setProperty('IsPlayable','true')
    for key in ('rank','plays','baselineplays','liveplays','lastplayed'):
        li.setProperty('playhistory.'+key,str(item.get(key,'')))
    li.setProperty('playhistory.lastplayeddate',compact_last_played_date(item.get('lastplayed')))
    li.addContextMenuItems([('Queue item','QueueMedia(%s)' % item['path'])])
    return li

def album_item(item):
    li=xbmcgui.ListItem(label=item['album'],label2=item['artist'])
    li.setInfo('music',{'album':item['album'],'artist':item['artist'],'year':item['year']})
    art=dict(item.get('art') or {})
    if not art.get('thumb'):
        art['thumb']=art.get('album.thumb','')
    li.setArt(art)
    for key in ('rank','score','plays','coverage','playedtracks','trackcount','lastplayed'):
        li.setProperty('playhistory.'+key,str(item.get(key,'')))
    li.setProperty('playhistory.lastplayeddate',compact_last_played_date(item.get('lastplayed')))
    score = item.get('score', '')
    try:
        score = str(int(Decimal(str(score)).quantize(Decimal('1'), rounding=ROUND_HALF_UP)))
    except (TypeError, ValueError, ArithmeticError):
        score = str(score)
    li.setProperty('playhistory.scoredisplay', score)
    album_url='musicdb://albums/%s/' % item['albumid'] if item.get('albumid') else ''
    if album_url:
        li.addContextMenuItems([
            ('Play album','PlayMedia(%s)' % album_url),
            ('Queue album','QueueMedia(%s)' % album_url)])
    return li

def compact_artist_metadata(artist):
    """Return local Kodi art and KMM-equivalent derived genre for one artist."""
    if not artist:
        return {}
    request = {
        'jsonrpc': '2.0', 'id': 'playhistory-artist-widget',
        'method': 'AudioLibrary.GetArtists',
        'params': {
            'albumartistsonly': True,
            'properties': ['art'],
            'filter': {'field': 'artist', 'operator': 'is', 'value': artist},
            'limits': {'start': 0, 'end': 2},
        },
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
        artists = response.get('result', {}).get('artists') or []
    except Exception as exc:
        xbmc.log('[%s] compact artist metadata lookup failed: %s' % (ADDON_ID, exc), xbmc.LOGDEBUG)
        return {}
    exact = [row for row in artists
             if str(row.get('artist') or '').casefold() == str(artist).casefold()]
    if len(exact) != 1:
        return {}
    row = exact[0]
    genre_request = {
        'jsonrpc': '2.0', 'id': 'playhistory-artist-widget-genre',
        'method': 'AudioLibrary.GetAlbums',
        'params': {
            'properties': ['artist', 'genre'],
            'filter': {'field': 'albumartist', 'operator': 'is', 'value': artist},
            'limits': {'start': 0, 'end': 1000},
        },
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(genre_request)))
        albums = response.get('result', {}).get('albums') or []
    except Exception as exc:
        xbmc.log('[%s] compact artist genre lookup failed: %s' % (ADDON_ID, exc), xbmc.LOGDEBUG)
        albums = []

    # Match KMM/KMM Client: one vote per valid album; blank or multi-genre
    # albums do not vote; alphabetically ordered tied leaders use " / ".
    votes = Counter()
    for album in albums:
        album_artists = album.get('artist') or []
        if not any(str(value or '').casefold() == str(artist).casefold()
                   for value in album_artists):
            continue
        values = {
            str(value or '').strip()
            for value in (album.get('genre') or [])
            if str(value or '').strip()
        }
        if len(values) == 1:
            votes[next(iter(values))] += 1
    genre = ''
    if votes:
        highest = max(votes.values())
        genre = ' / '.join(sorted(
            (value for value, count in votes.items() if count == highest),
            key=lambda value: (value.casefold(), value)))
    return {
        'thumb': (row.get('art') or {}).get('thumb') or '',
        'genre': genre,
    }

def artist_item(item, enrich=False):
    li=xbmcgui.ListItem(label=item['artist'],label2=str(item.get('plays', '')))
    li.setInfo('music',{'artist':item['artist']})
    if enrich:
        metadata = compact_artist_metadata(item['artist'])
        if metadata.get('thumb'):
            li.setArt({'thumb': metadata['thumb']})
        li.setProperty('playhistory.genre', metadata.get('genre', ''))
    for key in ('rank','plays','lastplayed'):
        li.setProperty('playhistory.'+key,str(item.get(key,'')))
    li.setProperty('playhistory.lastplayeddate',compact_last_played_date(item.get('lastplayed')))
    return li

def run():
    handle=int(sys.argv[1]); params=parse_qs(sys.argv[2][1:])
    if value(params,'maintenance') == 'refresh':
        if value(params,'confirm') != 'CATALOG_REFRESH_V1':
            xbmc.log('[%s] rejected catalog refresh request without confirmation token' % ADDON_ID, xbmc.LOGWARNING)
        else:
            database=HistoryDatabase(ADDON_ID, lambda message: xbmc.log('[%s] %s' % (ADDON_ID, message), xbmc.LOGINFO))
            try:
                database.initialize()
                database.request_catalog_refresh()
                xbmc.log('[%s] catalog refresh requested' % ADDON_ID, xbmc.LOGINFO)
            finally:
                database.close()
        xbmcplugin.endOfDirectory(handle, succeeded=True, cacheToDisc=False)
        return
    if value(params,'maintenance') == 'recover_catalog_generation':
        if value(params,'confirm') != 'RECOVER_SCHEMA7_DERIVED_V1':
            xbmc.log('[%s] rejected derived catalog recovery without confirmation token' % ADDON_ID, xbmc.LOGWARNING)
        else:
            try:
                generation_id=int(value(params,'generation_id'))
                database=HistoryDatabase(ADDON_ID, lambda message: xbmc.log('[%s] %s' % (ADDON_ID, message), xbmc.LOGINFO))
                try:
                    database.initialize()
                    database.recover_failed_catalog_generation(generation_id, configured_source_root(xbmcaddon.Addon(ADDON_ID)))
                    xbmc.log('[%s] recovered catalog generation %d derived state' % (ADDON_ID, generation_id), xbmc.LOGINFO)
                finally:
                    database.close()
            except Exception as exc:
                xbmc.log('[%s] derived catalog recovery failed: %s' % (ADDON_ID, exc), xbmc.LOGERROR)
        xbmcplugin.endOfDirectory(handle, succeeded=True, cacheToDisc=False)
        return
    if value(params,'maintenance') == 'revoke':
        replacement_id=value(params,'replacement_id')
        if value(params,'confirm') != 'REVOKE_REPLACEMENT_V1':
            xbmc.log('[%s] rejected revoke request without confirmation token' % ADDON_ID, xbmc.LOGWARNING)
        else:
            database=HistoryDatabase(ADDON_ID, lambda message: xbmc.log('[%s] %s' % (ADDON_ID, message), xbmc.LOGINFO))
            try:
                database.initialize()
                changed=InboxProcessor(database, ADDON_ID, lambda message: xbmc.log('[%s] %s' % (ADDON_ID, message), xbmc.LOGINFO)).revoke(replacement_id)
                xbmc.log('[%s] revoke request %s for %s' % (ADDON_ID, 'applied' if changed else 'already applied', replacement_id), xbmc.LOGINFO)
            except ManifestError as exc:
                xbmc.log('[%s] revoke request rejected: %s' % (ADDON_ID, exc), xbmc.LOGWARNING)
            finally:
                database.close()
        xbmcplugin.endOfDirectory(handle, succeeded=True, cacheToDisc=False)
        return
    if value(params,'maintenance') == 'fresh_history_reset':
        if value(params,'confirm') != 'RESET_SCHEMA7_FRESH_HISTORY_V1':
            xbmc.log('[%s] rejected fresh-history reset request without confirmation token' % ADDON_ID, xbmc.LOGWARNING)
        else:
            database=HistoryDatabase(ADDON_ID, lambda message: xbmc.log('[%s] %s' % (ADDON_ID, message), xbmc.LOGINFO))
            try:
                database.initialize()
                database.reset_fresh_history('RESET_SCHEMA7_FRESH_HISTORY_V1')
                xbmc.log('[%s] fresh Schema-7 play history reset applied' % ADDON_ID, xbmc.LOGINFO)
            finally:
                database.close()
        xbmcplugin.endOfDirectory(handle, succeeded=True, cacheToDisc=False)
        return
    view=value(params,'view','tracks')
    if view not in ('tracks','albums','artists'): view='tracks'
    try: limit=int(value(params,'limit','50'))
    except ValueError: limit=50
    reader=CacheReader(ADDON_ID)
    try: rows=reader.rows(view,limit)
    finally: reader.close()
    for item in rows:
        if view=='tracks':
            xbmcplugin.addDirectoryItem(handle,item['path'],track_item(item),False)
        elif view=='albums':
            url='musicdb://albums/%s/' % item['albumid'] if item.get('albumid') else ''
            xbmcplugin.addDirectoryItem(handle,url,album_item(item),bool(url))
        else:
            xbmcplugin.addDirectoryItem(handle,'',artist_item(item, enrich=(limit <= 5)),False)
    xbmcplugin.endOfDirectory(handle, cacheToDisc=False)

if __name__=='__main__': run()
