# -*- coding: utf-8 -*-
"""Current-session only qualifying-play tracker."""
from __future__ import absolute_import

import json
import time
import uuid

import xbmc


SAMPLE_SECONDS = 5.0
MAX_SAMPLE_GAP_SECONDS = 12.0
SEEK_TOLERANCE_SECONDS = 0.25
PENDING_START_TIMEOUT_SECONDS = 30.0
ALBUM_NATURAL_END_GRACE_SECONDS = 2.0


def _call(obj, names, default=None):
    for name in names:
        method = getattr(obj, name, None)
        if method:
            try:
                return method()
            except Exception:
                pass
    return default


class PlaybackTracker(xbmc.Player):
    def __init__(self, database, logger, clock=None):
        xbmc.Player.__init__(self)
        self.database = database
        self.log = logger
        self.clock = clock or time.monotonic
        self.session = None
        self.pending_start = None
        self._expired_pending_file = None
        self._last_tick = 0.0
        self._recovery_skip_key = None
        self.album_occurrence = None
        self.pending_album_end = None

    def onAVStarted(self):
        self._start_session("av_started")

    def onPlayBackStarted(self):
        self._start_session("playback_started")

    def onPlayBackStopped(self):
        self._clear_pending("playback stopped")
        self._finish_session("stopped")
        self._end_album_occurrence("stopped")

    def onPlayBackEnded(self):
        self._clear_pending("playback ended")
        self._finish_session("ended", natural_end=True)
        # Kodi can emit Ended between queued tracks.  Keep only this live
        # handoff pending briefly; a following start resolves the continuity.
        self._schedule_natural_album_end()

    def onPlayBackError(self):
        self._clear_pending("playback error")
        self._finish_session("error")
        self._end_album_occurrence("error")

    def onPlayBackPaused(self):
        """Anchor the active session so paused wall time is never credited."""
        if self.session:
            self._sample(self.clock(), final=True)
            self.session["paused"] = True

    def onPlayBackResumed(self):
        """Start a new observed interval after a pause, without inferring it."""
        if self.session:
            now = self.clock()
            self.session["paused"] = False
            self.session["last_wall"] = now
            self.session["last_position"] = self._position()

    def onPlayBackSeek(self, timeSeek, seekOffset):
        """A seek makes an otherwise unobserved transition tail unsafe to infer."""
        if self.session:
            self.session["seek_seen"] = True
            self.session["last_wall"] = self.clock()
            try:
                self.session["last_position"] = max(0.0, float(timeSeek))
            except (TypeError, ValueError):
                self.session["last_position"] = self._position()

    def tick(self):
        now = self.clock()
        self._finalize_pending_album_end(now)
        if now - self._last_tick < SAMPLE_SECONDS:
            return
        self._last_tick = now
        had_pending = bool(self.pending_start)
        self._retry_pending_start(now)
        if not self.session and not self.pending_start and not had_pending and self.isPlayingAudio():
            self._recover_session()
        elif self.session and self.isPlayingAudio():
            self._recover_track_transition()
        if self.session and self.isPlayingAudio():
            # Kodi JSON-RPC is deliberately kept out of player callbacks.  A
            # missing-metadata SMB item is resolved, at most once, from the
            # regular service tick after its session has been tied to this
            # exact current file.
            self._recover_bounded_library_metadata(self.session)
            self._ensure_metadata_album_membership(self.session)
            self._sample(now, final=False)

    def shutdown(self):
        self._clear_pending("service shutdown")
        self._finish_session("service_shutdown")
        self._end_album_occurrence("service_shutdown")

    def _schedule_natural_album_end(self):
        if self.album_occurrence:
            self.pending_album_end = {'occurrence': self.album_occurrence,
                                      'due_at': self.clock() + ALBUM_NATURAL_END_GRACE_SECONDS}

    def _finalize_pending_album_end(self, now):
        pending = self.pending_album_end
        if pending and now >= pending['due_at']:
            if self.album_occurrence and self.album_occurrence.get('id') == pending['occurrence'].get('id'):
                self._end_album_occurrence('natural_end')
            self.pending_album_end = None

    def _end_album_occurrence(self, reason):
        occurrence = self.album_occurrence
        self.pending_album_end = None
        self.album_occurrence = None
        if occurrence:
            if occurrence.get('metadata_album_key'):
                self.database.end_metadata_album_occurrence(occurrence.get('id'))
            else:
                self.database.end_album_occurrence(occurrence.get('id'))
            self.log('album occurrence ended (%s)' % reason, xbmc.LOGDEBUG)

    def _transition_album_occurrence(self, metadata):
        if self.database.schema_version() >= 13:
            # A new Music -> Files item can initially expose only its concrete
            # file path and duration.  That is not an album transition: retain
            # any valid current occurrence until the same session has resolved
            # usable metadata through the normal catalog/Kodi fallback path.
            # This prevents a transient blank context from fragmenting a
            # consecutive recovered album into one-track occurrences.
            if not self._has_usable_album_context(metadata):
                return self.album_occurrence
            metadata_album_key = self.database.metadata_album_context(metadata)
            current = self.album_occurrence
            self.pending_album_end = None
            if current and current.get('metadata_album_key') == metadata_album_key:
                return current
            if current:
                self._end_album_occurrence('album_transition')
            if not metadata_album_key:
                return None
            current = {'id': uuid.uuid4().hex, 'metadata_album_key': metadata_album_key,
                       'started_at': int(time.time()), 'membership_attempt_at': 0.0}
            self.album_occurrence = current
            return current
        logical_album_key = self.database.resolve_current_album_membership(metadata)
        current = self.album_occurrence
        self.pending_album_end = None
        if current and current.get('logical_album_key') == logical_album_key:
            return current
        if current:
            self._end_album_occurrence('album_transition')
        if not logical_album_key:
            return None
        current = {'id': uuid.uuid4().hex, 'logical_album_key': logical_album_key,
                   'started_at': int(time.time())}
        self.album_occurrence = current
        return current

    def _has_usable_album_context(self, metadata):
        """Return true only when Schema-13 album context is meaningful."""
        return (self._usable_metadata_value('artist', metadata.get('artist')) and
                self._usable_metadata_value('album', metadata.get('album')) and
                self._usable_metadata_value('title', metadata.get('title')))

    def _ensure_metadata_album_membership(self, session):
        """Fetch only the active Kodi album, outside player callbacks.

        A failed/unsupported lookup simply leaves album occurrence unqualified;
        it never manipulates playback and retries at most once per 30 seconds.
        """
        occurrence = session.get('album_occurrence')
        metadata = session.get('metadata') or {}
        if not occurrence or not occurrence.get('metadata_album_key'):
            return
        now = self.clock()
        if not self.database.metadata_album_membership_needs_refresh(metadata):
            return
        if now - float(occurrence.get('membership_attempt_at') or 0) < 30.0:
            return
        occurrence['membership_attempt_at'] = now
        songid = metadata.get('kodi_dbid')
        albumid = self._kodi_album_id_for_song(songid)
        if albumid is None:
            # Artist/title can be complete while Kodi's MusicInfoTag carries a
            # stale post-library-rebuild song DBID.  Reuse the already bounded
            # exact-current-file recovery only after GetSongDetails has proved
            # that ID unusable.  It is one attempt per active session and
            # retains its exact-path and active-file race protections.
            if not self._recover_bounded_library_metadata(session, stale_dbid=True):
                self.log('metadata album membership unavailable: Kodi song id unusable', xbmc.LOGDEBUG)
                return
            metadata = session.get('metadata') or {}
            occurrence = session.get('album_occurrence')
            if not occurrence or not occurrence.get('metadata_album_key'):
                return
            albumid = self._kodi_album_id_for_song(metadata.get('kodi_dbid'))
            if albumid is None:
                self.log('metadata album membership unavailable after exact recovery', xbmc.LOGDEBUG)
                return
        try:
            songs_request = {'jsonrpc':'2.0','id':'playhistory-album-membership',
                             'method':'AudioLibrary.GetSongs',
                             'params':{'filter':{'albumid':int(albumid)},
                                       'properties':['title','artist','albumartist','album','track','disc','year','file','art']}}
            result = json.loads(xbmc.executeJSONRPC(json.dumps(songs_request))).get('result', {})
            songs = result.get('songs') or []
            rows = []
            for song in songs:
                rows.append({'title':song.get('title') or song.get('label'),
                             'artist':' / '.join(song.get('artist') or []),
                             'album_artist':' / '.join(song.get('albumartist') or []),
                             'album':song.get('album'),'track':song.get('track'),'disc':song.get('disc'),
                             'year':song.get('year'),'file':song.get('file'),
                             'artwork_json':json.dumps(song.get('art') or {})})
            stored = self.database.replace_metadata_album_membership(metadata, rows, int(albumid))
            self.log('metadata album membership refreshed: albumid=%s songs=%d slots=%d' % (albumid, len(songs), stored), xbmc.LOGDEBUG)
        except Exception as exc:
            self.log('metadata album membership lookup failed: %s' % exc, xbmc.LOGWARNING)

    def _kodi_album_id_for_song(self, songid):
        """Return a current Kodi album ID only for one usable song DBID."""
        try:
            songid = int(songid)
            if songid < 0:
                return None
        except (TypeError, ValueError):
            return None
        try:
            detail_request = {'jsonrpc':'2.0','id':'playhistory-album-membership',
                              'method':'AudioLibrary.GetSongDetails',
                              'params':{'songid':songid,'properties':['albumid']}}
            detail = json.loads(xbmc.executeJSONRPC(json.dumps(detail_request))).get('result', {}).get('songdetails', {})
            albumid = detail.get('albumid')
            return int(albumid) if albumid is not None and int(albumid) >= 0 else None
        except (TypeError, ValueError):
            return None
        except Exception as exc:
            self.log('metadata album membership song lookup failed: %s' % exc, xbmc.LOGDEBUG)
            return None

    def _recover_session(self):
        """Recover only a missed start callback; never infer prior listening."""
        metadata = self._metadata()
        self._start_session("tick_recovery", metadata)

    def _retry_pending_start(self, now):
        pending = self.pending_start
        if not pending:
            return
        metadata = self._metadata()
        file_name = metadata.get("file") or ""
        if file_name and file_name != pending["file"]:
            self.log("pending playback start replaced: current file changed", xbmc.LOGDEBUG)
            self.pending_start = None
            self._start_session("pending_file_change", metadata)
            return
        if now - pending["first_seen"] >= PENDING_START_TIMEOUT_SECONDS:
            self.log("pending playback start expired: duration unavailable", xbmc.LOGWARNING)
            self._expired_pending_file = pending["file"]
            self.pending_start = None
            return
        if file_name == pending["file"] and self.isPlayingAudio():
            pending["last_retry"] = now
            self._start_session("pending_start_recovery", metadata)

    def _set_pending_start(self, file_name, now):
        if self._expired_pending_file == file_name:
            return
        if self._expired_pending_file and self._expired_pending_file != file_name:
            self._expired_pending_file = None
        pending = self.pending_start
        if pending and pending["file"] == file_name:
            pending["last_retry"] = now
            return
        if pending:
            self.log("pending playback start replaced: current file changed", xbmc.LOGDEBUG)
        self.pending_start = {"file": file_name, "first_seen": now, "last_retry": now}
        self.log("pending playback start created: duration unavailable", xbmc.LOGDEBUG)

    def _clear_pending(self, reason):
        if self.pending_start:
            self.pending_start = None
        self._expired_pending_file = None

    def _recover_track_transition(self):
        """Handle a second missed callback when Kodi has moved to another file."""
        metadata = self._metadata()
        file_name = metadata.get("file") or ""
        if file_name and file_name != self.session.get("file"):
            self._start_session("tick_recovery_track_transition", metadata)

    def _start_session(self, reason, metadata=None):
        metadata = metadata or self._metadata()
        file_name = metadata.get("file")
        now = self.clock()
        if not file_name:
            return
        if not self.isPlayingAudio():
            if file_name and reason in ("av_started", "playback_started"):
                self._set_pending_start(file_name, now)
            return
        if self.session and self.session["file"] == file_name:
            if reason == "tick_recovery":
                self.log("tick recovery suppressed: session already active", xbmc.LOGDEBUG)
            return  # paired callbacks or a callback after tick recovery
        if self.session:
            # The current Player position now belongs to the new track, so never
            # use it to calculate a final interval for the previous session.
            self._finish_session("track_transition", sample_current=False)
        bounded_metadata_recovery = False
        if self._needs_catalog_identity_fallback(metadata):
            state, catalog = self._catalog_metadata(file_name)
            if state == 'exact':
                metadata = self._merge_catalog_metadata(metadata, catalog)
                self.log("metadata fallback: exact catalog path match artist=%r title=%r album=%r" %
                         (catalog.get('artist'), catalog.get('title'), catalog.get('album')), xbmc.LOGDEBUG)
            elif state == 'ambiguous':
                self.log("metadata fallback skipped: ambiguous catalog path match", xbmc.LOGDEBUG)
            else:
                self.log("metadata fallback skipped: no catalog path match", xbmc.LOGDEBUG)
                # Generation-scoped catalog state is intentionally not kept in
                # sync with every Kodi scan.  If it has no exact candidate for
                # this active, library-backed non-musicdb file, defer one
                # strictly exact Kodi lookup to tick().
                bounded_metadata_recovery = self._is_bounded_library_file(file_name)
        duration = float(metadata.get("duration") or 0)
        if duration <= 0:
            # Kodi exposes the duration of the exact active playback stream
            # without requiring native-library membership or an SMB tag read.
            duration = self._playing_duration()
            if duration > 0:
                metadata = dict(metadata)
                metadata["duration"] = duration
        if duration <= 0:
            self._set_pending_start(file_name, now)
            return
        self._expired_pending_file = None
        self.pending_start = None
        threshold = min(duration * 0.80, 240.0)
        album_occurrence = self._transition_album_occurrence(metadata)
        self.session = {"id": uuid.uuid4().hex, "file": file_name, "metadata": metadata,
                         "started": now, "last_wall": now, "last_position": self._position(),
                         "listened": 0.0, "duration": duration, "threshold": threshold,
                         "qualified": False, "finishing": False, "seek_seen": False, "paused": False,
                         "album_occurrence": album_occurrence,
                         "bounded_metadata_recovery_pending": bounded_metadata_recovery,
                         "bounded_metadata_recovery_attempted": False}
        if reason == "pending_start_recovery":
            self.log("pending playback start resolved: %s" % (metadata.get("title") or file_name), xbmc.LOGDEBUG)
        self.log("session started (%s): %s; threshold %.1fs" % (reason, metadata.get("title") or file_name, threshold))

    def _needs_catalog_identity_fallback(self, metadata):
        return (not self._usable_metadata_value("artist", metadata.get("artist")) or
                not self._usable_metadata_value("title", metadata.get("title")))

    @staticmethod
    def _usable_metadata_value(field, value):
        if field in ("duration", "track", "kodi_dbid"):
            try:
                return float(value or 0) > 0
            except (TypeError, ValueError):
                return False
        if field == "disc":
            return value not in (None, "")
        return bool(str(value or "").strip())

    def _merge_catalog_metadata(self, metadata, catalog):
        merged = dict(metadata)
        for field, value in catalog.items():
            if not self._usable_metadata_value(field, merged.get(field)) and self._usable_metadata_value(field, value):
                merged[field] = value
        return merged

    def _catalog_metadata(self, file_name):
        """Use the database's read-only in-memory current-catalog path index."""
        try:
            return self.database.catalog_metadata_for_playback_path(file_name)
        except Exception as exc:
            self.log("catalog metadata lookup failed: %s" % exc, xbmc.LOGDEBUG)
            return 'error', {}

    @staticmethod
    def _is_bounded_library_file(file_name):
        """Return true only for a concrete non-musicdb file path."""
        value = str(file_name or "").replace("\\", "/")
        lowered = value.casefold()
        return bool(value and not lowered.startswith("musicdb://") and
                    (lowered.startswith("smb://") or lowered.startswith("nfs://") or
                     lowered.startswith("file://") or lowered.startswith("/")))

    @staticmethod
    def _exact_path_key(file_name):
        """Canonicalize only case and slash representation; never fuzzy-match."""
        return str(file_name or "").replace("\\", "/").casefold()

    def _recover_bounded_library_metadata(self, session, stale_dbid=False):
        """Resolve one active SMB/local library file outside player callbacks.

        The pending marker is created only after player metadata and the trusted
        exact-path catalog have both failed.  One tick attempt per session keeps
        this from becoming a retrying per-callback library query.
        """
        if (not session or session.get("bounded_metadata_recovery_attempted") or
                (not session.get("bounded_metadata_recovery_pending") and not stale_dbid)):
            return False
        session["bounded_metadata_recovery_attempted"] = True
        requested_file = session.get("file") or ""
        if not self._is_bounded_library_file(requested_file):
            return False
        try:
            current_file = self.getPlayingFile()
        except Exception as exc:
            self.log("bounded metadata fallback skipped: active file unavailable: %s" % exc, xbmc.LOGDEBUG)
            return False
        if self._exact_path_key(current_file) != self._exact_path_key(requested_file):
            self.log("bounded metadata fallback skipped: active file changed", xbmc.LOGDEBUG)
            return False
        state, resolved = self._exact_library_file_metadata(requested_file)
        if state != "exact":
            self.log("bounded metadata fallback skipped: %s Kodi exact-file match" % state, xbmc.LOGDEBUG)
            return False
        merged = self._merge_catalog_metadata(session.get("metadata") or {}, resolved)
        if stale_dbid:
            # The explicit stale-ID trigger is the sole case where a positive
            # current-session DBID may be replaced.  The replacement comes only
            # from this unique exact active-file Kodi result.
            merged["kodi_dbid"] = resolved.get("kodi_dbid")
        if self._needs_catalog_identity_fallback(merged):
            self.log("bounded metadata fallback skipped: Kodi match lacked usable artist/title", xbmc.LOGDEBUG)
            return False
        # Check the exact active item again before applying a result.  A queued
        # transition must never let Track A populate Track B's session.
        try:
            if self._exact_path_key(self.getPlayingFile()) != self._exact_path_key(requested_file):
                self.log("bounded metadata fallback discarded: active file changed", xbmc.LOGDEBUG)
                return False
        except Exception:
            return False
        session["metadata"] = merged
        session["bounded_metadata_recovery_pending"] = False
        session["album_occurrence"] = self._transition_album_occurrence(merged)
        self.log("bounded metadata fallback: exact Kodi file match artist=%r title=%r album=%r" %
                 (merged.get("artist"), merged.get("title"), merged.get("album")), xbmc.LOGDEBUG)
        return True

    def _exact_library_file_metadata(self, file_name):
        """Return metadata only when Kodi has exactly one matching full path."""
        value = str(file_name or "").replace("\\", "/")
        if "/" not in value:
            return "missing", {}
        directory, filename = value.rsplit("/", 1)
        if not directory or not filename:
            return "missing", {}
        directory += "/"
        request = {"jsonrpc": "2.0", "id": "playhistory-exact-active-file",
                   "method": "AudioLibrary.GetSongs",
                   "params": {"filter": {"and": [
                       {"field": "path", "operator": "is", "value": directory},
                       {"field": "filename", "operator": "is", "value": filename}]},
                       "properties": ["title", "artist", "albumartist", "album", "track", "disc", "file"],
                       "limits": {"start": 0, "end": 2}}}
        try:
            result = json.loads(xbmc.executeJSONRPC(json.dumps(request))).get("result", {})
            songs = result.get("songs") or []
        except Exception as exc:
            self.log("bounded metadata fallback lookup failed: %s" % exc, xbmc.LOGWARNING)
            return "error", {}
        exact = [song for song in songs
                 if self._exact_path_key(song.get("file")) == self._exact_path_key(file_name)]
        if not exact:
            return "missing", {}
        if len(exact) != 1 or len(songs) != 1:
            return "ambiguous", {}
        song = exact[0]
        return "exact", {"file": song.get("file") or file_name,
                         "title": song.get("title") or song.get("label") or "",
                         "artist": " / ".join(song.get("artist") or []),
                         "album_artist": " / ".join(song.get("albumartist") or []),
                         "album": song.get("album") or "",
                         "track": song.get("track") or 0,
                         "disc": song.get("disc"),
                         "kodi_dbid": song.get("songid")}

    def _finish_session(self, reason, sample_current=True, natural_end=False):
        session = self.session
        if not session or session["finishing"]:
            return
        session["finishing"] = True
        if sample_current and not natural_end:
            # Kodi can mark playback inactive before this callback. getTime() is
            # still worth trying: it captures the final genuine interval when the
            # position remains available, and safely adds nothing if it does not.
            self._sample(self.clock(), final=True)
        elif reason == "track_transition":
            # At a queued transition the Player's position already belongs to
            # the incoming item.  The outgoing item cannot be sampled again.
            # Credit only the bounded monotonic interval since its last known
            # position, and only while Kodi reported no pause or seek.  This
            # makes a real short-track completion independent of tick phase
            # without turning an early Next or a seek into a qualifying play.
            self._credit_transition_tail()
        if natural_end and not session["qualified"] and not session["seek_seen"]:
            # Kodi can reset getTime() before its natural-end callback. Credit
            # only the unsampled tail that elapsed in real time, and only when
            # it is no larger than one bounded sampling interval.
            remaining = max(0.0, session["duration"] - session["last_position"])
            elapsed = max(0.0, self.clock() - session["last_wall"])
            if remaining and remaining <= MAX_SAMPLE_GAP_SECONDS and elapsed + SEEK_TOLERANCE_SECONDS >= remaining:
                session["listened"] += remaining
                self.log("natural end tail credited: %.1fs" % remaining)
                if session["listened"] >= session["threshold"]:
                    self._qualify()
        if not session["qualified"]:
            self.log("session %s before qualification (%.1f/%.1fs)" % (reason, session["listened"], session["threshold"]))
        self.session = None

    def _credit_transition_tail(self):
        session = self.session
        if (not session or session["qualified"] or session.get("seek_seen") or
                session.get("paused")):
            return
        elapsed = max(0.0, self.clock() - session["last_wall"])
        if elapsed > MAX_SAMPLE_GAP_SECONDS:
            self.log("transition tail not credited: unobserved gap %.1fs" % elapsed, xbmc.LOGDEBUG)
            return
        remaining = max(0.0, session["duration"] - session["last_position"])
        credited = min(elapsed, remaining)
        if credited <= 0:
            return
        session["listened"] += credited
        session["last_wall"] += credited
        session["last_position"] += credited
        self.log("transition tail credited: %.1fs" % credited, xbmc.LOGDEBUG)
        if session["listened"] >= session["threshold"]:
            self._qualify()

    def _sample(self, now, final):
        session = self.session
        if not session or session["qualified"] or session.get("paused"):
            return
        position = self._position()
        wall_gap = max(0.0, min(now - session["last_wall"], MAX_SAMPLE_GAP_SECONDS))
        position_gap = position - session["last_position"]
        if position_gap < -SEEK_TOLERANCE_SECONDS or position_gap > wall_gap + SEEK_TOLERANCE_SECONDS:
            # Both directions reset the anchor. Forward skips receive no credit; prior listening remains.
            self.log("seek detected; preserving %.1fs accumulated listening" % session["listened"])
            session["seek_seen"] = True
        elif position_gap > 0:
            session["listened"] += min(position_gap, wall_gap)
        session["last_wall"] = now
        session["last_position"] = position
        if session["listened"] >= session["threshold"]:
            self._qualify()

    def _qualify(self):
        session = self.session
        if not session or session["qualified"]:
            return
        session["qualified"] = True  # set before DB work to guard callbacks/races
        metadata = self._persistent_metadata(session["metadata"])
        prepared = self.database.targeted_metadata_for_playback(metadata.get("file"))
        if prepared.get("state") == "pending":
            # Never store a provisional file-path identity for a successor that
            # is already being prepared. Retain the qualified session until
            # Kodi's targeted scan has supplied release-specific evidence.
            self.database.defer_qualifying_play(session["id"], prepared["replacement_id"], metadata,
                                                session["listened"], session["threshold"])
            self.log("qualifying play deferred pending targeted successor verification")
            return
        if prepared.get("state") == "verified":
            resolved = dict(metadata)
            resolved.update({key: value for key, value in prepared["metadata"].items() if value not in (None, "")})
            metadata = resolved
        committed = self.database.record_live_play(session["id"], metadata, session["listened"], session["threshold"],
                                                   album_occurrence=session.get("album_occurrence"))
        if committed:
            self.log("qualifying play committed: %s (%.1fs)" % (session["metadata"].get("title") or session["file"], session["listened"]))
        else:
            self.log("duplicate play event prevented for session %s" % session["id"], xbmc.LOGWARNING)

    def _position(self):
        try:
            return max(0.0, float(self.getTime()))
        except Exception:
            return 0.0

    def _playing_duration(self):
        """Read the current stream duration only; never control playback."""
        try:
            return max(0.0, float(self.getTotalTime()))
        except Exception:
            return 0.0

    def _metadata(self):
        # Kodi can briefly report audio activity while it has no current file
        # during a normal queued-track handoff.  That is recoverable: leave
        # acquisition to the next callback/tick instead of killing the service.
        try:
            item = self.getPlayingItem()
            file_name = self.getPlayingFile()
        except Exception as exc:
            if "not playing any file" not in str(exc).casefold():
                raise
            self.log("playback metadata temporarily unavailable during transition", xbmc.LOGDEBUG)
            return {}
        tag = _call(item, ["getMusicInfoTag"])
        artist = _call(tag, ["getArtist"], "")
        if isinstance(artist, (list, tuple)):
            artist = " / ".join(artist)
        return {"file": file_name, "title": _call(tag, ["getTitle"], ""),
                "artist": artist, "album_artist": _call(tag, ["getAlbumArtist"], ""),
                "album": _call(tag, ["getAlbum"], ""), "track": _call(tag, ["getTrack"], 0),
                "disc": _call(tag, ["getDisc"], 1), "duration": _call(tag, ["getDuration"], 0),
                "year": _call(tag, ["getYear"], ""), "kodi_dbid": _call(tag, ["getDbId", "getDBID"], None),
                "musicbrainz_recording_id": _call(tag, ["getMusicBrainzTrackID", "getMusicBrainzRecordingID"], ""),
                "musicbrainz_release_id": _call(tag, ["getMusicBrainzAlbumID", "getMusicBrainzReleaseID"], "")}

    def _persistent_metadata(self, metadata):
        """Resolve Kodi's transient musicdb URI only once, at commit time."""
        file_name = metadata.get("file") or ""
        if not file_name.casefold().startswith("musicdb://"):
            return metadata
        resolved = dict(metadata)
        try:
            request = {"jsonrpc": "2.0", "id": "playhistory-identity",
                       "method": "AudioLibrary.GetSongDetails",
                       "params": {"songid": int(metadata.get("kodi_dbid")),
                                  "properties": ["file", "title", "artist", "albumartist", "album", "track", "disc", "year", "musicbrainztrackid", "musicbrainzalbumid"]}}
            response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
            details = response.get("result", {}).get("songdetails", {})
            if details.get("file"):
                resolved.update({"file": details.get("file"), "title": details.get("title") or metadata.get("title"),
                                 "artist": " / ".join(details.get("artist") or []) or metadata.get("artist"),
                                 "album_artist": " / ".join(details.get("albumartist") or []) or metadata.get("album_artist"),
                                 "album": details.get("album") or metadata.get("album"), "track": details.get("track") or metadata.get("track"),
                                 "disc": details.get("disc") or metadata.get("disc"), "year": details.get("year") or metadata.get("year"),
                                 "musicbrainz_recording_id": details.get("musicbrainztrackid") or metadata.get("musicbrainz_recording_id"),
                                 "musicbrainz_release_id": details.get("musicbrainzalbumid") or metadata.get("musicbrainz_release_id")})
                return resolved
        except Exception as exc:
            self.log("persistent identity lookup failed: %s" % exc, xbmc.LOGWARNING)
        # A transient Kodi URI is never stored as a path identity. The approved
        # conservative artist/album/disc/track/title composite remains safe.
        resolved["file"] = ""
        self.log("persistent identity fell back to conservative composite", xbmc.LOGWARNING)
        return resolved
