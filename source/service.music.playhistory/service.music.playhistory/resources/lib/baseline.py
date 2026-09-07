# -*- coding: utf-8 -*-
"""Explicit, read-only Kodi playcount preview. It is never run by the service."""
from __future__ import absolute_import

import json

import xbmc

from .identity import track_identity


class BaselinePreview(object):
    def __init__(self, rows):
        self.rows = rows

    @classmethod
    def from_kodi(cls, logger):
        rows = []
        start = 0
        while True:
            request = {"jsonrpc": "2.0", "id": "playhistory-preview", "method": "AudioLibrary.GetSongs",
                       "params": {"properties": ["album", "artist", "albumartist", "track", "disc", "duration", "year", "file", "playcount", "musicbrainztrackid", "musicbrainzalbumid"], "limits": {"start": start, "end": start + 500}}}
            response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
            result = response.get("result", {})
            batch = result.get("songs", [])
            rows.extend([cls._to_row(song) for song in batch if int(song.get("playcount") or 0) > 0])
            limits = result.get("limits", {})
            if limits.get("end", 0) >= limits.get("total", 0) or not batch:
                break
            start = limits.get("end", start + len(batch))
        logger("baseline preview read %d played Kodi songs; no data written" % len(rows))
        return cls(rows)

    @staticmethod
    def _to_row(song):
        return {"kodi_dbid": song.get("songid"), "title": song.get("title"), "artist": " / ".join(song.get("artist") or []),
                "album_artist": " / ".join(song.get("albumartist") or []), "album": song.get("album"), "track": song.get("track"), "disc": song.get("disc"), "duration": song.get("duration"), "year": song.get("year"), "file": song.get("file"), "playcount": song.get("playcount"), "musicbrainz_recording_id": song.get("musicbrainztrackid"), "musicbrainz_release_id": song.get("musicbrainzalbumid")}

    def summary(self):
        analysis = self.analysis()
        return {"played_song_rows": len(self.rows), "total_kodi_playcounts": sum(int(row.get("playcount") or 0) for row in self.rows), "unique_library_track_identities": analysis["unique_identities"], "identity_collisions_requiring_review": len(analysis["collisions"]), "unsafe_identity_rows": len(analysis["unsafe_rows"]), "would_import_only_after_explicit_approval": True}

    def analysis(self):
        """Preview-only collision classification for an explicit import review."""
        seen = {}
        unsafe = []
        for row in self.rows:
            key, kind = track_identity(row)
            seen.setdefault(key, []).append(row)
            if kind == "conservative_composite" and not (row.get("title") and (row.get("artist") or row.get("album"))):
                unsafe.append(row)
        collisions = []
        for key, rows in seen.items():
            if len(rows) > 1:
                files = set(row.get("file") or "" for row in rows)
                albums = set(row.get("album") or "" for row in rows)
                reason = "same_library_item_duplicate" if len(files) == 1 else "distinct_release_or_file_instances"
                collisions.append({"identity_key": key, "reason": reason, "albums": sorted(albums), "files": sorted(files), "rows": rows})
        return {"unique_identities": len(seen), "collisions": collisions, "unsafe_rows": unsafe}
