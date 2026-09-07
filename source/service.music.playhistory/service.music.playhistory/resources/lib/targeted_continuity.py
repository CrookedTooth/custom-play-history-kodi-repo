# -*- coding: utf-8 -*-
"""Low-frequency, manifest-qualified successor preparation.

This module deliberately does not treat a folder path as continuity evidence.
The path only selects one accepted manifest for Kodi's own directory scanner;
identity, fingerprint, and mapping proof is taken from AudioLibrary afterwards.
"""
from __future__ import absolute_import

import json
import time

from .continuity import InboxProcessor, ManifestError, catalog_fingerprint, canonical_uuid, normalized_fingerprint_text
from .identity import album_identity, track_identity
from .source_root import common_album_path, normalize_relative_path, relative_path


SCAN_POLL_SECONDS = 5
SCAN_TIMEOUT_SECONDS = 180
RETRY_DELAY_SECONDS = 300
FOLDER_OBSERVE_SECONDS = 2

SONG_PROPERTIES = ['album', 'albumartist', 'artist', 'disc', 'duration', 'file',
                   'musicbrainzalbumid', 'musicbrainztrackid', 'track', 'year']


def _join(value):
    return ' / '.join(value or []) if isinstance(value, (list, tuple)) else (value or '')


def _directory(path):
    value = (path or '').replace('\\', '/').rstrip('/')
    return value.rsplit('/', 1)[0] + '/' if '/' in value else ''


def _path_equal(left, right):
    return normalize_relative_path(left).casefold() == normalize_relative_path(right).casefold()


def rows_from_targeted_songs(songs):
    """Convert one AudioLibrary directory result to existing catalog row shape."""
    rows = []
    for song in songs:
        rows.append({
            'kodi_dbid': song.get('songid'),
            'kodi_album_dbid': song.get('albumid'),
            'title': song.get('title') or song.get('label'),
            'artist': _join(song.get('artist')),
            'album_artist': _join(song.get('albumartist')),
            'album': song.get('album'),
            'track': song.get('track'),
            'disc': song.get('disc'),
            'year': song.get('year'),
            'duration_ms': int(float(song.get('duration') or 0) * 1000) or None,
            'file': song.get('file'),
            'musicbrainz_recording_id': song.get('musicbrainztrackid'),
            'musicbrainz_release_id': song.get('musicbrainzalbumid'),
        })
    return rows


class TargetedContinuityManager(object):
    """Runs only for an already accepted pending/suspended manifest candidate."""
    def __init__(self, database, inbox, source_root, rpc, logger, clock=None):
        self.database = database
        self.inbox = inbox
        self.source_root = source_root
        self.rpc = rpc
        self.log = logger
        self.clock = clock or time.time
        self._last_folder = None
        self._last_folder_observed = 0

    def observe_folder(self, folder_path):
        """Consider an active Music Files folder at most once per two seconds."""
        now = int(self.clock())
        if not folder_path or (folder_path == self._last_folder and now - self._last_folder_observed < FOLDER_OBSERVE_SECONDS):
            return None
        self._last_folder, self._last_folder_observed = folder_path, now
        relative = relative_path(folder_path, self.source_root.get('physical_source_root'))
        if not relative:
            return None
        relative = normalize_relative_path(relative)
        candidates = self._matching_manifests(relative)
        if len(candidates) != 1:
            return None
        return self._request_scan(candidates[0], folder_path, relative, now)

    def on_notification(self, method):
        """Kodi's AudioLibrary.OnScanFinished is the preferred completion signal."""
        if method != 'AudioLibrary.OnScanFinished':
            return
        now = int(self.clock())
        with self.database.connection:
            self.database.connection.execute("UPDATE targeted_successor_observation SET state='scanning',scan_completed_at=? WHERE state IN ('requested','scanning')", (now,))

    def tick(self):
        """Bounded fallback polling for one outstanding directory scan only."""
        now = int(self.clock())
        rows = self.database.connection.execute("SELECT * FROM targeted_successor_observation WHERE state IN ('requested','scanning') ORDER BY requested_at LIMIT 1").fetchall()
        for job in rows:
            if now - int(job['requested_at']) > SCAN_TIMEOUT_SECONDS:
                self._fail(job['replacement_id'], 'Targeted AudioLibrary scan did not complete before timeout.', now)
                continue
            completed = job['scan_completed_at']
            if completed is None and now - int(job['requested_at']) < SCAN_POLL_SECONDS:
                continue
            self._verify_job(job, now)

    def _matching_manifests(self, relative_folder):
        candidates = []
        rows = self.database.connection.execute("SELECT replacement_id,raw_manifest_json,lifecycle_state FROM manifest_import WHERE receipt_state='accepted' AND lifecycle_state IN ('pending_catalog_validation','suspended')").fetchall()
        for row in rows:
            try:
                manifest, unused, unused_digest = self.inbox.validate_raw(bytes(row['raw_manifest_json']))
                successor = manifest['successor']
                if successor['source_root_id'] != self.source_root.get('source_root_id'):
                    continue
                if _path_equal(successor['library_relative_path'], relative_folder):
                    candidates.append((row['replacement_id'], manifest))
            except ManifestError:
                continue
        return candidates

    def _request_scan(self, candidate, folder_path, relative_folder, now):
        replacement_id, manifest = candidate
        row = self.database.connection.execute("SELECT state,retry_after FROM targeted_successor_observation WHERE replacement_id=?", (replacement_id,)).fetchone()
        if row and row['state'] in ('requested', 'scanning', 'verified'):
            return row['state']
        if row and row['retry_after'] and int(row['retry_after']) > now:
            return 'retry_later'
        with self.database.connection:
            self.database.connection.execute("""INSERT INTO targeted_successor_observation(replacement_id,state,scan_directory,source_root_id,expected_path,requested_at,retry_after)
                VALUES(?,?,?,?,?,?,NULL)
                ON CONFLICT(replacement_id) DO UPDATE SET state='requested',scan_directory=excluded.scan_directory,
                source_root_id=excluded.source_root_id,expected_path=excluded.expected_path,requested_at=excluded.requested_at,
                scan_completed_at=NULL,verified_at=NULL,retry_after=NULL,error_message=NULL,evidence_json=NULL""",
                (replacement_id, 'requested', folder_path, self.source_root['source_root_id'], relative_folder, now))
        # The ACK means Kodi accepted scheduling; it is not treated as scan completion.
        result = self.rpc('AudioLibrary.Scan', {'directory': folder_path, 'showdialogs': False})
        if result is None:
            self._fail(replacement_id, 'Kodi did not accept the targeted AudioLibrary scan request.', now)
            return 'failed'
        self.log('targeted continuity scan requested for %s' % replacement_id)
        return 'requested'

    def _verify_job(self, job, now):
        response = self.rpc('AudioLibrary.GetSongs', {
            'properties': SONG_PROPERTIES,
            'filter': {'field': 'path', 'operator': 'is', 'value': job['scan_directory']},
            'limits': {'start': 0, 'end': 500},
        }) or {}
        songs = response.get('songs') or []
        if not songs:
            return
        try:
            manifest, unused, unused_digest = self.inbox.manifest_for(job['replacement_id'])
            verified = self._verify_manifest_successor(manifest, job, rows_from_targeted_songs(songs))
            self.database.persist_targeted_successor_observation(job['replacement_id'], job, verified, now)
            # This is a narrow manifest reconciliation after immutable targeted
            # proof; it does not create a catalog generation or scan anything.
            self.inbox.reconcile_pending(job['replacement_id'])
            self.log('targeted continuity successor verified for %s' % job['replacement_id'])
        except ManifestError as exc:
            self._fail(job['replacement_id'], str(exc), now)

    def _verify_manifest_successor(self, manifest, job, rows):
        successor = manifest['successor']
        if not rows:
            raise ManifestError('Targeted scan returned no successor tracks.')
        relative_files = [relative_path(row.get('file'), self.source_root['physical_source_root']) for row in rows]
        if not all(relative_files):
            raise ManifestError('Targeted successor is outside the configured source root.')
        expected = common_album_path(relative_files)
        if expected is None or not _path_equal(expected, successor['library_relative_path']):
            raise ManifestError('Targeted successor path does not match the manifest expected path.')
        releases = set(canonical_uuid(row.get('musicbrainz_release_id')) for row in rows)
        if releases != set([successor['musicbrainz_release_id']]):
            raise ManifestError('Targeted successor Release ID is missing or inconsistent.')
        first = rows[0]
        if (normalized_fingerprint_text(first.get('album_artist') or first.get('artist')) != normalized_fingerprint_text(successor['album_artist']) or
                normalized_fingerprint_text(first.get('album')) != normalized_fingerprint_text(successor['album_title']) or
                int(first.get('year') or 0) != int(successor['original_year']) or len(rows) != int(successor['track_count'])):
            raise ManifestError('Targeted successor album evidence does not match the manifest.')
        fingerprint = catalog_fingerprint(first.get('album_artist') or first.get('artist'), first.get('album'), successor['musicbrainz_release_id'], int(first.get('year') or 0), rows)
        if fingerprint != successor['catalog_fingerprint']:
            raise ManifestError('Targeted successor catalog fingerprint mismatch.')
        album_key, unused_kind = album_identity(first)
        if album_key != 'mb-release:' + successor['musicbrainz_release_id']:
            raise ManifestError('Targeted successor did not produce the expected release identity.')
        by_position = {}
        for row in rows:
            key = (row.get('disc'), row.get('track'))
            if key in by_position:
                raise ManifestError('Targeted successor has ambiguous disc/track positions.')
            by_position[key] = row
        predecessor_generation_id = self.inbox.trusted_predecessor_generation(manifest)
        predecessor_album_key = 'mb-release:' + manifest['predecessor']['musicbrainz_release_id']
        mappings = []
        for mapping in manifest['track_mappings']:
            descriptor = mapping['successor']
            row = by_position.get((descriptor['disc'], descriptor['track']))
            if row is None:
                raise ManifestError('A mapped successor track is not uniquely present after targeted scan.')
            if canonical_uuid(row.get('musicbrainz_recording_id')) != descriptor['musicbrainz_recording_id']:
                raise ManifestError('Targeted successor Recording ID mapping mismatch.')
            track_key, unused_kind = track_identity(row)
            predecessor_rows = self.database.connection.execute("SELECT track_key FROM catalog_track_snapshot WHERE generation_id=? AND album_key=? AND disc IS ? AND track_number IS ?", (predecessor_generation_id, predecessor_album_key, mapping['predecessor']['disc'], mapping['predecessor']['track'])).fetchall()
            if len(predecessor_rows) != 1:
                raise ManifestError('A mapped predecessor track is not uniquely present in trusted catalog evidence.')
            mappings.append({'predecessor_track_key': predecessor_rows[0]['track_key'],
                             'successor_track_key': track_key, 'mapping': mapping})
        if len(mappings) != len(manifest['track_mappings']):
            raise ManifestError('Targeted successor mapping count mismatch.')
        tracks = []
        for row in rows:
            track_key, unused_kind = track_identity(row)
            tracks.append(dict(row, track_key=track_key))
        return {'album_key': album_key, 'release_id': successor['musicbrainz_release_id'], 'fingerprint': fingerprint,
                'expected_path': expected, 'current_path': first.get('file'), 'kodi_album_dbid': first.get('kodi_album_dbid'),
                'predecessor_generation_id': predecessor_generation_id, 'tracks': tracks, 'mappings': mappings,
                'evidence': {'verification_method': 'targeted_audio_library_scan', 'scan_directory': job['scan_directory'],
                             'source_root_id': self.source_root['source_root_id'], 'expected_path': expected,
                             'current_path': first.get('file'), 'successor_release_id': successor['musicbrainz_release_id'],
                             'catalog_fingerprint': fingerprint, 'artist': first.get('album_artist') or first.get('artist'),
                             'title': first.get('album'), 'original_year': int(first.get('year') or 0), 'track_count': len(rows),
                             'mappings': mappings, 'tracks': tracks}}

    def _fail(self, replacement_id, message, now):
        with self.database.connection:
            self.database.connection.execute("UPDATE targeted_successor_observation SET state='failed',retry_after=?,error_message=? WHERE replacement_id=?", (now + RETRY_DELAY_SECONDS, message, replacement_id))
        self.log('targeted continuity preparation failed for %s: %s' % (replacement_id, message))
