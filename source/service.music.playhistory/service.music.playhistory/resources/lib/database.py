# -*- coding: utf-8 -*-
"""Small migration-managed SQLite store; no Kodi-library scans occur here."""
from __future__ import absolute_import

import json
import os
import sqlite3
import time

import xbmcvfs

from .identity import album_identity, logical_album_identity, logical_song_identity, metadata_album_identity, metadata_artist_identity, metadata_track_identity, normalized_path, normalized_text, recording_identity, track_identity
from .continuity import catalog_fingerprint
from .source_root import common_album_path, relative_path
from .maintenance import CatalogRefreshAborted


SCHEMA_VERSION = 13


class HistoryDatabase(object):
    def __init__(self, addon_id, logger, path=None):
        self.addon_id = addon_id
        self.log = logger
        self.path = path
        self.connection = None
        self._catalog_path_index = {}

    def initialize(self):
        if not self.path:
            directory = xbmcvfs.translatePath("special://profile/addon_data/%s/" % self.addon_id)
            if not xbmcvfs.exists(directory):
                xbmcvfs.mkdirs(directory)
            self.path = os.path.join(directory, "playhistory.db")
        # This is captured before sqlite can create the file.  It is the only
        # approved entry to direct current-schema initialization: an existing
        # file, even one with no schema marker, remains on the guarded legacy
        # migration path.
        fresh_database = not os.path.exists(self.path)
        self.connection = sqlite3.connect(self.path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self._migrate(fresh_database=fresh_database)
        self._rebuild_catalog_path_index()

    def close(self):
        if self.connection:
            self.connection.close()
            self.connection = None

    def _rebuild_catalog_path_index(self):
        """Atomically replace the local current-catalog exact-path index.

        This runs only at database initialization and after a successful catalog
        replacement. Playback performs one dictionary lookup against the last
        complete catalog; it never waits for catalog work or touches Kodi/SMB.
        """
        index = {}
        generation = self.authoritative_catalog_generation_id() if self.connection and self.schema_version() >= 12 else None
        if generation is None:
            rows = self.connection.execute("""SELECT DISTINCT c.track_key,t.duration_ms,t.artist,t.title,t.album,
                a.artist AS album_artist,t.track_number,t.disc,t.recording_id,t.release_id,c.kodi_song_dbid,t.canonical_path
                FROM album_track_catalog c JOIN track_identity t ON t.identity_key=c.track_key
                LEFT JOIN album_identity a ON a.album_key=c.album_key""").fetchall()
        else:
            rows = self.connection.execute("""SELECT DISTINCT c.track_key,t.duration_ms,t.artist,t.title,t.album,
                a.artist AS album_artist,t.track_number,t.disc,t.recording_id,t.release_id,c.kodi_song_dbid,c.current_path AS canonical_path
                FROM catalog_track_snapshot c JOIN track_identity t ON t.identity_key=c.track_key
                LEFT JOIN album_identity a ON a.album_key=c.album_key WHERE c.generation_id=?""",
                (generation,)).fetchall()
        for row in rows:
            path = normalized_path(row['canonical_path'])
            if not path:
                continue
            index.setdefault(path, []).append({
                'track_key': row['track_key'],
                'duration': (float(row['duration_ms']) / 1000.0) if row['duration_ms'] else 0.0,
                'artist': row['artist'], 'title': row['title'], 'album': row['album'],
                'album_artist': row['album_artist'], 'track': row['track_number'],
                'disc': row['disc'], 'musicbrainz_recording_id': row['recording_id'],
                'musicbrainz_release_id': row['release_id'], 'kodi_dbid': row['kodi_song_dbid']})
        # A track can occur in more than one current album row. That remains
        # one deterministic physical candidate, not an ambiguity.
        self._catalog_path_index = {
            path: tuple({candidate['track_key']: candidate for candidate in candidates}.values())
            for path, candidates in index.items()}

    def catalog_metadata_for_playback_path(self, file_name):
        """Return ``(state, metadata)`` for one normalized exact catalog path.

        ``state`` is explicit so callers can log a safe failure without choosing
        between multiple physical catalog candidates.
        """
        path = normalized_path(file_name)
        if not path:
            return 'missing', {}
        candidates = self._catalog_path_index.get(path, ())
        if not candidates:
            return 'missing', {}
        if len(candidates) != 1:
            return 'ambiguous', {}
        return 'exact', dict(candidates[0])

    def resolve_authoritative_logical_song_for_live_event(self, track_key, file_name, generation_id=None):
        """Resolve an existing scoped logical key; never create catalog state.

        Direct physical identity is primary.  Only a direct miss may use the
        already-built exact-path index, and that path must identify exactly one
        eligible authoritative catalog candidate.
        """
        generation = generation_id if generation_id is not None else self.authoritative_catalog_generation_id()
        if generation is None:
            return None
        row = self.connection.execute("""SELECT logical_song_key FROM catalog_logical_projection
            WHERE generation_id=? AND track_key=? AND eligibility='eligible'""",
            (generation, track_key)).fetchone()
        if row is not None:
            return row['logical_song_key']
        state, candidate = self.catalog_metadata_for_playback_path(file_name)
        candidate_key = candidate.get('track_key') if state == 'exact' else None
        if not candidate_key:
            return None
        row = self.connection.execute("""SELECT logical_song_key FROM catalog_logical_projection
            WHERE generation_id=? AND track_key=? AND eligibility='eligible'""",
            (generation, candidate_key)).fetchone()
        return row['logical_song_key'] if row is not None else None

    def schema_version(self):
        row = self.connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        return int(row[0]) if row else 0

    def _migrate(self, fresh_database=False):
        self.connection.execute("CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        current = self.schema_version()
        if current > SCHEMA_VERSION:
            raise RuntimeError("database schema is newer than this add-on")
        if fresh_database and current == 0:
            self._initialize_fresh_schema13()
            return
        if current < 1:
            self._migration_1()
            self.connection.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', '1')")
            self.connection.commit()
            current = 1
        if current < 2:
            self._migration_2()
            self.connection.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', '2')")
            self.connection.commit()
            current = 2
        if current < 3:
            self._migration_3()
            self.connection.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', '3')")
            self.connection.commit()
            current = 3
        if current < 4:
            self._migration_4()
            self.connection.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', '4')")
            self.connection.commit()
            current = 4
        if current < 5:
            raise RuntimeError("schema 4 requires the explicit Identity v2 migration")
        if current < 6:
            self._migration_6()
            self.connection.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', '6')")
            self.connection.commit()
            current = 6
        if current < 7:
            self._migration_7()
            self.connection.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', '7')")
            self.connection.commit()
            current = 7
        if current < 8:
            self._migration_8()
            current = 8
        if current < 9:
            self._migration_9()
            current = 9
        if current < 10:
            self._migration_10()
            current = 10
        if current < 11:
            self._migration_11()
            current = 11
        if current < 12:
            self._migration_12()
            current = 12
        if current < 13:
            self._migration_13()
            current = 13
        if current != SCHEMA_VERSION:
            raise RuntimeError("incomplete schema migration")

    def _set_schema_version(self, version):
        self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)", (str(version),))

    def _create_schema12_objects(self):
        """Create Schema 12 structural tables without copying a catalog.

        A new installation has no trusted catalog generation to materialise.
        The tables remain empty until normal bounded runtime work creates the
        first catalog state; no library walk is started here.
        """
        statements = """
CREATE TABLE IF NOT EXISTS catalog_logical_song (
 generation_id INTEGER NOT NULL, logical_song_key TEXT NOT NULL,
 normalized_artist TEXT NOT NULL, normalized_title TEXT NOT NULL,
 collision_discriminator TEXT, display_artist TEXT NOT NULL, display_title TEXT NOT NULL,
 preferred_track_key TEXT, preferred_path TEXT, preferred_artwork_json TEXT, updated_at INTEGER NOT NULL,
 PRIMARY KEY(generation_id,logical_song_key),
 FOREIGN KEY(generation_id) REFERENCES catalog_generation(generation_id));
CREATE TABLE IF NOT EXISTS catalog_logical_projection (
 generation_id INTEGER NOT NULL, track_key TEXT NOT NULL, logical_song_key TEXT,
 eligibility TEXT NOT NULL, exclusion_reason TEXT, projected_at INTEGER NOT NULL,
 PRIMARY KEY(generation_id,track_key),
 FOREIGN KEY(generation_id) REFERENCES catalog_generation(generation_id));
CREATE TABLE IF NOT EXISTS catalog_logical_totals (
 generation_id INTEGER NOT NULL, logical_song_key TEXT NOT NULL,
 imported_plays INTEGER NOT NULL DEFAULT 0, observed_plays INTEGER NOT NULL DEFAULT 0,
 total_plays INTEGER NOT NULL DEFAULT 0, last_played_at INTEGER,
 PRIMARY KEY(generation_id,logical_song_key),
 FOREIGN KEY(generation_id) REFERENCES catalog_generation(generation_id));
CREATE TABLE IF NOT EXISTS catalog_logical_album (
 generation_id INTEGER NOT NULL, logical_album_key TEXT NOT NULL,
 source_root_id TEXT, artist_folder TEXT, album_folder TEXT, artist TEXT, title TEXT,
 year TEXT, kodi_album_dbid INTEGER, artwork_json TEXT, updated_at INTEGER NOT NULL,
 PRIMARY KEY(generation_id,logical_album_key),
 FOREIGN KEY(generation_id) REFERENCES catalog_generation(generation_id));
CREATE TABLE IF NOT EXISTS catalog_logical_album_song (
 generation_id INTEGER NOT NULL, logical_album_key TEXT NOT NULL, logical_song_key TEXT NOT NULL,
 PRIMARY KEY(generation_id,logical_album_key,logical_song_key));
CREATE TABLE IF NOT EXISTS catalog_logical_album_track (
 generation_id INTEGER NOT NULL, logical_album_key TEXT NOT NULL, track_key TEXT NOT NULL,
 PRIMARY KEY(generation_id,logical_album_key,track_key));
CREATE INDEX IF NOT EXISTS catalog_logical_projection_song_idx ON catalog_logical_projection(generation_id,logical_song_key);
CREATE INDEX IF NOT EXISTS catalog_logical_album_song_idx ON catalog_logical_album_song(generation_id,logical_song_key);
CREATE INDEX IF NOT EXISTS catalog_logical_album_track_idx ON catalog_logical_album_track(generation_id,track_key);
""".strip().split(';\n')
        for statement in statements:
            if statement.strip():
                self.connection.execute(statement)
        self._verify_schema12_objects()

    def _initialize_fresh_schema13(self):
        """Build an empty, complete Schema 13 database for a proven new file."""
        with self.connection:
            self._migration_1(); self._set_schema_version(1)
            self._migration_2(); self._set_schema_version(2)
            self._migration_3(); self._set_schema_version(3)
            self._migration_4(); self._set_schema_version(4)
            self.connection.execute("""CREATE TABLE identity_migration_audit (
                old_identity_key TEXT PRIMARY KEY,new_identity_key TEXT NOT NULL,
                migrated_at INTEGER NOT NULL,identity_version INTEGER NOT NULL,
                mapping_method TEXT NOT NULL, evidence_json TEXT)""")
            self.connection.execute("CREATE UNIQUE INDEX identity_migration_audit_new_idx ON identity_migration_audit(new_identity_key)")
            self._set_schema_version(5)
            self._migration_6(); self._set_schema_version(6)
            self._migration_7(); self._set_schema_version(7)
            self._migration_8()
            self._migration_9()
            self._migration_10()
            self._migration_11()
            self._create_schema12_objects(); self._set_schema_version(12)
        # Schema 13 deliberately begins its own IMMEDIATE transaction.
        self._migration_13(allow_empty_catalog=True)
        self._set_schema_version(SCHEMA_VERSION)
        self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('rankings_dirty','1')")
        self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('fresh_install_empty_catalog','1')")
        self.connection.commit()

    def _migration_1(self):
        statements = [
            """CREATE TABLE track_identity (
                identity_key TEXT PRIMARY KEY, identity_kind TEXT NOT NULL,
                recording_id TEXT, release_id TEXT, disc INTEGER, track_number INTEGER,
                canonical_path TEXT, artist TEXT, album TEXT, title TEXT,
                kodi_dbid_last_seen INTEGER, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""",
            """CREATE TABLE album_identity (
                album_key TEXT PRIMARY KEY, identity_kind TEXT NOT NULL, release_id TEXT,
                artist TEXT, title TEXT, year TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""",
            """CREATE TABLE album_track (
                album_key TEXT NOT NULL, track_key TEXT NOT NULL, disc INTEGER, track_number INTEGER,
                PRIMARY KEY (album_key, track_key),
                FOREIGN KEY(album_key) REFERENCES album_identity(album_key),
                FOREIGN KEY(track_key) REFERENCES track_identity(identity_key))""",
            """CREATE TABLE play_event (
                event_key TEXT PRIMARY KEY, track_key TEXT NOT NULL, album_key TEXT,
                source TEXT NOT NULL CHECK(source IN ('kodi_baseline_import','live_tracking')),
                play_count INTEGER NOT NULL CHECK(play_count > 0), listened_seconds REAL,
                threshold_seconds REAL, occurred_at INTEGER NOT NULL, session_key TEXT,
                FOREIGN KEY(track_key) REFERENCES track_identity(identity_key))""",
            """CREATE TABLE track_totals (
                track_key TEXT PRIMARY KEY, imported_plays INTEGER NOT NULL DEFAULT 0,
                observed_plays INTEGER NOT NULL DEFAULT 0, total_plays INTEGER NOT NULL DEFAULT 0,
                last_played_at INTEGER, FOREIGN KEY(track_key) REFERENCES track_identity(identity_key))""",
            """CREATE TABLE album_rank_cache (
                album_key TEXT PRIMARY KEY, rank_position INTEGER, score REAL, breadth_percent REAL,
                raw_plays INTEGER, distinct_tracks INTEGER, played_tracks INTEGER, updated_at INTEGER NOT NULL)""",
            """CREATE TABLE track_rank_cache (
                track_key TEXT PRIMARY KEY, rank_position INTEGER, total_plays INTEGER,
                updated_at INTEGER NOT NULL)""",
            "CREATE INDEX play_event_track_source_idx ON play_event(track_key, source)",
            "CREATE INDEX play_event_album_idx ON play_event(album_key)",
            "CREATE INDEX album_track_track_idx ON album_track(track_key)",
        ]
        for statement in statements:
            self.connection.execute(statement)

    def _migration_2(self):
        # Version 1 used MusicBrainz recording IDs as primary track keys. Keep
        # that information, but distinguish it from the playable release/file.
        self.connection.execute("ALTER TABLE track_identity ADD COLUMN recording_key TEXT")
        self.connection.execute("UPDATE track_identity SET recording_key='mb-recording:' || lower(recording_id) WHERE recording_id IS NOT NULL AND trim(recording_id) != ''")
        self.connection.execute("CREATE INDEX track_identity_recording_key_idx ON track_identity(recording_key)")

    def _migration_3(self):
        self.connection.execute("ALTER TABLE track_identity ADD COLUMN artwork_json TEXT")
        self.connection.execute("ALTER TABLE album_identity ADD COLUMN kodi_album_dbid INTEGER")
        self.connection.execute("ALTER TABLE album_identity ADD COLUMN artwork_json TEXT")
        self.connection.execute("ALTER TABLE track_rank_cache ADD COLUMN payload_json TEXT")
        self.connection.execute("ALTER TABLE album_rank_cache ADD COLUMN payload_json TEXT")
        self.connection.execute("""CREATE TABLE album_track_catalog (
            album_key TEXT NOT NULL, track_key TEXT NOT NULL, kodi_song_dbid INTEGER,
            disc INTEGER, track_number INTEGER, PRIMARY KEY(album_key, track_key))""")
        self.connection.execute("CREATE INDEX album_track_catalog_album_idx ON album_track_catalog(album_key)")
        self.connection.execute("CREATE INDEX album_track_catalog_track_idx ON album_track_catalog(track_key)")

    def _migration_4(self):
        """Add the inert replacement-continuity foundation.

        No existing event, total, cache, or identity is rewritten here.  Stage
        2 will be the first stage allowed to activate lineage.
        """
        statements = [
            "ALTER TABLE track_identity ADD COLUMN duration_ms INTEGER",
            """CREATE TABLE catalog_generation (
                generation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at INTEGER NOT NULL, completed_at INTEGER,
                state TEXT NOT NULL CHECK(state IN ('building','complete','failed','superseded')),
                source TEXT NOT NULL, notes TEXT)""",
            """CREATE TABLE catalog_album_snapshot (
                generation_id INTEGER NOT NULL, album_key TEXT NOT NULL,
                release_id TEXT, source_root_id TEXT, expected_path TEXT,
                current_path TEXT, catalog_fingerprint TEXT, track_count INTEGER NOT NULL,
                artist TEXT, title TEXT, original_year INTEGER,
                PRIMARY KEY(generation_id, album_key),
                FOREIGN KEY(generation_id) REFERENCES catalog_generation(generation_id))""",
            """CREATE TABLE catalog_track_snapshot (
                generation_id INTEGER NOT NULL, album_key TEXT NOT NULL, track_key TEXT NOT NULL,
                release_id TEXT, disc INTEGER, track_number INTEGER,
                recording_id TEXT, artist TEXT, title TEXT, duration_ms INTEGER,
                current_path TEXT, kodi_song_dbid INTEGER,
                PRIMARY KEY(generation_id, track_key),
                FOREIGN KEY(generation_id) REFERENCES catalog_generation(generation_id))""",
            """CREATE TABLE manifest_import (
                replacement_id TEXT PRIMARY KEY, manifest_sha256 TEXT NOT NULL,
                raw_manifest_json BLOB NOT NULL, imported_at INTEGER NOT NULL,
                receipt_state TEXT NOT NULL CHECK(receipt_state IN ('accepted','rejected','quarantined')),
                lifecycle_state TEXT NOT NULL CHECK(lifecycle_state IN ('pending_catalog_validation','active','suspended','rejected','revoked')),
                status_revision INTEGER NOT NULL, status_updated_at INTEGER NOT NULL,
                receipt_at INTEGER NOT NULL, resolved_path_mode TEXT,
                message TEXT NOT NULL)""",
            """CREATE TABLE album_replacement (
                replacement_id TEXT PRIMARY KEY, predecessor_album_key TEXT NOT NULL,
                successor_album_key TEXT NOT NULL,
                predecessor_release_id TEXT, successor_release_id TEXT,
                manifest_sha256 TEXT NOT NULL, imported_at INTEGER NOT NULL,
                approved_at INTEGER, lifecycle_state TEXT NOT NULL CHECK(lifecycle_state IN ('pending_catalog_validation','active','suspended','rejected','revoked')),
                catalog_verified_at INTEGER, suspended_at INTEGER, revoked_at INTEGER,
                audit_json TEXT,
                CHECK(predecessor_album_key != successor_album_key),
                FOREIGN KEY(replacement_id) REFERENCES manifest_import(replacement_id))""",
            """CREATE TABLE track_replacement (
                replacement_id TEXT NOT NULL, predecessor_track_key TEXT NOT NULL,
                successor_track_key TEXT NOT NULL, mapping_json TEXT NOT NULL,
                PRIMARY KEY(replacement_id, predecessor_track_key),
                UNIQUE(replacement_id, successor_track_key),
                CHECK(predecessor_track_key != successor_track_key),
                FOREIGN KEY(replacement_id) REFERENCES album_replacement(replacement_id))""",
            "CREATE INDEX catalog_album_snapshot_album_idx ON catalog_album_snapshot(album_key, generation_id)",
            "CREATE INDEX catalog_track_snapshot_track_idx ON catalog_track_snapshot(track_key, generation_id)",
            "CREATE INDEX manifest_import_hash_idx ON manifest_import(manifest_sha256)",
            "CREATE INDEX album_replacement_predecessor_idx ON album_replacement(predecessor_album_key)",
            "CREATE INDEX album_replacement_successor_idx ON album_replacement(successor_album_key)",
        ]
        for statement in statements:
            self.connection.execute(statement)

    def _migration_6(self):
        """Add immutable, narrowly scoped successor observations.

        A targeted observation is deliberately not a catalog generation: it
        records one manifest-qualified successor after Kodi's directory scan.
        Historical predecessor proof remains in catalog snapshots.
        """
        statements = [
            """CREATE TABLE targeted_successor_observation (
                replacement_id TEXT PRIMARY KEY,
                state TEXT NOT NULL CHECK(state IN ('requested','scanning','verified','failed')),
                scan_directory TEXT NOT NULL,
                source_root_id TEXT NOT NULL,
                expected_path TEXT NOT NULL,
                predecessor_generation_id INTEGER,
                successor_album_key TEXT,
                successor_release_id TEXT,
                catalog_fingerprint TEXT,
                current_path TEXT,
                kodi_album_dbid INTEGER,
                verification_method TEXT,
                requested_at INTEGER NOT NULL,
                scan_completed_at INTEGER,
                verified_at INTEGER,
                retry_after INTEGER,
                error_message TEXT,
                evidence_json TEXT,
                FOREIGN KEY(replacement_id) REFERENCES manifest_import(replacement_id),
                FOREIGN KEY(predecessor_generation_id) REFERENCES catalog_generation(generation_id))""",
            """CREATE TABLE targeted_successor_track (
                replacement_id TEXT NOT NULL,
                track_key TEXT NOT NULL,
                kodi_song_dbid INTEGER,
                current_path TEXT NOT NULL,
                release_id TEXT NOT NULL,
                recording_id TEXT,
                disc INTEGER,
                track_number INTEGER,
                artist TEXT,
                title TEXT,
                duration_ms INTEGER,
                PRIMARY KEY(replacement_id, track_key),
                UNIQUE(replacement_id, current_path),
                FOREIGN KEY(replacement_id) REFERENCES targeted_successor_observation(replacement_id),
                FOREIGN KEY(track_key) REFERENCES track_identity(identity_key))""",
            """CREATE TABLE targeted_successor_mapping (
                replacement_id TEXT NOT NULL,
                predecessor_track_key TEXT NOT NULL,
                successor_track_key TEXT NOT NULL,
                mapping_json TEXT NOT NULL,
                PRIMARY KEY(replacement_id, predecessor_track_key),
                UNIQUE(replacement_id, successor_track_key),
                FOREIGN KEY(replacement_id) REFERENCES targeted_successor_observation(replacement_id))""",
            """CREATE TABLE deferred_qualifying_play (
                session_key TEXT PRIMARY KEY,
                replacement_id TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                listened_seconds REAL NOT NULL,
                threshold_seconds REAL NOT NULL,
                occurred_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(replacement_id) REFERENCES targeted_successor_observation(replacement_id))""",
            "CREATE INDEX targeted_successor_observation_state_idx ON targeted_successor_observation(state, retry_after)",
            "CREATE INDEX targeted_successor_track_path_idx ON targeted_successor_track(current_path)",
            """CREATE TRIGGER targeted_successor_observation_verified_immutable
                BEFORE UPDATE ON targeted_successor_observation WHEN OLD.state='verified'
                BEGIN SELECT RAISE(ABORT, 'verified targeted observation is immutable'); END""",
            """CREATE TRIGGER targeted_successor_observation_verified_no_delete
                BEFORE DELETE ON targeted_successor_observation WHEN OLD.state='verified'
                BEGIN SELECT RAISE(ABORT, 'verified targeted observation is immutable'); END""",
            """CREATE TRIGGER targeted_successor_track_verified_immutable
                BEFORE UPDATE ON targeted_successor_track WHEN EXISTS(SELECT 1 FROM targeted_successor_observation o WHERE o.replacement_id=OLD.replacement_id AND o.state='verified')
                BEGIN SELECT RAISE(ABORT, 'verified targeted track evidence is immutable'); END""",
            """CREATE TRIGGER targeted_successor_track_verified_no_delete
                BEFORE DELETE ON targeted_successor_track WHEN EXISTS(SELECT 1 FROM targeted_successor_observation o WHERE o.replacement_id=OLD.replacement_id AND o.state='verified')
                BEGIN SELECT RAISE(ABORT, 'verified targeted track evidence is immutable'); END""",
            """CREATE TRIGGER targeted_successor_mapping_verified_immutable
                BEFORE UPDATE ON targeted_successor_mapping WHEN EXISTS(SELECT 1 FROM targeted_successor_observation o WHERE o.replacement_id=OLD.replacement_id AND o.state='verified')
                BEGIN SELECT RAISE(ABORT, 'verified targeted mapping evidence is immutable'); END""",
            """CREATE TRIGGER targeted_successor_mapping_verified_no_delete
                BEFORE DELETE ON targeted_successor_mapping WHEN EXISTS(SELECT 1 FROM targeted_successor_observation o WHERE o.replacement_id=OLD.replacement_id AND o.state='verified')
                BEGIN SELECT RAISE(ABORT, 'verified targeted mapping evidence is immutable'); END""",
        ]
        for statement in statements:
            self.connection.execute(statement)

    def _migration_7(self):
        """Add derived logical listening history without changing raw evidence."""
        statements = [
            """CREATE TABLE IF NOT EXISTS logical_song (
                logical_song_key TEXT PRIMARY KEY, normalized_artist TEXT NOT NULL,
                normalized_title TEXT NOT NULL, display_artist TEXT NOT NULL,
                display_title TEXT NOT NULL, preferred_track_key TEXT,
                preferred_path TEXT, preferred_artwork_json TEXT, updated_at INTEGER NOT NULL,
                UNIQUE(normalized_artist, normalized_title))""",
            """CREATE TABLE IF NOT EXISTS logical_song_projection (
                track_key TEXT PRIMARY KEY, logical_song_key TEXT,
                eligibility TEXT NOT NULL CHECK(eligibility IN ('eligible','excluded')),
                exclusion_reason TEXT,
                projected_at INTEGER NOT NULL,
                FOREIGN KEY(track_key) REFERENCES track_identity(identity_key),
                FOREIGN KEY(logical_song_key) REFERENCES logical_song(logical_song_key),
                CHECK((eligibility='eligible' AND logical_song_key IS NOT NULL AND exclusion_reason IS NULL) OR
                      (eligibility='excluded' AND logical_song_key IS NULL AND exclusion_reason IS NOT NULL)) )""",
            """CREATE TABLE IF NOT EXISTS logical_song_totals (
                logical_song_key TEXT PRIMARY KEY, imported_plays INTEGER NOT NULL DEFAULT 0,
                observed_plays INTEGER NOT NULL DEFAULT 0, total_plays INTEGER NOT NULL DEFAULT 0,
                last_played_at INTEGER, FOREIGN KEY(logical_song_key) REFERENCES logical_song(logical_song_key))""",
            """CREATE TABLE IF NOT EXISTS logical_album_current (
                logical_album_key TEXT PRIMARY KEY, source_root_id TEXT NOT NULL,
                artist_folder TEXT NOT NULL, album_folder TEXT NOT NULL,
                artist TEXT, title TEXT, year TEXT, kodi_album_dbid INTEGER, artwork_json TEXT,
                generation_id INTEGER, updated_at INTEGER NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS logical_album_current_song (
                logical_album_key TEXT NOT NULL, logical_song_key TEXT NOT NULL,
                PRIMARY KEY(logical_album_key, logical_song_key),
                FOREIGN KEY(logical_album_key) REFERENCES logical_album_current(logical_album_key),
                FOREIGN KEY(logical_song_key) REFERENCES logical_song(logical_song_key))""",
            "CREATE INDEX IF NOT EXISTS logical_song_projection_logical_idx ON logical_song_projection(logical_song_key)",
            "CREATE INDEX IF NOT EXISTS logical_album_current_song_song_idx ON logical_album_current_song(logical_song_key)",
        ]
        for statement in statements:
            self.connection.execute(statement)
        self._verify_schema7_objects()
        self.rebuild_logical_projection()

    def _migration_8(self):
        """Refine only proven artist/title recording collisions.

        Logical tables are wholly derived from immutable physical identities,
        catalog membership, and raw events.  Recreating the logical-song
        parent is consequently safe; raw events and all catalog/continuity
        evidence are deliberately left untouched.
        """
        with self.connection:
            self._verify_schema7_objects()
            self._clear_logical_album_current()
            self.connection.execute("DELETE FROM logical_song_totals")
            self.connection.execute("DELETE FROM logical_song_projection")
            self.connection.execute("DROP TABLE logical_song")
            self.connection.execute("""CREATE TABLE logical_song (
                logical_song_key TEXT PRIMARY KEY, normalized_artist TEXT NOT NULL,
                normalized_title TEXT NOT NULL, collision_discriminator TEXT NOT NULL DEFAULT '',
                display_artist TEXT NOT NULL, display_title TEXT NOT NULL,
                preferred_track_key TEXT, preferred_path TEXT, preferred_artwork_json TEXT,
                updated_at INTEGER NOT NULL,
                UNIQUE(normalized_artist, normalized_title, collision_discriminator))""")
            self.connection.execute("CREATE INDEX IF NOT EXISTS logical_song_base_idx ON logical_song(normalized_artist, normalized_title)")
            self._verify_schema8_objects()
            self._rebuild_logical_projection_contents()
            self.connection.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', '8')")

    def _migration_9(self):
        """Start album ranking from durable, prospective occurrences only.

        Schema 8 did not retain an album-listening session identifier, so its
        lifetime track totals cannot be converted into album plays safely.
        This migration preserves that authoritative track history and simply
        clears the obsolete derived album cache.
        """
        with self.connection:
            statements = [
                """CREATE TABLE IF NOT EXISTS logical_album_current_track (
                    logical_album_key TEXT NOT NULL, track_key TEXT NOT NULL,
                    PRIMARY KEY(logical_album_key, track_key),
                    FOREIGN KEY(logical_album_key) REFERENCES logical_album_current(logical_album_key),
                    FOREIGN KEY(track_key) REFERENCES track_identity(identity_key))""",
                """CREATE TABLE IF NOT EXISTS album_play_occurrence (
                    occurrence_id TEXT PRIMARY KEY, logical_album_key TEXT NOT NULL,
                    started_at INTEGER NOT NULL, last_activity_at INTEGER NOT NULL,
                    ended_at INTEGER, qualified_at INTEGER,
                    distinct_tracks INTEGER NOT NULL DEFAULT 0,
                    required_tracks INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('open','ended')),
                    FOREIGN KEY(logical_album_key) REFERENCES logical_album_current(logical_album_key))""",
                """CREATE TABLE IF NOT EXISTS album_play_occurrence_track (
                    occurrence_id TEXT NOT NULL, logical_song_key TEXT NOT NULL,
                    PRIMARY KEY(occurrence_id, logical_song_key),
                    FOREIGN KEY(occurrence_id) REFERENCES album_play_occurrence(occurrence_id),
                    FOREIGN KEY(logical_song_key) REFERENCES logical_song(logical_song_key))""",
                "CREATE INDEX IF NOT EXISTS logical_album_current_track_track_idx ON logical_album_current_track(track_key)",
                "CREATE INDEX IF NOT EXISTS track_identity_canonical_path_idx ON track_identity(canonical_path)",
                "CREATE INDEX IF NOT EXISTS album_play_occurrence_rank_idx ON album_play_occurrence(logical_album_key, qualified_at)",
            ]
            for statement in statements:
                self.connection.execute(statement)
            # Existing logical membership was derived from current catalog data.
            # Backfill only physical tracks whose old logical membership is unique;
            # leave any ambiguity out rather than guessing an album occurrence.
            self.connection.execute("""INSERT OR IGNORE INTO logical_album_current_track(logical_album_key,track_key)
                SELECT s.logical_album_key,p.track_key
                  FROM logical_song_projection p
                  JOIN logical_album_current_song s ON s.logical_song_key=p.logical_song_key
                 WHERE p.eligibility='eligible'
              GROUP BY p.track_key
                HAVING count(DISTINCT s.logical_album_key)=1""")
            self._verify_schema9_objects()
            self.connection.execute("DELETE FROM album_rank_cache")
            self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('rankings_dirty','1')")
            self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version','9')")

    def _migration_10(self):
        """Detach durable occurrence history from rebuildable current mapping.

        ``logical_album_current`` is intentionally cleared and regenerated at
        startup/catalog refresh.  The logical album key stored in an occurrence
        is durable evidence by itself and must not have a foreign-key parent in
        that derived table.
        """
        with self.connection:
            self.connection.execute("""CREATE TABLE album_play_occurrence_new (
                occurrence_id TEXT PRIMARY KEY, logical_album_key TEXT NOT NULL,
                started_at INTEGER NOT NULL, last_activity_at INTEGER NOT NULL,
                ended_at INTEGER, qualified_at INTEGER,
                distinct_tracks INTEGER NOT NULL DEFAULT 0,
                required_tracks INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('open','ended')))""")
            self.connection.execute("""CREATE TABLE album_play_occurrence_track_new (
                occurrence_id TEXT NOT NULL, logical_song_key TEXT NOT NULL,
                PRIMARY KEY(occurrence_id, logical_song_key),
                FOREIGN KEY(occurrence_id) REFERENCES album_play_occurrence_new(occurrence_id),
                FOREIGN KEY(logical_song_key) REFERENCES logical_song(logical_song_key))""")
            self.connection.execute("""INSERT INTO album_play_occurrence_new(
                occurrence_id,logical_album_key,started_at,last_activity_at,ended_at,qualified_at,
                distinct_tracks,required_tracks,state)
                SELECT occurrence_id,logical_album_key,started_at,last_activity_at,ended_at,qualified_at,
                       distinct_tracks,required_tracks,state FROM album_play_occurrence""")
            self.connection.execute("""INSERT INTO album_play_occurrence_track_new(occurrence_id,logical_song_key)
                SELECT occurrence_id,logical_song_key FROM album_play_occurrence_track""")
            self.connection.execute("DROP TABLE album_play_occurrence_track")
            self.connection.execute("DROP TABLE album_play_occurrence")
            self.connection.execute("ALTER TABLE album_play_occurrence_new RENAME TO album_play_occurrence")
            self.connection.execute("ALTER TABLE album_play_occurrence_track_new RENAME TO album_play_occurrence_track")
            self.connection.execute("CREATE INDEX album_play_occurrence_rank_idx ON album_play_occurrence(logical_album_key, qualified_at)")
            self._verify_schema10_objects()
            self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version','10')")

    def _migration_11(self):
        """Detach durable occurrence-track history from rebuildable logical songs.

        ``logical_song`` is deliberately deleted and regenerated whenever the
        logical projection is rebuilt.  An occurrence stores the durable
        logical-song key as historical evidence, so it cannot retain a
        foreign-key parent in that rebuildable table.
        """
        with self.connection:
            self.connection.execute("""CREATE TABLE album_play_occurrence_track_new (
                occurrence_id TEXT NOT NULL, logical_song_key TEXT NOT NULL,
                PRIMARY KEY(occurrence_id, logical_song_key),
                FOREIGN KEY(occurrence_id) REFERENCES album_play_occurrence(occurrence_id))""")
            self.connection.execute("""INSERT INTO album_play_occurrence_track_new(
                occurrence_id, logical_song_key)
                SELECT occurrence_id, logical_song_key FROM album_play_occurrence_track""")
            self.connection.execute("DROP TABLE album_play_occurrence_track")
            self.connection.execute("ALTER TABLE album_play_occurrence_track_new RENAME TO album_play_occurrence_track")
            self._verify_schema11_objects()
            self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version','11')")

    def _migration_12(self):
        """Add generation-owned derived catalog state without touching history.

        Schema 11 keeps these rebuildable tables global.  Schema 12 retains
        them for the running add-on while also materialising the authoritative
        generation's equivalent state in candidate-owned tables.  A later
        staged refresh can build these rows for a ``building`` generation and
        promote solely by changing generation metadata.
        """
        with self.connection:
            # SQLite does not automatically begin a transaction for DDL.  Begin
            # explicitly so table creation, copy validation, and schema-version
            # advancement are one guarded unit.
            self.connection.execute("BEGIN IMMEDIATE")
            statements = """
CREATE TABLE IF NOT EXISTS catalog_logical_song (
 generation_id INTEGER NOT NULL, logical_song_key TEXT NOT NULL,
 normalized_artist TEXT NOT NULL, normalized_title TEXT NOT NULL,
 collision_discriminator TEXT, display_artist TEXT NOT NULL, display_title TEXT NOT NULL,
 preferred_track_key TEXT, preferred_path TEXT, preferred_artwork_json TEXT, updated_at INTEGER NOT NULL,
 PRIMARY KEY(generation_id,logical_song_key),
 FOREIGN KEY(generation_id) REFERENCES catalog_generation(generation_id));
CREATE TABLE IF NOT EXISTS catalog_logical_projection (
 generation_id INTEGER NOT NULL, track_key TEXT NOT NULL, logical_song_key TEXT,
 eligibility TEXT NOT NULL, exclusion_reason TEXT, projected_at INTEGER NOT NULL,
 PRIMARY KEY(generation_id,track_key),
 FOREIGN KEY(generation_id) REFERENCES catalog_generation(generation_id));
CREATE TABLE IF NOT EXISTS catalog_logical_totals (
 generation_id INTEGER NOT NULL, logical_song_key TEXT NOT NULL,
 imported_plays INTEGER NOT NULL DEFAULT 0, observed_plays INTEGER NOT NULL DEFAULT 0,
 total_plays INTEGER NOT NULL DEFAULT 0, last_played_at INTEGER,
 PRIMARY KEY(generation_id,logical_song_key),
 FOREIGN KEY(generation_id) REFERENCES catalog_generation(generation_id));
CREATE TABLE IF NOT EXISTS catalog_logical_album (
 generation_id INTEGER NOT NULL, logical_album_key TEXT NOT NULL,
 source_root_id TEXT, artist_folder TEXT, album_folder TEXT, artist TEXT, title TEXT,
 year TEXT, kodi_album_dbid INTEGER, artwork_json TEXT, updated_at INTEGER NOT NULL,
 PRIMARY KEY(generation_id,logical_album_key),
 FOREIGN KEY(generation_id) REFERENCES catalog_generation(generation_id));
CREATE TABLE IF NOT EXISTS catalog_logical_album_song (
 generation_id INTEGER NOT NULL, logical_album_key TEXT NOT NULL, logical_song_key TEXT NOT NULL,
 PRIMARY KEY(generation_id,logical_album_key,logical_song_key));
CREATE TABLE IF NOT EXISTS catalog_logical_album_track (
 generation_id INTEGER NOT NULL, logical_album_key TEXT NOT NULL, track_key TEXT NOT NULL,
 PRIMARY KEY(generation_id,logical_album_key,track_key));
CREATE INDEX IF NOT EXISTS catalog_logical_projection_song_idx ON catalog_logical_projection(generation_id,logical_song_key);
CREATE INDEX IF NOT EXISTS catalog_logical_album_song_idx ON catalog_logical_album_song(generation_id,logical_song_key);
CREATE INDEX IF NOT EXISTS catalog_logical_album_track_idx ON catalog_logical_album_track(generation_id,track_key);
""".strip().split(';\n')
            # Do not use executescript here: Python's sqlite wrapper can issue an
            # implicit COMMIT before it runs a script, defeating the migration's
            # all-or-nothing failure guarantee.
            for statement in statements:
                if statement.strip():
                    self.connection.execute(statement)
            self._verify_schema12_objects()
            row = self.latest_current_catalog_generation()
            if row is None:
                raise RuntimeError('schema 12 requires a complete catalog generation')
            generation = row['generation_id']
            existing = self.connection.execute(
                "SELECT 1 FROM catalog_logical_projection WHERE generation_id=? LIMIT 1",
                (generation,)).fetchone()
            if existing is not None:
                raise RuntimeError('schema 12 authoritative generation is already materialised')
            self.connection.execute("INSERT INTO catalog_logical_song SELECT ?,logical_song_key,normalized_artist,normalized_title,collision_discriminator,display_artist,display_title,preferred_track_key,preferred_path,preferred_artwork_json,updated_at FROM logical_song", (generation,))
            self.connection.execute("INSERT INTO catalog_logical_projection SELECT ?,track_key,logical_song_key,eligibility,exclusion_reason,projected_at FROM logical_song_projection", (generation,))
            self.connection.execute("INSERT INTO catalog_logical_totals SELECT ?,logical_song_key,imported_plays,observed_plays,total_plays,last_played_at FROM logical_song_totals", (generation,))
            self.connection.execute("INSERT INTO catalog_logical_album SELECT ?,logical_album_key,source_root_id,artist_folder,album_folder,artist,title,year,kodi_album_dbid,artwork_json,updated_at FROM logical_album_current", (generation,))
            self.connection.execute("INSERT INTO catalog_logical_album_song SELECT ?,logical_album_key,logical_song_key FROM logical_album_current_song", (generation,))
            self.connection.execute("INSERT INTO catalog_logical_album_track SELECT ?,logical_album_key,track_key FROM logical_album_current_track", (generation,))
            source_tables = (
                ('logical_song', 'catalog_logical_song'),
                ('logical_song_projection', 'catalog_logical_projection'),
                ('logical_song_totals', 'catalog_logical_totals'),
                ('logical_album_current', 'catalog_logical_album'),
                ('logical_album_current_song', 'catalog_logical_album_song'),
                ('logical_album_current_track', 'catalog_logical_album_track'))
            for source, target in source_tables:
                before = self.connection.execute('SELECT count(*) FROM %s' % source).fetchone()[0]
                copied = self.connection.execute(
                    'SELECT count(*) FROM %s WHERE generation_id=?' % target,
                    (generation,)).fetchone()[0]
                if before != copied:
                    raise RuntimeError('schema 12 copy mismatch for %s' % source)
            self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version','12')")

    def _migration_13(self, allow_empty_catalog=False):
        """Add metadata-driven ranking state without changing raw history.

        Schema 12 tables remain intact during this first simplification pass.
        The new tables project immutable qualified events to conservative
        Artist/Album/Title identities and map qualified album occurrences to a
        metadata album where historical evidence is still available.
        """
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            statements = """
CREATE TABLE IF NOT EXISTS metadata_artist (
 artist_key TEXT PRIMARY KEY, normalized_artist TEXT NOT NULL, display_artist TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS metadata_album (
 album_key TEXT PRIMARY KEY, artist_key TEXT NOT NULL, normalized_artist TEXT NOT NULL,
 normalized_album TEXT NOT NULL, display_artist TEXT NOT NULL, display_album TEXT NOT NULL,
 preferred_artwork_json TEXT, kodi_album_dbid INTEGER, year INTEGER,
 UNIQUE(normalized_artist,normalized_album));
CREATE TABLE IF NOT EXISTS metadata_track (
 track_key TEXT PRIMARY KEY, artist_key TEXT NOT NULL, album_key TEXT NOT NULL,
 normalized_artist TEXT NOT NULL, normalized_album TEXT NOT NULL, normalized_title TEXT NOT NULL,
 slot_discriminator TEXT, display_artist TEXT NOT NULL, display_album TEXT NOT NULL,
 display_title TEXT NOT NULL, preferred_path TEXT, preferred_artwork_json TEXT,
 disc INTEGER, track_number INTEGER,
 UNIQUE(normalized_artist,normalized_album,normalized_title,slot_discriminator));
CREATE TABLE IF NOT EXISTS metadata_event_projection (
 event_key TEXT PRIMARY KEY, metadata_track_key TEXT, metadata_album_key TEXT,
 metadata_artist_key TEXT, play_count INTEGER NOT NULL, occurred_at INTEGER NOT NULL,
 exclusion_reason TEXT, FOREIGN KEY(event_key) REFERENCES play_event(event_key));
CREATE TABLE IF NOT EXISTS metadata_album_occurrence (
 occurrence_id TEXT PRIMARY KEY, metadata_album_key TEXT, qualified_at INTEGER,
 exclusion_reason TEXT);
CREATE TABLE IF NOT EXISTS metadata_album_membership (
 album_key TEXT NOT NULL, track_key TEXT NOT NULL, source_album_dbid INTEGER,
 refreshed_at INTEGER NOT NULL, PRIMARY KEY(album_key,track_key));
CREATE TABLE IF NOT EXISTS metadata_album_session (
 occurrence_id TEXT PRIMARY KEY, metadata_album_key TEXT NOT NULL, started_at INTEGER NOT NULL,
 last_activity_at INTEGER NOT NULL, ended_at INTEGER, qualified_at INTEGER,
 distinct_tracks INTEGER NOT NULL DEFAULT 0, required_tracks INTEGER NOT NULL, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS metadata_album_session_track (
 occurrence_id TEXT NOT NULL, track_key TEXT NOT NULL,
 PRIMARY KEY(occurrence_id,track_key), FOREIGN KEY(occurrence_id) REFERENCES metadata_album_session(occurrence_id));
CREATE TABLE IF NOT EXISTS artist_rank_cache (
 artist_key TEXT PRIMARY KEY, rank_position INTEGER NOT NULL, total_plays INTEGER NOT NULL,
 updated_at INTEGER NOT NULL, payload_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS metadata_event_track_idx ON metadata_event_projection(metadata_track_key);
CREATE INDEX IF NOT EXISTS metadata_event_album_idx ON metadata_event_projection(metadata_album_key);
CREATE INDEX IF NOT EXISTS metadata_event_artist_idx ON metadata_event_projection(metadata_artist_key);
CREATE INDEX IF NOT EXISTS metadata_occurrence_album_idx ON metadata_album_occurrence(metadata_album_key);
CREATE INDEX IF NOT EXISTS metadata_membership_album_idx ON metadata_album_membership(album_key);
CREATE INDEX IF NOT EXISTS metadata_session_album_idx ON metadata_album_session(metadata_album_key,qualified_at);
CREATE INDEX IF NOT EXISTS artist_rank_cache_rank_idx ON artist_rank_cache(rank_position);
""".strip().split(';\n')
            for statement in statements:
                if statement.strip():
                    self.connection.execute(statement)
            if allow_empty_catalog and self.latest_current_catalog_generation() is None:
                # A proven fresh install has no authoritative generation yet.
                # All metadata/ranking tables are intentionally empty.
                pass
            else:
                self._rebuild_metadata_rank_state()
            self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('rankings_dirty','1')")
            self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version','13')")

    def _metadata_slot_groups(self):
        """Return only primary metadata groups proven duplicated in one release."""
        groups = {}
        rows = self.connection.execute("SELECT artist,album,title,release_id,disc,track_number,identity_key FROM track_identity")
        for row in rows:
            primary, artist, album, title, unused = metadata_track_identity(
                row['artist'], row['album'], row['title'])
            if not primary:
                continue
            release = normalized_text(row['release_id']) or ('identity:' + row['identity_key'])
            groups.setdefault(primary, {}).setdefault(release, set()).add(
                (int(row['disc'] or 0), int(row['track_number'] or 0)))
        return {key for key, releases in groups.items()
                if any(len(slots) > 1 for slots in releases.values())}

    def _rebuild_metadata_rank_state(self):
        """Rebuild bounded metadata projections from immutable raw history only."""
        self.connection.execute("DELETE FROM metadata_album_occurrence")
        self.connection.execute("DELETE FROM metadata_event_projection")
        self.connection.execute("DELETE FROM metadata_track")
        self.connection.execute("DELETE FROM metadata_album")
        self.connection.execute("DELETE FROM metadata_artist")
        slot_groups = self._metadata_slot_groups()
        rows = self.connection.execute("""SELECT e.event_key,e.play_count,e.occurred_at,
                t.artist,t.album,t.title,t.disc,t.track_number,t.canonical_path,t.artwork_json,
                ai.artist AS album_artist,ai.artwork_json AS album_artwork,ai.kodi_album_dbid,ai.year
            FROM play_event e LEFT JOIN track_identity t ON t.identity_key=e.track_key
            LEFT JOIN album_identity ai ON ai.album_key=e.album_key ORDER BY e.event_key""").fetchall()
        for row in rows:
            primary, artist, album, title, unused = metadata_track_identity(
                row['artist'], row['album'], row['title'])
            artist_key, normalized_artist = metadata_artist_identity(row['artist'])
            album_key, normalized_album_artist, normalized_album = metadata_album_identity(
                row['album_artist'], row['artist'], row['album'])
            if not primary or not artist_key or not album_key:
                self.connection.execute("INSERT INTO metadata_event_projection(event_key,play_count,occurred_at,exclusion_reason) VALUES(?,?,?,?)",
                                        (row['event_key'], row['play_count'], row['occurred_at'], 'missing_artist_album_or_title'))
                continue
            track_key, unused_artist, unused_album, unused_title, slot = metadata_track_identity(
                row['artist'], row['album'], row['title'], row['disc'], row['track_number'],
                primary in slot_groups)
            self.connection.execute("INSERT OR IGNORE INTO metadata_artist(artist_key,normalized_artist,display_artist) VALUES(?,?,?)",
                                    (artist_key, normalized_artist, row['artist']))
            self.connection.execute("INSERT OR IGNORE INTO metadata_album(album_key,artist_key,normalized_artist,normalized_album,display_artist,display_album,preferred_artwork_json,kodi_album_dbid,year) VALUES(?,?,?,?,?,?,?,?,?)",
                                     (album_key, artist_key, normalized_album_artist, normalized_album,
                                      row['album_artist'] or row['artist'], row['album'], row['album_artwork'],
                                      row['kodi_album_dbid'], row['year']))
            self.connection.execute("INSERT OR IGNORE INTO metadata_track(track_key,artist_key,album_key,normalized_artist,normalized_album,normalized_title,slot_discriminator,display_artist,display_album,display_title,preferred_path,preferred_artwork_json,disc,track_number) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                     (track_key, artist_key, album_key, artist, album, title, slot,
                                      row['artist'], row['album'], row['title'], row['canonical_path'],
                                      row['artwork_json'] or row['album_artwork'], row['disc'], row['track_number']))
            self.connection.execute("INSERT INTO metadata_event_projection(event_key,metadata_track_key,metadata_album_key,metadata_artist_key,play_count,occurred_at) VALUES(?,?,?,?,?,?)",
                                    (row['event_key'], track_key, album_key, artist_key,
                                     row['play_count'], row['occurred_at']))
        # Preserve current album-play semantics: only qualified occurrences
        # contribute to album ranking.  Historical occurrences not resolvable
        # through retained Schema-12 catalog evidence remain safely excluded.
        generation = self.authoritative_catalog_generation_id()
        if generation is not None:
            occurrences = self.connection.execute("""SELECT o.occurrence_id,o.qualified_at,a.artist,a.title
                FROM album_play_occurrence o LEFT JOIN catalog_logical_album a
                  ON a.generation_id=? AND a.logical_album_key=o.logical_album_key
                WHERE o.qualified_at IS NOT NULL""", (generation,)).fetchall()
            for row in occurrences:
                album_key, unused_artist, unused_album = metadata_album_identity(row['artist'], row['artist'], row['title'])
                if album_key:
                    self.connection.execute("INSERT INTO metadata_album_occurrence(occurrence_id,metadata_album_key,qualified_at) VALUES(?,?,?)",
                                            (row['occurrence_id'], album_key, row['qualified_at']))
                else:
                    self.connection.execute("INSERT INTO metadata_album_occurrence(occurrence_id,qualified_at,exclusion_reason) VALUES(?,?,?)",
                                            (row['occurrence_id'], row['qualified_at'], 'historical_album_metadata_unavailable'))

    def rebuild_metadata_rank_state(self):
        """Explicit maintenance/migration helper; no Kodi library enumeration."""
        with self.connection:
            self._rebuild_metadata_rank_state()

    def _project_metadata_event(self, event_key):
        """Project one newly committed raw event without catalog reconstruction.

        This is deliberately event-bounded: playback never enumerates Kodi or
        rebuilds the historical projection.  A rare new duplicate-title group
        is conservatively represented without a slot until explicit repair;
        already-known ambiguous groups retain their slot discriminator.
        """
        row = self.connection.execute("""SELECT e.event_key,e.play_count,e.occurred_at,
                t.artist,t.album,t.title,t.disc,t.track_number,t.canonical_path,t.artwork_json,
                ai.artist AS album_artist,ai.artwork_json AS album_artwork,ai.kodi_album_dbid,ai.year
            FROM play_event e LEFT JOIN track_identity t ON t.identity_key=e.track_key
            LEFT JOIN album_identity ai ON ai.album_key=e.album_key WHERE e.event_key=?""",
            (event_key,)).fetchone()
        if row is None:
            return (None, None)
        primary, artist, album, title, unused = metadata_track_identity(row['artist'], row['album'], row['title'])
        artist_key, normalized_artist = metadata_artist_identity(row['artist'])
        album_key, normalized_album_artist, normalized_album = metadata_album_identity(
            row['album_artist'], row['artist'], row['album'])
        if not primary or not artist_key or not album_key:
            self.connection.execute("INSERT OR IGNORE INTO metadata_event_projection(event_key,play_count,occurred_at,exclusion_reason) VALUES(?,?,?,?)",
                                    (row['event_key'], row['play_count'], row['occurred_at'], 'missing_artist_album_or_title'))
            return (None, None)
        has_slot = self.connection.execute("SELECT 1 FROM metadata_track WHERE normalized_artist=? AND normalized_album=? AND normalized_title=? AND slot_discriminator IS NOT NULL LIMIT 1",
                                           (artist, album, title)).fetchone() is not None
        track_key, unused_artist, unused_album, unused_title, slot = metadata_track_identity(
            row['artist'], row['album'], row['title'], row['disc'], row['track_number'], has_slot)
        self.connection.execute("INSERT OR IGNORE INTO metadata_artist(artist_key,normalized_artist,display_artist) VALUES(?,?,?)",
                                (artist_key, normalized_artist, row['artist']))
        self.connection.execute("INSERT OR IGNORE INTO metadata_album(album_key,artist_key,normalized_artist,normalized_album,display_artist,display_album,preferred_artwork_json,kodi_album_dbid,year) VALUES(?,?,?,?,?,?,?,?,?)",
                                (album_key, artist_key, normalized_album_artist, normalized_album,
                                 row['album_artist'] or row['artist'], row['album'], row['album_artwork'],
                                 row['kodi_album_dbid'], row['year']))
        self.connection.execute("INSERT OR IGNORE INTO metadata_track(track_key,artist_key,album_key,normalized_artist,normalized_album,normalized_title,slot_discriminator,display_artist,display_album,display_title,preferred_path,preferred_artwork_json,disc,track_number) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (track_key, artist_key, album_key, artist, album, title, slot,
                                 row['artist'], row['album'], row['title'], row['canonical_path'],
                                 row['artwork_json'] or row['album_artwork'], row['disc'], row['track_number']))
        self.connection.execute("INSERT OR IGNORE INTO metadata_event_projection(event_key,metadata_track_key,metadata_album_key,metadata_artist_key,play_count,occurred_at) VALUES(?,?,?,?,?,?)",
                                (row['event_key'], track_key, album_key, artist_key,
                                 row['play_count'], row['occurred_at']))
        return (track_key, album_key)

    def _verify_schema12_objects(self):
        """Reject pre-existing candidate objects that cannot hold schema-12 state."""
        required = {
            'catalog_logical_song': {'generation_id','logical_song_key','normalized_artist','normalized_title',
                                    'display_artist','display_title','updated_at'},
            'catalog_logical_projection': {'generation_id','track_key','logical_song_key','eligibility',
                                           'exclusion_reason','projected_at'},
            'catalog_logical_totals': {'generation_id','logical_song_key','imported_plays','observed_plays',
                                       'total_plays','last_played_at'},
            'catalog_logical_album': {'generation_id','logical_album_key','source_root_id','artist_folder',
                                      'album_folder','updated_at'},
            'catalog_logical_album_song': {'generation_id','logical_album_key','logical_song_key'},
            'catalog_logical_album_track': {'generation_id','logical_album_key','track_key'},
        }
        for table, columns in required.items():
            actual = {row[1] for row in self.connection.execute('PRAGMA table_info(%s)' % table)}
            if not columns.issubset(actual):
                raise RuntimeError('incompatible partial schema-12 table: %s' % table)

    def _verify_schema7_objects(self):
        """Refuse a partial migration only when its existing objects conflict."""
        required = {
            'logical_song': {'logical_song_key','normalized_artist','normalized_title','display_artist','display_title','updated_at'},
            'logical_song_projection': {'track_key','logical_song_key','eligibility','exclusion_reason','projected_at'},
            'logical_song_totals': {'logical_song_key','imported_plays','observed_plays','total_plays','last_played_at'},
            'logical_album_current': {'logical_album_key','source_root_id','artist_folder','album_folder','updated_at'},
            'logical_album_current_song': {'logical_album_key','logical_song_key'},
        }
        for table, columns in required.items():
            actual = {row[1] for row in self.connection.execute('PRAGMA table_info(%s)' % table)}
            if not columns.issubset(actual):
                raise RuntimeError('incompatible partial schema-7 table: %s' % table)

    def _verify_schema8_objects(self):
        actual = {row[1] for row in self.connection.execute('PRAGMA table_info(logical_song)')}
        required = {'logical_song_key', 'normalized_artist', 'normalized_title', 'collision_discriminator',
                    'display_artist', 'display_title', 'updated_at'}
        if not required.issubset(actual):
            raise RuntimeError('incompatible partial schema-8 logical_song table')

    def _verify_schema9_objects(self):
        required = {
            'logical_album_current_track': {'logical_album_key', 'track_key'},
            'album_play_occurrence': {'occurrence_id', 'logical_album_key', 'started_at', 'last_activity_at',
                                      'ended_at', 'qualified_at', 'distinct_tracks', 'required_tracks', 'state'},
            'album_play_occurrence_track': {'occurrence_id', 'logical_song_key'},
        }
        for table, columns in required.items():
            actual = {row[1] for row in self.connection.execute('PRAGMA table_info(%s)' % table)}
            if not columns.issubset(actual):
                raise RuntimeError('incompatible partial schema-9 table: %s' % table)

    def _verify_schema10_objects(self):
        sql = self.connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='album_play_occurrence'").fetchone()
        if sql is None or 'logical_album_current' in (sql[0] or '').casefold():
            raise RuntimeError('schema-10 occurrence table still depends on logical_album_current')

    def _verify_schema11_objects(self):
        rows = self.connection.execute("PRAGMA foreign_key_list(album_play_occurrence_track)").fetchall()
        parents = set(row[2] for row in rows)
        if parents != set(('album_play_occurrence',)):
            raise RuntimeError('schema-11 occurrence-track table has invalid foreign-key parents')

    def _clear_logical_album_current(self):
        """Remove current derived membership before replacing referenced songs."""
        if self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='logical_album_current_track'").fetchone():
            self.connection.execute("DELETE FROM logical_album_current_track")
        self.connection.execute("DELETE FROM logical_album_current_song")
        self.connection.execute("DELETE FROM logical_album_current")

    def _collision_resolution_map(self, generation_id=None):
        """Return deterministic Schema-8 collision decisions for known tracks.

        A recording discriminator exists only inside a base artist/title group
        with more than one trustworthy MusicBrainz recording ID.  Missing-ID
        copies may join a cluster only when their album or release context has
        exactly one trustworthy recording candidate; otherwise the physical
        identity is explicitly ambiguous and is never guessed into a cluster.
        Exact current paths and recording IDs also let historical identities
        resolve to a current recording-specific cluster.
        """
        if generation_id is None:
            rows = self.connection.execute("""SELECT c.album_key,c.track_key,
                        i.artist,i.title,i.recording_id,i.release_id,i.canonical_path,
                        i.duration_ms,c.disc,c.track_number
                    FROM album_track_catalog c JOIN track_identity i ON i.identity_key=c.track_key
                    ORDER BY c.track_key,c.album_key""").fetchall()
        else:
            rows = self.connection.execute("""SELECT c.album_key,c.track_key,
                        i.artist,i.title,i.recording_id,i.release_id,i.canonical_path,
                        i.duration_ms,c.disc,c.track_number
                    FROM catalog_track_snapshot c JOIN track_identity i ON i.identity_key=c.track_key
                    WHERE c.generation_id=? ORDER BY c.track_key,c.album_key""",
                    (generation_id,)).fetchall()
        grouped = {}
        for row in rows:
            artist, title = normalized_text(row['artist']), normalized_text(row['title'])
            if artist and title:
                grouped.setdefault((artist, title), []).append(dict(row))
        all_identities = [dict(row) for row in self.connection.execute(
            "SELECT identity_key,canonical_path,recording_id,artist,title FROM track_identity")]
        historical_by_base = {}
        for row in all_identities:
            base = (normalized_text(row['artist']), normalized_text(row['title']))
            if base[0] and base[1]:
                historical_by_base.setdefault(base, []).append(row)
        result = {}
        for base, base_rows in grouped.items():
            recordings = {normalized_text(row['recording_id']) for row in base_rows
                          if normalized_text(row['recording_id'])}
            if len(recordings) <= 1:
                continue
            by_album, by_release, by_path, by_recording = {}, {}, {}, {}
            for row in base_rows:
                recording = normalized_text(row['recording_id'])
                if not recording:
                    continue
                discriminator = 'recording:' + recording
                by_album.setdefault(row['album_key'], set()).add(discriminator)
                release = normalized_text(row['release_id'])
                if release:
                    by_release.setdefault(release, set()).add(discriminator)
                path = normalized_path(row['canonical_path'])
                if path:
                    by_path.setdefault(path, set()).add(discriminator)
                by_recording.setdefault(recording, set()).add(discriminator)
            for row in base_rows:
                track_key = row['track_key']
                recording = normalized_text(row['recording_id'])
                if recording:
                    result[track_key] = ('recording:' + recording, None)
                    continue
                candidates = set(by_album.get(row['album_key'], set()))
                release = normalized_text(row['release_id'])
                if not candidates and release:
                    candidates = set(by_release.get(release, set()))
                if len(candidates) == 1:
                    result[track_key] = (next(iter(candidates)), None)
                else:
                    result[track_key] = (None, 'ambiguous_recording_collision')
            # Historical track identities may predate Identity-v2.  Resolve
            # them only through an exact canonical path or a unique recording.
            for row in historical_by_base.get(base, ()): 
                if row['identity_key'] in result:
                    continue
                candidates = set()
                path = normalized_path(row['canonical_path'])
                if path:
                    candidates.update(by_path.get(path, set()))
                recording = normalized_text(row['recording_id'])
                if recording:
                    candidates.update(by_recording.get(recording, set()))
                if len(candidates) == 1:
                    result[row['identity_key']] = (next(iter(candidates)), None)
                else:
                    result[row['identity_key']] = (None, 'ambiguous_recording_collision')
        return result

    def _logical_identity_for_track(self, track_key, metadata, resolutions=None, generation_id=None):
        if resolutions is None:
            resolutions = self._collision_resolution_map(generation_id)
        discriminator, exclusion = resolutions.get(track_key, ('', None))
        if exclusion:
            artist = normalized_text(metadata.get('artist'))
            title = normalized_text(metadata.get('title'))
            return None, artist, title, exclusion, ''
        key, artist, title, exclusion = logical_song_identity(metadata, discriminator)
        return key, artist, title, exclusion, discriminator or ''

    def _populate_logical_album_current(self, source_root, now, generation_id=None, should_abort=None):
        """Populate current folder membership inside the caller's transaction."""
        if not source_root:
            return
        if generation_id is None:
            generation = self.latest_current_catalog_generation()
            generation_id = generation[0] if generation else None
        rows = self.connection.execute("""SELECT c.album_key,c.track_key,a.artist,a.title,a.year,a.kodi_album_dbid,a.artwork_json,i.canonical_path,p.logical_song_key
            FROM album_track_catalog c JOIN album_identity a ON a.album_key=c.album_key
            JOIN track_identity i ON i.identity_key=c.track_key
            JOIN logical_song_projection p ON p.track_key=c.track_key WHERE p.eligibility='eligible'""").fetchall()
        seen = set()
        has_physical_membership = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='logical_album_current_track'"
        ).fetchone() is not None
        for index, row in enumerate(rows):
            if should_abort and index % 250 == 0 and should_abort():
                raise CatalogRefreshAborted('media playback started during logical album membership rebuild')
            identity = logical_album_identity(row['canonical_path'], source_root.get('source_root_id'), source_root.get('physical_source_root'))
            if not identity:
                continue
            key, artist_folder, album_folder, unused_artist, unused_album = identity
            if key not in seen:
                self.connection.execute("INSERT INTO logical_album_current(logical_album_key,source_root_id,artist_folder,album_folder,artist,title,year,kodi_album_dbid,artwork_json,generation_id,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (key, source_root.get('source_root_id'), artist_folder, album_folder, row['artist'], row['title'], row['year'], row['kodi_album_dbid'], row['artwork_json'], generation_id, now))
                seen.add(key)
            self.connection.execute("INSERT OR IGNORE INTO logical_album_current_song(logical_album_key,logical_song_key) VALUES(?,?)", (key, row['logical_song_key']))
            if has_physical_membership:
                self.connection.execute("INSERT OR IGNORE INTO logical_album_current_track(logical_album_key,track_key) VALUES(?,?)", (key, row['track_key']))

    def _candidate_table_counts(self, generation_id):
        """Return scoped derived counts for validation and focused testing."""
        tables = ('catalog_logical_song', 'catalog_logical_projection',
                  'catalog_logical_totals', 'catalog_logical_album',
                  'catalog_logical_album_song', 'catalog_logical_album_track')
        return {table: self.connection.execute(
            'SELECT count(*) FROM %s WHERE generation_id=?' % table,
            (generation_id,)).fetchone()[0] for table in tables}

    def _clear_candidate_logical_state(self, generation_id):
        for table in ('catalog_logical_album_track', 'catalog_logical_album_song',
                      'catalog_logical_album', 'catalog_logical_totals',
                      'catalog_logical_projection', 'catalog_logical_song'):
            self.connection.execute('DELETE FROM %s WHERE generation_id=?' % table,
                                    (generation_id,))

    def _candidate_projection_rows(self, generation_id, now, should_abort=None):
        """Precompute Schema-8 projection rows without per-identity SQL writes."""
        resolutions = self._collision_resolution_map(generation_id)
        songs, projections = {}, []
        rows = self.connection.execute("SELECT * FROM track_identity ORDER BY identity_key").fetchall()
        for index, row in enumerate(rows):
            if should_abort and index % 250 == 0 and should_abort():
                raise CatalogRefreshAborted('media playback started during candidate logical projection preparation')
            metadata = dict(row)
            key, artist, title, exclusion, discriminator = self._logical_identity_for_track(
                row['identity_key'], metadata, resolutions, generation_id)
            if key:
                # This mirrors INSERT OR IGNORE over identity_key-ordered rows.
                songs.setdefault(key, (generation_id, key, artist, title, discriminator,
                                       row['artist'] or '', row['title'] or '', row['identity_key'],
                                       row['canonical_path'], row['artwork_json'], now))
                projections.append((generation_id, row['identity_key'], key, 'eligible', None, now))
            else:
                projections.append((generation_id, row['identity_key'], None, 'excluded', exclusion, now))
        return list(songs.values()), projections

    def _candidate_artwork_rows(self, generation_id, songs, projections, should_abort=None):
        """Select candidate artwork in memory, preserving the old ordering policy."""
        current_tracks = set(row[0] for row in self.connection.execute(
            'SELECT DISTINCT track_key FROM catalog_track_snapshot WHERE generation_id=?',
            (generation_id,)))
        projected = {row[1]: row[2] for row in projections if row[3] == 'eligible'}
        by_song = {}
        for index, row in enumerate(self.connection.execute(
                'SELECT identity_key,artwork_json FROM track_identity ORDER BY identity_key')):
            if should_abort and index % 250 == 0 and should_abort():
                raise CatalogRefreshAborted('media playback started during candidate artwork preparation')
            key = projected.get(row['identity_key'])
            if key:
                by_song.setdefault(key, []).append((row['identity_key'], row['artwork_json']))
        result = []
        for index, song in enumerate(songs):
            if should_abort and index % 250 == 0 and should_abort():
                raise CatalogRefreshAborted('media playback started during candidate artwork selection')
            generation, key, artist, title, discriminator, display_artist, display_title, preferred, path, artwork, now = song
            options = sorted(by_song.get(key, ()), key=lambda item: (
                0 if item[0] == preferred else 1 if item[0] in current_tracks else 2, item[0]))
            chosen = next((value for unused, value in options if self._usable_artwork_json(value)), None)
            result.append((generation, key, artist, title, discriminator, display_artist,
                           display_title, preferred, path, chosen, now))
        return result

    def _candidate_album_rows(self, generation_id, source_root, now, should_abort=None):
        if not source_root:
            return [], [], []
        rows = self.connection.execute("""SELECT c.album_key,c.track_key,a.artist,a.title,a.year,
                    a.kodi_album_dbid,a.artwork_json,i.canonical_path,p.logical_song_key
            FROM catalog_track_snapshot c JOIN album_identity a ON a.album_key=c.album_key
            JOIN track_identity i ON i.identity_key=c.track_key
            JOIN catalog_logical_projection p ON p.generation_id=c.generation_id AND p.track_key=c.track_key
            WHERE c.generation_id=? AND p.eligibility='eligible'
            ORDER BY c.album_key,c.track_key""", (generation_id,)).fetchall()
        albums, songs, tracks = {}, set(), set()
        for index, row in enumerate(rows):
            if should_abort and index % 250 == 0 and should_abort():
                raise CatalogRefreshAborted('media playback started during candidate logical album preparation')
            identity = logical_album_identity(row['canonical_path'], source_root.get('source_root_id'),
                                              source_root.get('physical_source_root'))
            if not identity:
                continue
            key, artist_folder, album_folder, unused_artist, unused_album = identity
            albums.setdefault(key, (generation_id, key, source_root.get('source_root_id'), artist_folder,
                                    album_folder, row['artist'], row['title'], row['year'],
                                    row['kodi_album_dbid'], row['artwork_json'], now))
            songs.add((generation_id, key, row['logical_song_key']))
            tracks.add((generation_id, key, row['track_key']))
        return list(albums.values()), list(songs), list(tracks)

    def _catalog_playback_checkpoint(self, should_abort, phase):
        """Yield before beginning another bounded catalog-maintenance unit."""
        if should_abort and should_abort():
            raise CatalogRefreshAborted('media playback started before staged ' + phase)

    def _candidate_transaction(self, phase, action, metrics, should_abort=None):
        """Execute one bounded candidate-only write transaction.

        A transaction already in progress is allowed to commit.  The immediate
        post-commit checkpoint prevents the following batch from starting while
        media is active, without rolling back a safely committed bounded unit.
        """
        self._catalog_playback_checkpoint(should_abort, phase)
        started = time.monotonic()
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            action()
            commit_started = time.monotonic()
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        metrics['transactions'] += 1
        metrics['max_transaction_seconds'] = max(
            metrics['max_transaction_seconds'], time.monotonic() - started)
        metrics['max_commit_seconds'] = max(
            metrics['max_commit_seconds'], time.monotonic() - commit_started)
        self._catalog_playback_checkpoint(should_abort, phase + ' next batch')

    def _build_candidate_logical_projection(self, generation_id, source_root=None, should_abort=None,
                                            batch_size=1000):
        """Build only one non-authoritative generation's derived logical catalog.

        Preparation is read-only.  Each durable phase has its own bounded
        transaction, so an interrupted candidate cannot touch the authoritative
        generation or leave a half-populated phase visible as complete.
        """
        candidate = self.connection.execute(
            'SELECT state FROM catalog_generation WHERE generation_id=?', (generation_id,)).fetchone()
        if candidate is None or candidate['state'] != 'building':
            raise RuntimeError('candidate logical projection requires a building generation')
        now = int(time.time())
        metrics = {'transactions': 0, 'max_transaction_seconds': 0.0,
                   'max_commit_seconds': 0.0}
        songs, projections = self._candidate_projection_rows(generation_id, now, should_abort)
        songs = self._candidate_artwork_rows(generation_id, songs, projections, should_abort)
        def clear():
            self._clear_candidate_logical_state(generation_id)
        self._candidate_transaction('logical-projection clear', clear, metrics, should_abort)
        for start in range(0, len(songs), batch_size):
            def write_songs(start=start):
                self.connection.executemany("""INSERT INTO catalog_logical_song(
                    generation_id,logical_song_key,normalized_artist,normalized_title,collision_discriminator,
                    display_artist,display_title,preferred_track_key,preferred_path,preferred_artwork_json,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""", songs[start:start + batch_size])
            self._candidate_transaction('logical-song batch %d' % (start // batch_size + 1),
                                        write_songs, metrics, should_abort)
        # Projection rows are the largest output.  Bounded commits give the
        # playback gate a chance to abort safely between batches.
        for start in range(0, len(projections), batch_size):
            if should_abort and should_abort():
                raise CatalogRefreshAborted('media playback started during candidate logical projection write')
            def write_projection(start=start):
                self.connection.executemany("""INSERT INTO catalog_logical_projection(
                    generation_id,track_key,logical_song_key,eligibility,exclusion_reason,projected_at)
                    VALUES(?,?,?,?,?,?)""", projections[start:start + batch_size])
            self._candidate_transaction('logical-projection batch %d' % (start // batch_size + 1),
                                        write_projection, metrics, should_abort)
        def write_totals():
            self.connection.execute("""INSERT INTO catalog_logical_totals(
                    generation_id,logical_song_key,imported_plays,observed_plays,total_plays,last_played_at)
                SELECT ?,p.logical_song_key,
                       sum(CASE WHEN e.source='kodi_baseline_import' THEN e.play_count ELSE 0 END),
                       sum(CASE WHEN e.source='live_tracking' THEN e.play_count ELSE 0 END),
                       sum(e.play_count),max(e.occurred_at)
                  FROM play_event e JOIN catalog_logical_projection p ON p.track_key=e.track_key
                 WHERE p.generation_id=? AND p.eligibility='eligible' GROUP BY p.logical_song_key""",
                (generation_id, generation_id))
        self._candidate_transaction('logical-totals', write_totals, metrics, should_abort)
        albums, album_songs, album_tracks = self._candidate_album_rows(
            generation_id, source_root, now, should_abort)
        for label, sql, values in (
                ('logical-album', """INSERT INTO catalog_logical_album(
                    generation_id,logical_album_key,source_root_id,artist_folder,album_folder,artist,title,year,
                    kodi_album_dbid,artwork_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""", albums),
                ('logical-album-song', """INSERT INTO catalog_logical_album_song(
                    generation_id,logical_album_key,logical_song_key) VALUES(?,?,?)""", album_songs),
                ('logical-album-track', """INSERT INTO catalog_logical_album_track(
                    generation_id,logical_album_key,track_key) VALUES(?,?,?)""", album_tracks)):
            for start in range(0, len(values), batch_size):
                def write_membership(start=start, sql=sql, values=values):
                    self.connection.executemany(sql, values[start:start + batch_size])
                self._candidate_transaction('%s batch %d' % (label, start // batch_size + 1),
                                            write_membership, metrics, should_abort)
        metrics.update(self._candidate_table_counts(generation_id))
        return metrics

    def rebuild_logical_projection(self, source_root=None, generation_id=None):
        """Atomically replace derived logical projection and current album membership."""
        with self.connection:
            self._rebuild_logical_projection_contents(source_root, generation_id)

    def _rebuild_logical_projection_contents(self, source_root=None, generation_id=None, should_abort=None):
        """Replace derived logical state inside an existing transaction."""
        if generation_id is not None and self.schema_version() >= 12:
            return self._build_candidate_logical_projection(generation_id, source_root, should_abort)
        now = int(time.time())
        resolutions = self._collision_resolution_map()
        # logical_album_current_song references logical_song, so clear the
        # dependent current membership before rebuilding logical_song.
        self._clear_logical_album_current()
        self.connection.execute("DELETE FROM logical_song_totals")
        self.connection.execute("DELETE FROM logical_song_projection")
        self.connection.execute("DELETE FROM logical_song")
        for index, row in enumerate(self.connection.execute("SELECT * FROM track_identity ORDER BY identity_key")):
            if should_abort and index % 250 == 0 and should_abort():
                raise CatalogRefreshAborted('media playback started during logical projection rebuild')
            metadata = dict(row)
            key, artist, title, exclusion, discriminator = self._logical_identity_for_track(row['identity_key'], metadata, resolutions)
            if key:
                self.connection.execute("INSERT OR IGNORE INTO logical_song(logical_song_key,normalized_artist,normalized_title,collision_discriminator,display_artist,display_title,preferred_track_key,preferred_path,preferred_artwork_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (key, artist, title, discriminator, row['artist'] or '', row['title'] or '', row['identity_key'], row['canonical_path'], row['artwork_json'], now))
                self.connection.execute("INSERT INTO logical_song_projection(track_key,logical_song_key,eligibility,exclusion_reason,projected_at) VALUES(?,?,?,?,?)", (row['identity_key'], key, 'eligible', None, now))
            else:
                self.connection.execute("INSERT INTO logical_song_projection(track_key,logical_song_key,eligibility,exclusion_reason,projected_at) VALUES(?,?,?,?,?)", (row['identity_key'], None, 'excluded', exclusion, now))
        self.connection.execute("""INSERT INTO logical_song_totals(logical_song_key,imported_plays,observed_plays,total_plays,last_played_at)
                SELECT p.logical_song_key,
                       sum(CASE WHEN e.source='kodi_baseline_import' THEN e.play_count ELSE 0 END),
                       sum(CASE WHEN e.source='live_tracking' THEN e.play_count ELSE 0 END),
                       sum(e.play_count), max(e.occurred_at)
                  FROM play_event e JOIN logical_song_projection p ON p.track_key=e.track_key
             WHERE p.eligibility='eligible' GROUP BY p.logical_song_key""")
        self._refresh_logical_artwork(should_abort)
        self._validate_logical_projection()
        self._populate_logical_album_current(source_root, now, generation_id, should_abort)
        # A physical-release cache cannot represent Schema 8's logical-song
        # projection, so rebuild it only after the derived data is complete.
        self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('rankings_dirty','1')")

    def _validate_logical_projection(self):
        """Reject incomplete derived logical data before invalidating rankings."""
        identities = self.connection.execute("SELECT count(*) FROM track_identity").fetchone()[0]
        projections = self.connection.execute("SELECT count(*) FROM logical_song_projection").fetchone()[0]
        invalid = self.connection.execute("""SELECT count(*)
            FROM play_event e LEFT JOIN logical_song_projection p ON p.track_key=e.track_key
            WHERE p.track_key IS NULL OR (p.eligibility='eligible' AND p.logical_song_key IS NULL)""").fetchone()[0]
        raw = self.connection.execute("SELECT coalesce(sum(play_count),0) FROM play_event").fetchone()[0]
        logical = self.connection.execute("SELECT coalesce(sum(total_plays),0) FROM logical_song_totals").fetchone()[0]
        excluded = self.connection.execute("""SELECT coalesce(sum(e.play_count),0)
            FROM play_event e JOIN logical_song_projection p ON p.track_key=e.track_key
            WHERE p.eligibility='excluded'""").fetchone()[0]
        if projections != identities or invalid or raw != logical + excluded:
            raise RuntimeError("logical projection validation failed")

    def ensure_logical_projection(self, track_key, metadata):
        """Project one physical identity; returns its logical key or None if excluded."""
        now = int(time.time())
        key, artist, title, exclusion, discriminator = self._logical_identity_for_track(track_key, metadata)
        with self.connection:
            if not key:
                self.connection.execute("INSERT OR REPLACE INTO logical_song_projection(track_key,logical_song_key,eligibility,exclusion_reason,projected_at) VALUES(?,?,?,?,?)", (track_key, None, 'excluded', exclusion, now))
                return None
            art = metadata.get('artwork_json')
            self.connection.execute("INSERT OR IGNORE INTO logical_song(logical_song_key,normalized_artist,normalized_title,collision_discriminator,display_artist,display_title,preferred_track_key,preferred_path,preferred_artwork_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (key, artist, title, discriminator, metadata.get('artist') or '', metadata.get('title') or '', track_key, metadata.get('file') or '', art, now))
            # A direct, non-musicdb playback path is the most recently proven playable copy.
            path = metadata.get('file') or ''
            if path and not path.casefold().startswith('musicdb://'):
                self.connection.execute("UPDATE logical_song SET preferred_track_key=?,preferred_path=?,preferred_artwork_json=COALESCE(?,preferred_artwork_json),updated_at=? WHERE logical_song_key=?", (track_key, path, art, now, key))
            self.connection.execute("INSERT OR REPLACE INTO logical_song_projection(track_key,logical_song_key,eligibility,exclusion_reason,projected_at) VALUES(?,?,?,?,?)", (track_key, key, 'eligible', None, now))
            self._select_logical_artwork(key)
        return key

    @staticmethod
    def _usable_artwork_json(value):
        """Return true only for artwork Kodi can use as a Widget 1 thumbnail."""
        try:
            artwork = json.loads(value or '{}')
        except (TypeError, ValueError):
            return False
        if not isinstance(artwork, dict):
            return False
        return any(str(artwork.get(key) or '').strip() for key in ('thumb', 'album.thumb'))

    def _select_logical_artwork(self, logical_song_key):
        """Choose artwork independently without changing the approved playable copy."""
        preferred = self.connection.execute(
            "SELECT preferred_track_key FROM logical_song WHERE logical_song_key=?",
            (logical_song_key,)).fetchone()
        if preferred is None:
            return
        rows = self.connection.execute("""SELECT i.identity_key,i.artwork_json,
                CASE WHEN EXISTS(SELECT 1 FROM album_track_catalog c WHERE c.track_key=i.identity_key)
                     THEN 1 ELSE 0 END AS is_current
            FROM track_identity i JOIN logical_song_projection p ON p.track_key=i.identity_key
            WHERE p.logical_song_key=? AND p.eligibility='eligible'
            ORDER BY CASE WHEN i.identity_key=? THEN 0 WHEN EXISTS(
                SELECT 1 FROM album_track_catalog c WHERE c.track_key=i.identity_key) THEN 1 ELSE 2 END,
                i.identity_key""", (logical_song_key, preferred['preferred_track_key'])).fetchall()
        for row in rows:
            if self._usable_artwork_json(row['artwork_json']):
                self.connection.execute(
                    "UPDATE logical_song SET preferred_artwork_json=? WHERE logical_song_key=?",
                    (row['artwork_json'], logical_song_key))
                return
        self.connection.execute(
            "UPDATE logical_song SET preferred_artwork_json=NULL WHERE logical_song_key=?",
            (logical_song_key,))

    def _refresh_logical_artwork(self, should_abort=None):
        """Bulk-select fallback artwork without changing any preferred paths."""
        selected = {}
        existing = {}
        rows = self.connection.execute("""SELECT s.logical_song_key,s.preferred_track_key,
                s.preferred_artwork_json,i.identity_key,i.artwork_json
            FROM logical_song s
            JOIN logical_song_projection p ON p.logical_song_key=s.logical_song_key
            JOIN track_identity i ON i.identity_key=p.track_key
            WHERE p.eligibility='eligible'
            ORDER BY s.logical_song_key,
                CASE WHEN i.identity_key=s.preferred_track_key THEN 0 WHEN EXISTS(
                    SELECT 1 FROM album_track_catalog c WHERE c.track_key=i.identity_key) THEN 1 ELSE 2 END,
                i.identity_key""").fetchall()
        for index, row in enumerate(rows):
            if should_abort and index % 250 == 0 and should_abort():
                raise CatalogRefreshAborted('media playback started during logical artwork rebuild')
            key = row['logical_song_key']
            existing.setdefault(key, row['preferred_artwork_json'])
            if key not in selected and self._usable_artwork_json(row['artwork_json']):
                selected[key] = row['artwork_json']
        updates = [(selected.get(key), key) for key, value in existing.items()
                   if value != selected.get(key)]
        if updates:
            self.connection.executemany(
                "UPDATE logical_song SET preferred_artwork_json=? WHERE logical_song_key=?", updates)

    def refresh_logical_artwork(self):
        """Refresh only derived artwork choices; paths, projection, and totals stay intact."""
        with self.connection:
            self._refresh_logical_artwork()
            self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('rankings_dirty','1')")

    def migrate_identity_v2(self, mapping):
        """Explicit, transactional v1-to-v2 key migration; never automatic."""
        if self.schema_version() != 4:
            raise RuntimeError("Identity v2 migration requires schema 4")
        if not mapping:
            raise ValueError("Identity v2 migration requires verified mappings")
        now = int(time.time())
        with self.connection:
            self.connection.execute("""CREATE TABLE IF NOT EXISTS identity_migration_audit (
                old_identity_key TEXT PRIMARY KEY,new_identity_key TEXT NOT NULL,
                migrated_at INTEGER NOT NULL,identity_version INTEGER NOT NULL,
                mapping_method TEXT NOT NULL, evidence_json TEXT)""")
            self.connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS identity_migration_audit_new_idx ON identity_migration_audit(new_identity_key)")
            for old_key, new_key, method, evidence in mapping:
                row=self.connection.execute("SELECT * FROM track_identity WHERE identity_key=?",(old_key,)).fetchone()
                if row is None: raise ValueError("unknown historical identity: %s" % old_key)
                existing=self.connection.execute("SELECT 1 FROM track_identity WHERE identity_key=?",(new_key,)).fetchone()
                if existing and new_key != old_key: raise ValueError("Identity v2 key already exists: %s" % new_key)
                if new_key != old_key:
                    values=dict(row); values['identity_key']=new_key; values['identity_kind']='identity_v2'; values['updated_at']=now
                    columns=','.join(values.keys()); marks=','.join('?' for unused in values)
                    self.connection.execute("INSERT INTO track_identity(%s) VALUES(%s)" % (columns,marks),tuple(values.values()))
                    self.connection.execute("UPDATE play_event SET track_key=? WHERE track_key=?",(new_key,old_key))
                    self.connection.execute("UPDATE track_totals SET track_key=? WHERE track_key=?",(new_key,old_key))
                    self.connection.execute("INSERT OR IGNORE INTO album_track(album_key,track_key,disc,track_number) SELECT album_key,?,disc,track_number FROM album_track WHERE track_key=?",(new_key,old_key))
                    self.connection.execute("DELETE FROM album_track WHERE track_key=?",(old_key,))
                    self.connection.execute("UPDATE album_track_catalog SET track_key=? WHERE track_key=?",(new_key,old_key))
                    self.connection.execute("UPDATE catalog_track_snapshot SET track_key=? WHERE track_key=?",(new_key,old_key))
                    self.connection.execute("UPDATE track_replacement SET predecessor_track_key=? WHERE predecessor_track_key=?",(new_key,old_key))
                    self.connection.execute("UPDATE track_replacement SET successor_track_key=? WHERE successor_track_key=?",(new_key,old_key))
                    self.connection.execute("DELETE FROM track_identity WHERE identity_key=?",(old_key,))
                self.connection.execute("INSERT INTO identity_migration_audit(old_identity_key,new_identity_key,migrated_at,identity_version,mapping_method,evidence_json) VALUES(?,?,?,?,?,?)",(old_key,new_key,now,2,method,evidence))
            self.connection.execute("DELETE FROM track_rank_cache")
            self.connection.execute("DELETE FROM album_rank_cache")
            self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('rankings_dirty','1')")
            self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version','5')")

    def repair_catalog_generation_states(self):
        """Schema-5 compatibility repair for the frozen catalog state names."""
        sql = self.connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='catalog_generation'").fetchone()[0]
        if "'complete'" in sql and "'superseded'" in sql:
            return False
        allowed = set(row[0] for row in self.connection.execute("SELECT DISTINCT state FROM catalog_generation"))
        if not allowed.issubset(set(('building', 'completed', 'complete', 'failed', 'superseded'))):
            raise RuntimeError("unknown legacy catalog generation state")
        self.connection.execute("PRAGMA foreign_keys=OFF")
        try:
            with self.connection:
                self.connection.execute("""CREATE TABLE catalog_generation_v5 (
                    generation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at INTEGER NOT NULL, completed_at INTEGER,
                    state TEXT NOT NULL CHECK(state IN ('building','complete','failed','superseded')),
                    source TEXT NOT NULL, notes TEXT)""")
                self.connection.execute("""INSERT INTO catalog_generation_v5(generation_id,started_at,completed_at,state,source,notes)
                    SELECT generation_id,started_at,completed_at,
                    CASE state WHEN 'completed' THEN 'complete' ELSE state END,source,notes FROM catalog_generation""")
                self.connection.execute("DROP TABLE catalog_generation")
                self.connection.execute("ALTER TABLE catalog_generation_v5 RENAME TO catalog_generation")
        finally:
            self.connection.execute("PRAGMA foreign_keys=ON")
        return True

    def request_catalog_refresh(self):
        """Persist an explicit maintenance request; service performs it only while idle."""
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('catalog_refresh_requested_at',?)", (str(int(time.time())),))

    def catalog_refresh_requested(self):
        return self.connection.execute("SELECT 1 FROM schema_meta WHERE key='catalog_refresh_requested_at'").fetchone() is not None

    def clear_catalog_refresh_request(self):
        """Clear one stale explicit request without altering catalog or history."""
        latest = self.connection.execute(
            "SELECT generation_id,state FROM catalog_generation ORDER BY generation_id DESC LIMIT 1"
        ).fetchone()
        current = self.connection.execute(
            "SELECT generation_id FROM catalog_generation WHERE state='complete' ORDER BY generation_id DESC LIMIT 1"
        ).fetchone()
        if (latest is None or current is None or latest['generation_id'] != current['generation_id'] or
                latest['state'] != 'complete'):
            raise RuntimeError('refresh request is not stale against the current complete catalog')
        snapshot_count = self.connection.execute(
            "SELECT count(*) FROM catalog_track_snapshot WHERE generation_id=?", (current['generation_id'],)
        ).fetchone()[0]
        membership_count = self.connection.execute("SELECT count(*) FROM album_track_catalog").fetchone()[0]
        if snapshot_count != membership_count:
            raise RuntimeError('current catalog membership does not match the current complete catalog')
        if self.connection.execute(
                "SELECT 1 FROM schema_meta WHERE key='catalog_refresh_requested_at'"
        ).fetchone() is None:
            raise RuntimeError('catalog refresh request is not present')
        with self.connection:
            self.connection.execute("DELETE FROM schema_meta WHERE key='catalog_refresh_requested_at'")

    def fail_interrupted_catalog_refresh(self, generation_id):
        """Atomically fail one interrupted newer build and clear its request."""
        with self.connection:
            candidate = self.connection.execute("SELECT state FROM catalog_generation WHERE generation_id=?", (generation_id,)).fetchone()
            trusted = self.connection.execute("SELECT generation_id FROM catalog_generation WHERE state='complete' ORDER BY generation_id DESC LIMIT 1").fetchone()
            snapshots = self.connection.execute("SELECT count(*) FROM catalog_track_snapshot WHERE generation_id=?", (generation_id,)).fetchone()[0]
            albums = self.connection.execute("SELECT count(*) FROM catalog_album_snapshot WHERE generation_id=?", (generation_id,)).fetchone()[0]
            if (candidate is None or candidate['state'] != 'building' or trusted is None or
                    generation_id <= trusted['generation_id'] or snapshots or albums):
                raise RuntimeError('catalog generation is not the exact empty interrupted refresh state')
            cursor = self.connection.execute("UPDATE catalog_generation SET state='failed',completed_at=? WHERE generation_id=? AND state='building'", (int(time.time()), generation_id))
            if cursor.rowcount != 1:
                raise RuntimeError('catalog generation changed during interruption repair')
            self.connection.execute("DELETE FROM schema_meta WHERE key='catalog_refresh_requested_at'")

    def latest_current_catalog_generation(self):
        return self.connection.execute("SELECT generation_id FROM catalog_generation WHERE state='complete' ORDER BY generation_id DESC LIMIT 1").fetchone()

    def authoritative_catalog_generation_id(self):
        """Return the one generation live Schema-12 catalog reads must use."""
        if self.schema_version() < 12:
            return None
        row = self.latest_current_catalog_generation()
        if row is None:
            fresh = self.connection.execute("SELECT value FROM schema_meta WHERE key='fresh_install_empty_catalog'").fetchone()
            if fresh is not None and fresh[0] == '1':
                return None
            raise RuntimeError('Schema 12 has no complete authoritative catalog generation')
        return row['generation_id']

    def authoritative_catalog_state(self):
        """Resolve authority once for callers that execute several related reads."""
        generation = self.authoritative_catalog_generation_id()
        return {'generation_id': generation, 'scoped': generation is not None}

    def _prune_catalog_snapshots_contents(self, generation_id):
        """Prune snapshots inside an existing transaction after validation."""
        snapshot_count = self.connection.execute(
            "SELECT count(*) FROM catalog_track_snapshot WHERE generation_id=?", (generation_id,)
        ).fetchone()[0]
        membership_count = self.connection.execute(
            "SELECT count(*) FROM album_track_catalog"
        ).fetchone()[0]
        if snapshot_count != membership_count:
            raise RuntimeError("current catalog snapshot does not match current catalog membership")
        track_cursor = self.connection.execute(
            "DELETE FROM catalog_track_snapshot WHERE generation_id<>?", (generation_id,)
        )
        album_cursor = self.connection.execute(
            "DELETE FROM catalog_album_snapshot WHERE generation_id<>?", (generation_id,)
        )
        return (track_cursor.rowcount, album_cursor.rowcount)

    def prune_catalog_snapshots(self):
        """Keep only the one current, complete full catalog snapshot.

        Current playback, logical projections, ranking, and exact-file fallback
        use the durable current identity/catalog tables.  Full generation
        snapshots are not part of the playback hot path.  A newly completed
        generation is retained until its current catalog membership has been
        verified; all earlier complete, superseded, or failed full snapshots
        are then removable.  Generation metadata remains as a lightweight
        audit trail.

        This method is deliberately idempotent.  It is called only after a
        successful promotion and projection rebuild, so a failed build cannot
        prune the last known-good snapshot.
        """
        current = self.latest_current_catalog_generation()
        if current is None:
            self.log("catalog snapshot prune skipped: no complete generation")
            return (0, 0)
        generation_id = current[0]
        with self.connection:
            removed = self._prune_catalog_snapshots_contents(generation_id)
        self.log("catalog snapshot prune retained generation %d; removed tracks=%d albums=%d" %
                 (generation_id, removed[0], removed[1]))
        return removed

    def ensure_identity(self, metadata):
        now = int(time.time())
        track_key, track_kind = track_identity(metadata)
        recording_key, unused_recording_kind = recording_identity(metadata)
        album_key, album_kind = album_identity(metadata)
        self.connection.execute(
            """INSERT INTO track_identity(identity_key,identity_kind,recording_id,release_id,disc,track_number,canonical_path,artist,album,title,kodi_dbid_last_seen,created_at,updated_at,recording_key,duration_ms)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(identity_key) DO UPDATE SET recording_id=excluded.recording_id,recording_key=excluded.recording_key,release_id=excluded.release_id,disc=excluded.disc,track_number=excluded.track_number,canonical_path=COALESCE(excluded.canonical_path,track_identity.canonical_path),artist=COALESCE(excluded.artist,track_identity.artist),album=COALESCE(excluded.album,track_identity.album),title=COALESCE(excluded.title,track_identity.title),kodi_dbid_last_seen=COALESCE(excluded.kodi_dbid_last_seen,track_identity.kodi_dbid_last_seen),duration_ms=COALESCE(excluded.duration_ms,track_identity.duration_ms),updated_at=excluded.updated_at""",
            (track_key, track_kind, metadata.get("musicbrainz_recording_id"), metadata.get("musicbrainz_release_id"), metadata.get("disc"), metadata.get("track"), metadata.get("file"), metadata.get("artist"), metadata.get("album"), metadata.get("title"), metadata.get("kodi_dbid"), now, now, recording_key or None, metadata.get("duration_ms")))
        self.connection.execute(
            """INSERT INTO album_identity(album_key,identity_kind,release_id,artist,title,year,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(album_key) DO UPDATE SET updated_at=excluded.updated_at""",
            (album_key, album_kind, metadata.get("musicbrainz_release_id"), metadata.get("album_artist") or metadata.get("artist"), metadata.get("album"), metadata.get("year"), now, now))
        self.connection.execute("INSERT OR IGNORE INTO album_track(album_key,track_key,disc,track_number) VALUES(?,?,?,?)", (album_key, track_key, metadata.get("disc"), metadata.get("track")))
        return track_key, album_key

    def persist_targeted_successor_observation(self, replacement_id, job, verified, now):
        """Persist one immutable, AudioLibrary-backed successor observation.

        This intentionally does not create or alter a full catalog generation.
        It only establishes the successor identities needed by one accepted
        replacement, after Kodi has scanned the exact directory.
        """
        evidence = json.dumps(verified['evidence'], sort_keys=True)
        with self.connection:
            for row in verified['tracks']:
                track_key, unused_album_key = self.ensure_identity(row)
                if track_key != row['track_key']:
                    raise RuntimeError('targeted successor identity changed during persistence')
            self.connection.execute('DELETE FROM targeted_successor_track WHERE replacement_id=?', (replacement_id,))
            self.connection.execute('DELETE FROM targeted_successor_mapping WHERE replacement_id=?', (replacement_id,))
            self.connection.executemany("""INSERT INTO targeted_successor_track(replacement_id,track_key,kodi_song_dbid,current_path,release_id,recording_id,disc,track_number,artist,title,duration_ms)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""", [
                    (replacement_id, row['track_key'], row.get('kodi_dbid'), row.get('file'), row.get('musicbrainz_release_id'),
                     row.get('musicbrainz_recording_id'), row.get('disc'), row.get('track'), row.get('artist'), row.get('title'), row.get('duration_ms'))
                    for row in verified['tracks']])
            self.connection.executemany("""INSERT INTO targeted_successor_mapping(replacement_id,predecessor_track_key,successor_track_key,mapping_json)
                VALUES(?,?,?,?)""", [(replacement_id, item['predecessor_track_key'], item['successor_track_key'],
                json.dumps(item['mapping'], sort_keys=True)) for item in verified['mappings']])
            self.connection.execute("""UPDATE targeted_successor_observation SET state='verified',predecessor_generation_id=?,
                successor_album_key=?,successor_release_id=?,catalog_fingerprint=?,expected_path=?,current_path=?,kodi_album_dbid=?,
                verification_method='targeted_audio_library_scan',scan_completed_at=COALESCE(scan_completed_at,?),verified_at=?,
                retry_after=NULL,error_message=NULL,evidence_json=? WHERE replacement_id=?""",
                (verified['predecessor_generation_id'], verified['album_key'], verified['release_id'], verified['fingerprint'],
                 verified['expected_path'], verified['current_path'], verified.get('kodi_album_dbid'), now, now, evidence, replacement_id))

    def targeted_metadata_for_playback(self, file_name):
        """Return verified release metadata for one prepared successor file."""
        row = self.connection.execute("""SELECT t.*,o.state,o.replacement_id
            FROM targeted_successor_track t JOIN targeted_successor_observation o USING(replacement_id)
            WHERE lower(t.current_path)=lower(?) ORDER BY o.verified_at DESC LIMIT 1""", (file_name or '',)).fetchone()
        if row is not None and row['state'] == 'verified':
            return {'state': 'verified', 'replacement_id': row['replacement_id'], 'metadata': {
                'file': row['current_path'], 'kodi_dbid': row['kodi_song_dbid'], 'musicbrainz_release_id': row['release_id'],
                'musicbrainz_recording_id': row['recording_id'], 'disc': row['disc'], 'track': row['track_number'],
                'artist': row['artist'], 'title': row['title'], 'duration_ms': row['duration_ms']}}
        pending = self.connection.execute("""SELECT replacement_id FROM targeted_successor_observation
            WHERE state IN ('requested','scanning') AND lower(?) LIKE lower(scan_directory) || '%' LIMIT 1""", (file_name or '',)).fetchone()
        return {'state': 'pending', 'replacement_id': pending['replacement_id']} if pending else {'state': 'none'}

    def defer_qualifying_play(self, session_key, replacement_id, metadata, listened_seconds, threshold_seconds, occurred_at=None):
        now = int(time.time())
        with self.connection:
            self.connection.execute("""INSERT OR IGNORE INTO deferred_qualifying_play(session_key,replacement_id,metadata_json,listened_seconds,threshold_seconds,occurred_at,created_at)
                VALUES(?,?,?,?,?,?,?)""", (session_key, replacement_id, json.dumps(metadata, sort_keys=True), listened_seconds,
                threshold_seconds, int(occurred_at or now), now))

    def flush_deferred_qualifying_plays(self):
        """Commit retained first plays only after their successor is verified."""
        rows = self.connection.execute("SELECT * FROM deferred_qualifying_play ORDER BY created_at").fetchall()
        committed = 0
        for row in rows:
            try:
                metadata = json.loads(row['metadata_json'])
                prepared = self.targeted_metadata_for_playback(metadata.get('file'))
                if prepared['state'] != 'verified' or prepared['replacement_id'] != row['replacement_id']:
                    continue
                resolved = dict(metadata); resolved.update({k: v for k, v in prepared['metadata'].items() if v not in (None, '')})
                if self.record_live_play(row['session_key'], resolved, row['listened_seconds'], row['threshold_seconds'], row['occurred_at']):
                    committed += 1
                with self.connection:
                    self.connection.execute("DELETE FROM deferred_qualifying_play WHERE session_key=?", (row['session_key'],))
            except Exception:
                # Leave the durable session evidence for the next safe retry.
                continue
        return committed

    def metadata_album_context(self, metadata):
        """Return the Schema-13 metadata album key for one current item."""
        if self.schema_version() < 13:
            return None
        album_key, unused_artist, unused_album = metadata_album_identity(
            metadata.get('album_artist'), metadata.get('artist'), metadata.get('album'))
        return album_key

    def metadata_album_membership_needs_refresh(self, metadata):
        """Check one album/track only; never enumerate the Kodi library."""
        album_key = self.metadata_album_context(metadata)
        primary, artist, album, title, unused = metadata_track_identity(
            metadata.get('artist'), metadata.get('album'), metadata.get('title'))
        if not album_key or not primary:
            return False
        rows = self.connection.execute("""SELECT m.track_key,t.normalized_artist,t.normalized_album,
                    t.normalized_title,t.slot_discriminator FROM metadata_album_membership m
                JOIN metadata_track t ON t.track_key=m.track_key WHERE m.album_key=?""", (album_key,)).fetchall()
        if not rows:
            return True
        # A primary match is sufficient unless the cache already marks that
        # title as a same-album duplicate; that conservative case refreshes the
        # one album before it can contribute to an occurrence.
        matches = [row for row in rows if row['normalized_artist'] == artist and
                   row['normalized_album'] == album and row['normalized_title'] == title]
        return not matches or any(row['slot_discriminator'] is not None for row in matches)

    def replace_metadata_album_membership(self, metadata, songs, source_album_dbid=None):
        """Replace one metadata album's musical-slot membership from Kodi rows.

        ``songs`` is the bounded result of one AudioLibrary.GetSongs(albumid)
        request.  Physical copies collapse because only normalized metadata
        track keys are retained; the caller never supplies a library-wide list.
        """
        album_key = self.metadata_album_context(metadata)
        if not album_key:
            return 0
        prepared = []
        for song in songs or ():
            row_album_key, unused_artist, unused_album = metadata_album_identity(
                song.get('album_artist'), song.get('artist'), song.get('album'))
            primary, artist, album, title, unused = metadata_track_identity(
                song.get('artist'), song.get('album'), song.get('title'))
            if row_album_key != album_key or not primary:
                continue
            prepared.append((song, artist, album, title))
        if not prepared:
            return 0
        groups = {}
        for song, artist, album, title in prepared:
            groups.setdefault((artist, album, title), set()).add(
                (int(song.get('disc') or 0), int(song.get('track') or 0)))
        now = int(time.time())
        keys = []
        with self.connection:
            self.connection.execute("DELETE FROM metadata_album_membership WHERE album_key=?", (album_key,))
            for song, artist, album, title in prepared:
                artist_key, normalized_artist = metadata_artist_identity(song.get('artist'))
                row_album_key, normalized_album_artist, normalized_album = metadata_album_identity(
                    song.get('album_artist'), song.get('artist'), song.get('album'))
                require_slot = len(groups[(artist, album, title)]) > 1
                track_key, unused_a, unused_b, unused_c, slot = metadata_track_identity(
                    song.get('artist'), song.get('album'), song.get('title'),
                    song.get('disc'), song.get('track'), require_slot)
                self.connection.execute("INSERT OR IGNORE INTO metadata_artist(artist_key,normalized_artist,display_artist) VALUES(?,?,?)",
                                        (artist_key, normalized_artist, song.get('artist') or ''))
                self.connection.execute("INSERT OR IGNORE INTO metadata_album(album_key,artist_key,normalized_artist,normalized_album,display_artist,display_album,preferred_artwork_json,kodi_album_dbid,year) VALUES(?,?,?,?,?,?,?,?,?)",
                                        (row_album_key, artist_key, normalized_album_artist, normalized_album,
                                         song.get('album_artist') or song.get('artist') or '', song.get('album') or '',
                                         song.get('artwork_json'), source_album_dbid, song.get('year')))
                self.connection.execute("INSERT OR IGNORE INTO metadata_track(track_key,artist_key,album_key,normalized_artist,normalized_album,normalized_title,slot_discriminator,display_artist,display_album,display_title,preferred_path,preferred_artwork_json,disc,track_number) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                        (track_key, artist_key, row_album_key, artist, album, title, slot,
                                         song.get('artist') or '', song.get('album') or '', song.get('title') or '',
                                         song.get('file'), song.get('artwork_json'), song.get('disc'), song.get('track')))
                self.connection.execute("INSERT OR REPLACE INTO metadata_album_membership(album_key,track_key,source_album_dbid,refreshed_at) VALUES(?,?,?,?)",
                                        (album_key, track_key, source_album_dbid, now))
                keys.append(track_key)
        return len(set(keys))

    def end_metadata_album_occurrence(self, occurrence_id, ended_at=None):
        if not occurrence_id or self.schema_version() < 13:
            return
        now = int(ended_at or time.time())
        with self.connection:
            self.connection.execute("UPDATE metadata_album_session SET state='ended',ended_at=?,last_activity_at=? WHERE occurrence_id=? AND state='open'",
                                    (now, now, occurrence_id))

    def _record_metadata_album_occurrence_track(self, occurrence, metadata_track_key, now):
        """Apply the unchanged 40%-with-three-minimum album rule lazily."""
        if not occurrence or not metadata_track_key:
            return False
        occurrence_id = occurrence.get('id')
        album_key = occurrence.get('metadata_album_key')
        if not occurrence_id or not album_key:
            return False
        membership = self.connection.execute("SELECT 1 FROM metadata_album_membership WHERE album_key=? AND track_key=?",
                                             (album_key, metadata_track_key)).fetchone()
        if membership is None:
            return False
        total = self.connection.execute("SELECT count(*) FROM metadata_album_membership WHERE album_key=?", (album_key,)).fetchone()[0]
        if not total:
            return False
        required = min(total, max(3, int((total * 0.40 + 0.999999))))
        self.connection.execute("""INSERT OR IGNORE INTO metadata_album_session(
            occurrence_id,metadata_album_key,started_at,last_activity_at,distinct_tracks,required_tracks,state)
            VALUES(?,?,?,?,0,?,'open')""", (occurrence_id, album_key,
                                               int(occurrence.get('started_at') or now), now, required))
        self.connection.execute("INSERT OR IGNORE INTO metadata_album_session_track(occurrence_id,track_key) VALUES(?,?)",
                                (occurrence_id, metadata_track_key))
        distinct = self.connection.execute("SELECT count(*) FROM metadata_album_session_track WHERE occurrence_id=?", (occurrence_id,)).fetchone()[0]
        row = self.connection.execute("SELECT qualified_at FROM metadata_album_session WHERE occurrence_id=?", (occurrence_id,)).fetchone()
        self.connection.execute("UPDATE metadata_album_session SET distinct_tracks=?,last_activity_at=? WHERE occurrence_id=?",
                                (distinct, now, occurrence_id))
        if row is not None and row['qualified_at'] is None and distinct >= required:
            self.connection.execute("UPDATE metadata_album_session SET qualified_at=? WHERE occurrence_id=? AND qualified_at IS NULL",
                                    (now, occurrence_id))
            self.connection.execute("INSERT OR IGNORE INTO metadata_album_occurrence(occurrence_id,metadata_album_key,qualified_at) VALUES(?,?,?)",
                                    (occurrence_id, album_key, now))
            return True
        return False

    def resolve_current_album_membership(self, metadata):
        """Return one exact current logical album for this physical playback.

        The lookup deliberately follows a physical catalog identity, never a
        broad artist/title logical-song key.  A track that is represented by
        more than one current album is therefore not assigned speculatively.
        """
        if self.schema_version() < 9:
            return None
        generation = self.authoritative_catalog_generation_id()
        if generation is not None:
            candidates = set()
            file_name = metadata.get('file') or ''
            if file_name and not file_name.casefold().startswith('musicdb://'):
                rows = self.connection.execute("""SELECT DISTINCT m.logical_album_key
                    FROM catalog_logical_album_track m JOIN catalog_track_snapshot c
                      ON c.generation_id=m.generation_id AND c.track_key=m.track_key
                    WHERE m.generation_id=? AND lower(c.current_path)=lower(?)""",
                    (generation, file_name)).fetchall()
                candidates.update(row['logical_album_key'] for row in rows)
            if not candidates:
                key, unused_kind = track_identity(metadata)
                rows = self.connection.execute("""SELECT DISTINCT logical_album_key
                    FROM catalog_logical_album_track WHERE generation_id=? AND track_key=?""",
                    (generation, key)).fetchall()
                candidates.update(row['logical_album_key'] for row in rows)
            return next(iter(candidates)) if len(candidates) == 1 else None
        candidates = set()
        file_name = metadata.get('file') or ''
        if file_name and not file_name.casefold().startswith('musicdb://'):
            rows = self.connection.execute("""SELECT DISTINCT m.logical_album_key
                FROM logical_album_current_track m JOIN track_identity i ON i.identity_key=m.track_key
                WHERE i.canonical_path=?""", (file_name,)).fetchall()
            candidates.update(row['logical_album_key'] for row in rows)
        if not candidates:
            track_key, unused_kind = track_identity(metadata)
            rows = self.connection.execute(
                "SELECT DISTINCT logical_album_key FROM logical_album_current_track WHERE track_key=?",
                (track_key,)).fetchall()
            candidates.update(row['logical_album_key'] for row in rows)
        return next(iter(candidates)) if len(candidates) == 1 else None

    def end_album_occurrence(self, occurrence_id, ended_at=None):
        """Close a persisted prospective occurrence; no history is rewritten."""
        if not occurrence_id or self.schema_version() < 9:
            return
        now = int(ended_at or time.time())
        with self.connection:
            self.connection.execute("""UPDATE album_play_occurrence
                SET state='ended',ended_at=?,last_activity_at=?
                WHERE occurrence_id=? AND state='open'""", (now, now, occurrence_id))

    def _album_occurrence_member_key(self, logical_album_key, track_key, logical_song_key, file_name):
        """Return the verified current member key for one occurrence contribution.

        The normal path is the event's own physical key.  Music -> Files can
        legitimately create a path-based event identity when the current
        catalog has an MBID-based identity for the same exact file.  Only in
        that direct-key miss case may the in-memory exact-path catalog index
        supply one unique member, and that member must project to the same
        logical song.
        """
        generation = self.authoritative_catalog_generation_id()
        if generation is not None:
            member = self.connection.execute("""SELECT 1 FROM catalog_logical_album_track m
                JOIN catalog_logical_projection p ON p.generation_id=m.generation_id AND p.track_key=m.track_key
                WHERE m.generation_id=? AND m.logical_album_key=? AND m.track_key=? AND p.eligibility='eligible'
                  AND p.logical_song_key=?""", (generation, logical_album_key, track_key, logical_song_key)).fetchone()
            if member is not None:
                return track_key
            state, candidate = self.catalog_metadata_for_playback_path(file_name)
            if state != 'exact' or not candidate.get('track_key'):
                return None
            candidate_key = candidate['track_key']
            projection = self.connection.execute("""SELECT p.eligibility,p.logical_song_key
                FROM catalog_logical_album_track m JOIN catalog_logical_projection p
                  ON p.generation_id=m.generation_id AND p.track_key=m.track_key
                WHERE m.generation_id=? AND m.logical_album_key=? AND m.track_key=?""",
                (generation, logical_album_key, candidate_key)).fetchone()
            if projection is not None and projection['eligibility']=='eligible' and projection['logical_song_key']==logical_song_key:
                return candidate_key
            return None
        member = self.connection.execute("""SELECT 1
            FROM logical_album_current_track m JOIN logical_song_projection p ON p.track_key=m.track_key
            WHERE m.logical_album_key=? AND m.track_key=? AND p.eligibility='eligible'
              AND p.logical_song_key=?""", (logical_album_key, track_key, logical_song_key)).fetchone()
        if member is not None:
            return track_key

        try:
            state, candidate = self.catalog_metadata_for_playback_path(file_name)
        except Exception as exc:
            self.log("album occurrence membership fallback lookup failed: %s" % exc)
            return None
        if state == 'missing':
            self.log("album occurrence membership fallback: direct track_key miss; no exact path member")
            return None
        if state == 'ambiguous':
            self.log("album occurrence membership fallback: direct track_key miss; ambiguous exact path")
            return None
        candidate_key = candidate.get('track_key') if state == 'exact' else None
        if not candidate_key:
            self.log("album occurrence membership fallback: direct track_key miss; exact path member unavailable")
            return None
        projection = self.connection.execute("""SELECT p.eligibility,p.logical_song_key
            FROM logical_album_current_track m JOIN logical_song_projection p ON p.track_key=m.track_key
            WHERE m.logical_album_key=? AND m.track_key=?""", (logical_album_key, candidate_key)).fetchone()
        if projection is None or projection['eligibility'] != 'eligible' or not projection['logical_song_key']:
            self.log("album occurrence membership fallback: direct track_key miss; logical projection unavailable")
            return None
        if projection['logical_song_key'] != logical_song_key:
            self.log("album occurrence membership fallback: direct track_key miss; logical-song mismatch")
            return None
        self.log("album occurrence membership fallback: direct track_key miss; exact path match; logical-song verified; catalog_track_key=%s" % candidate_key)
        return candidate_key

    def _record_album_occurrence_track(self, occurrence, track_key, logical_song_key, now, file_name=None):
        """Attach one qualifying logical track and award one play at threshold."""
        if not occurrence or not logical_song_key:
            return False
        occurrence_id = occurrence.get('id')
        logical_album_key = occurrence.get('logical_album_key')
        if not occurrence_id or not logical_album_key:
            return False
        member_key = self._album_occurrence_member_key(logical_album_key, track_key, logical_song_key, file_name)
        if member_key is None:
            return False
        generation = self.authoritative_catalog_generation_id()
        if generation is not None:
            required = self.connection.execute("""SELECT count(DISTINCT p.logical_song_key)
                FROM catalog_logical_album_track m JOIN catalog_logical_projection p
                  ON p.generation_id=m.generation_id AND p.track_key=m.track_key
                WHERE m.generation_id=? AND m.logical_album_key=? AND p.eligibility='eligible'""",
                (generation, logical_album_key)).fetchone()[0]
        else:
            required = self.connection.execute("""SELECT count(DISTINCT p.logical_song_key)
                FROM logical_album_current_track m JOIN logical_song_projection p ON p.track_key=m.track_key
                WHERE m.logical_album_key=? AND p.eligibility='eligible'""", (logical_album_key,)).fetchone()[0]
        if not required:
            return False
        required = min(required, max(3, int((required * 0.40 + 0.999999))))
        self.connection.execute("""INSERT OR IGNORE INTO album_play_occurrence(
            occurrence_id,logical_album_key,started_at,last_activity_at,distinct_tracks,required_tracks,state)
            VALUES(?,?,?,?,0,?,'open')""", (occurrence_id, logical_album_key,
                                                int(occurrence.get('started_at') or now), now, required))
        self.connection.execute("""INSERT OR IGNORE INTO album_play_occurrence_track(occurrence_id,logical_song_key)
            VALUES(?,?)""", (occurrence_id, logical_song_key))
        distinct = self.connection.execute(
            "SELECT count(*) FROM album_play_occurrence_track WHERE occurrence_id=?", (occurrence_id,)
        ).fetchone()[0]
        row = self.connection.execute(
            "SELECT qualified_at FROM album_play_occurrence WHERE occurrence_id=?", (occurrence_id,)
        ).fetchone()
        self.connection.execute("UPDATE album_play_occurrence SET distinct_tracks=?,last_activity_at=? WHERE occurrence_id=?",
                                (distinct, now, occurrence_id))
        if row is not None and row['qualified_at'] is None and distinct >= required:
            self.connection.execute("UPDATE album_play_occurrence SET qualified_at=? WHERE occurrence_id=? AND qualified_at IS NULL",
                                    (now, occurrence_id))
            return True
        return False

    def record_live_play(self, session_key, metadata, listened_seconds, threshold_seconds, occurred_at=None, album_occurrence=None):
        """Atomically commit one session at most once. Returns True only for a new event."""
        event_key = "live:" + session_key
        now = int(occurred_at or time.time())
        with self.connection:
            track_key, album_key = self.ensure_identity(metadata)
            generation = self.authoritative_catalog_generation_id()
            if generation is None:
                logical_key = self.ensure_logical_projection(track_key, metadata)
            else:
                logical_key = self.resolve_authoritative_logical_song_for_live_event(
                    track_key, metadata.get('file'), generation)
                # Schema 12 playback consumes its committed projection only.  A
                # current-catalog miss remains excluded; playback never creates
                # a new candidate identity or mutates catalog-derived state.
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO play_event(event_key,track_key,album_key,source,play_count,listened_seconds,threshold_seconds,occurred_at,session_key) VALUES(?,?,?,?,?,?,?,?,?)",
                (event_key, track_key, album_key, "live_tracking", 1, listened_seconds, threshold_seconds, now, session_key))
            if cursor.rowcount != 1:
                return False
            self.connection.execute(
                """INSERT INTO track_totals(track_key,observed_plays,total_plays,last_played_at) VALUES(?,1,1,?)
                   ON CONFLICT(track_key) DO UPDATE SET observed_plays=observed_plays+1,total_plays=total_plays+1,last_played_at=excluded.last_played_at""",
                (track_key, now))
            if logical_key:
                if generation is not None:
                    self.connection.execute("""INSERT INTO catalog_logical_totals(
                        generation_id,logical_song_key,observed_plays,total_plays,last_played_at)
                        VALUES(?,?,1,1,?) ON CONFLICT(generation_id,logical_song_key) DO UPDATE SET
                        observed_plays=observed_plays+1,total_plays=total_plays+1,last_played_at=excluded.last_played_at""",
                        (generation, logical_key, now))
                else:
                    self.connection.execute(
                        """INSERT INTO logical_song_totals(logical_song_key,observed_plays,total_plays,last_played_at) VALUES(?,1,1,?)
                           ON CONFLICT(logical_song_key) DO UPDATE SET observed_plays=observed_plays+1,total_plays=total_plays+1,last_played_at=excluded.last_played_at""",
                        (logical_key, now))
                if self._record_album_occurrence_track(album_occurrence, track_key, logical_key, now, metadata.get('file')):
                    # The newly qualified occurrence changes album rank only.
                    self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('rankings_dirty','1')")
            self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('rankings_dirty','1')")
            if self.schema_version() >= 13:
                metadata_track_key, unused_metadata_album_key = self._project_metadata_event(event_key)
                if self._record_metadata_album_occurrence_track(album_occurrence, metadata_track_key, now):
                    self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('rankings_dirty','1')")
        return True

    def rebuild_logical_album_current(self, source_root):
        """Derived current-folder membership for logical album ranking only."""
        if not source_root:
            return
        now = int(time.time())
        with self.connection:
            self._clear_logical_album_current()
            self._populate_logical_album_current(source_root, now)

    def recover_failed_catalog_generation(self, generation_id, source_root):
        """Promote one post-commit failed snapshot only after derived recovery."""
        row = self.connection.execute("SELECT state FROM catalog_generation WHERE generation_id=?", (generation_id,)).fetchone()
        if row is None or row['state'] != 'failed':
            raise RuntimeError('catalog generation is not a recoverable failed snapshot')
        snapshots = self.connection.execute("SELECT count(*) FROM catalog_track_snapshot WHERE generation_id=?", (generation_id,)).fetchone()[0]
        current = self.connection.execute("SELECT count(*) FROM album_track_catalog").fetchone()[0]
        albums = self.connection.execute("SELECT count(*) FROM catalog_album_snapshot WHERE generation_id=?", (generation_id,)).fetchone()[0]
        if not snapshots or not albums or snapshots != current:
            raise RuntimeError('catalog snapshot does not match current catalog state')
        # The projection transaction must succeed before the snapshot becomes trusted.
        self.rebuild_logical_projection(source_root, generation_id)
        with self.connection:
            cursor = self.connection.execute("UPDATE catalog_generation SET state='complete' WHERE generation_id=? AND state='failed'", (generation_id,))
            if cursor.rowcount != 1:
                raise RuntimeError('catalog generation state changed during recovery')

    def _wal_sample(self, label, samples):
        """Record a non-blocking WAL observation between candidate phases."""
        size = 0
        try:
            size = os.path.getsize(self.path + '-wal')
            # PASSIVE never waits for readers/writers; its return values explain
            # whether pages were reusable without imposing an exclusive stall.
            checkpoint = tuple(self.connection.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone())
        except (OSError, sqlite3.DatabaseError):
            checkpoint = None
        samples.append((label, size, checkpoint))
        self.log('catalog staged WAL %s bytes=%d checkpoint=%s' % (label, size, checkpoint))

    def _staged_transaction(self, phase, action, metrics, should_abort=None):
        """One measurable, playback-checkable bounded write transaction."""
        self._catalog_playback_checkpoint(should_abort, phase)
        started = time.monotonic()
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            action()
            commit_started = time.monotonic()
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        metrics['transactions'] += 1
        metrics['max_transaction_seconds'] = max(metrics['max_transaction_seconds'], time.monotonic() - started)
        metrics['max_commit_seconds'] = max(metrics['max_commit_seconds'], time.monotonic() - commit_started)
        self._catalog_playback_checkpoint(should_abort, phase + ' next batch')

    def _validate_candidate_catalog(self, generation_id, should_abort=None):
        """Prove a building generation is internally complete without promotion."""
        self._catalog_playback_checkpoint(should_abort, 'validation')
        candidate = self.connection.execute(
            'SELECT state FROM catalog_generation WHERE generation_id=?', (generation_id,)).fetchone()
        if candidate is None or candidate['state'] != 'building':
            raise RuntimeError('candidate validation requires a building generation')
        tracks = self.connection.execute(
            'SELECT count(*) FROM catalog_track_snapshot WHERE generation_id=?', (generation_id,)).fetchone()[0]
        self._catalog_playback_checkpoint(should_abort, 'validation track count')
        albums = self.connection.execute(
            'SELECT count(*) FROM catalog_album_snapshot WHERE generation_id=?', (generation_id,)).fetchone()[0]
        scoped = self._candidate_table_counts(generation_id)
        self._catalog_playback_checkpoint(should_abort, 'validation scoped counts')
        if not tracks or not albums or scoped['catalog_logical_projection'] != self.connection.execute(
                'SELECT count(*) FROM track_identity').fetchone()[0]:
            raise RuntimeError('candidate catalog is incomplete')
        raw = self.connection.execute('SELECT coalesce(sum(play_count),0) FROM play_event').fetchone()[0]
        self._catalog_playback_checkpoint(should_abort, 'validation raw accounting')
        logical = self.connection.execute('SELECT coalesce(sum(total_plays),0) FROM catalog_logical_totals WHERE generation_id=?',
                                          (generation_id,)).fetchone()[0]
        excluded = self.connection.execute("""SELECT coalesce(sum(e.play_count),0)
            FROM play_event e JOIN catalog_logical_projection p ON p.track_key=e.track_key
            WHERE p.generation_id=? AND p.eligibility='excluded'""", (generation_id,)).fetchone()[0]
        self._catalog_playback_checkpoint(should_abort, 'validation exclusion accounting')
        invalid = self.connection.execute("""SELECT count(*) FROM play_event e
            LEFT JOIN catalog_logical_projection p ON p.generation_id=? AND p.track_key=e.track_key
            WHERE p.track_key IS NULL OR (p.eligibility='eligible' AND p.logical_song_key IS NULL)""",
            (generation_id,)).fetchone()[0]
        self._catalog_playback_checkpoint(should_abort, 'validation projection accounting')
        if invalid or raw != logical + excluded:
            raise RuntimeError('candidate logical accounting is invalid')
        return {'tracks': tracks, 'albums': albums, 'raw': raw, 'logical': logical,
                'excluded': excluded, 'scoped': scoped}

    def _promote_candidate_generation(self, generation_id, previous_generation, requested_refresh, metrics,
                                      should_abort=None, promotion_hook=None):
        """Only authority metadata moves in this small, atomic transaction."""
        def promote():
            if promotion_hook:
                promotion_hook('after_begin')
            row = self.connection.execute('SELECT state FROM catalog_generation WHERE generation_id=?',
                                          (generation_id,)).fetchone()
            if row is None or row['state'] != 'building':
                raise RuntimeError('candidate changed before promotion')
            if previous_generation is not None:
                self.connection.execute("UPDATE catalog_generation SET state='superseded' "
                                        "WHERE generation_id=? AND state='complete'", (previous_generation,))
            if promotion_hook:
                promotion_hook('after_previous_superseded')
            changed = self.connection.execute("UPDATE catalog_generation SET completed_at=?,state='complete' "
                                               "WHERE generation_id=? AND state='building'",
                                              (int(time.time()), generation_id)).rowcount
            if changed != 1:
                raise RuntimeError('candidate promotion lost compare-and-set')
            self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('catalog_updated_at',?)",
                                    (str(int(time.time())),))
            self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('rankings_dirty','1')")
            # Promotion and cleanup are intentionally distinct.  This durable
            # marker lets cleanup yield to playback without ever questioning
            # the already committed authoritative generation.
            self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('catalog_cleanup_pending_generation',?)",
                                    (str(generation_id),))
            if requested_refresh:
                self.connection.execute("DELETE FROM schema_meta WHERE key='catalog_refresh_requested_at'")
        if promotion_hook:
            promotion_hook('before_begin')
        self._staged_transaction('promotion', promote, metrics, should_abort)
        if promotion_hook:
            promotion_hook('after_commit')

    def catalog_cleanup_pending_generation(self):
        row = self.connection.execute("SELECT value FROM schema_meta WHERE key='catalog_cleanup_pending_generation'").fetchone()
        return int(row['value']) if row is not None else None

    def _cleanup_obsolete_candidate_generations(self, current_generation, metrics, should_abort=None,
                                                batch_size=500):
        """Idempotently remove obsolete bulk rows in playback-checkable batches."""
        tables = ('catalog_logical_album_track', 'catalog_logical_album_song', 'catalog_logical_album',
                  'catalog_logical_totals', 'catalog_logical_projection', 'catalog_logical_song',
                  'catalog_track_snapshot', 'catalog_album_snapshot')
        for table in tables:
            while True:
                self._catalog_playback_checkpoint(should_abort, 'cleanup:' + table)
                removed = [0]
                def remove(table=table):
                    cursor = self.connection.execute(
                        'DELETE FROM %s WHERE rowid IN (SELECT rowid FROM %s WHERE generation_id<>? LIMIT ?)' %
                        (table, table), (current_generation, batch_size))
                    removed[0] = cursor.rowcount
                self._staged_transaction('cleanup:%s batch' % table, remove, metrics, should_abort)
                if removed[0] < batch_size:
                    break

    def resume_catalog_cleanup(self, should_abort=None, phase_callback=None, batch_size=500):
        """Resume deferred post-promotion pruning without changing authority."""
        generation = self.catalog_cleanup_pending_generation()
        if generation is None:
            return None
        current = self.latest_current_catalog_generation()
        if current is None or current['generation_id'] != generation:
            raise RuntimeError('catalog cleanup pending generation is not authoritative')
        if phase_callback:
            phase_callback('Cleaning')
        metrics = {'transactions': 0, 'max_transaction_seconds': 0.0,
                   'max_commit_seconds': 0.0}
        self._cleanup_obsolete_candidate_generations(generation, metrics, should_abort, batch_size)
        with self.connection:
            self.connection.execute("DELETE FROM schema_meta WHERE key='catalog_cleanup_pending_generation' AND value=?",
                                    (str(generation),))
        self._rebuild_catalog_path_index()
        self.log('catalog staged cleanup complete for generation %d' % generation)
        return metrics

    def _replace_album_catalog_staged_contents(self, rows, requested_refresh=False, source_root=None, should_abort=None,
                                               batch_size=500, phase_callback=None):
        """Schema-12 candidate build, validate, promote, then cleanup.

        The candidate's expensive data is durable before authority moves.  The
        promotion transaction contains no catalog/projection bulk writes.
        """
        started = time.monotonic()
        metrics = {'transactions': 0, 'max_transaction_seconds': 0.0,
                   'max_commit_seconds': 0.0, 'phases': {}, 'wal': []}
        def phase(name):
            if phase_callback:
                phase_callback(name)
            self.log('catalog staged phase %s' % name)
        phase('Writing')
        self._wal_sample('start', metrics['wal'])
        with self.connection:
            previous = self.latest_current_catalog_generation()
            generation = self.connection.execute("INSERT INTO catalog_generation(started_at,state,source) VALUES(?,?,?)",
                                                (int(time.time()), 'building', 'kodi_audio_library')).lastrowid
        self._wal_sample('candidate-created', metrics['wal'])
        by_album = {}
        prepared = []
        for metadata in rows:
            if should_abort and len(prepared) % 250 == 0 and should_abort():
                raise CatalogRefreshAborted('media playback started during staged catalog preparation')
            track_key, unused_track_kind = track_identity(metadata)
            album_key, unused_album_kind = album_identity(metadata)
            prepared.append((metadata, track_key, album_key))
            by_album.setdefault(album_key, []).append(metadata)
        track_started = time.monotonic()
        for start in range(0, len(prepared), batch_size):
            batch = prepared[start:start + batch_size]
            def write_tracks(batch=batch):
                for metadata, unused_track_key, unused_album_key in batch:
                    self.ensure_identity(metadata)
                self.connection.executemany("""INSERT INTO catalog_track_snapshot(
                    generation_id,album_key,track_key,release_id,disc,track_number,recording_id,artist,title,
                    duration_ms,current_path,kodi_song_dbid) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", [
                    (generation, album_key, track_key, metadata.get('musicbrainz_release_id'), metadata.get('disc'),
                     metadata.get('track'), metadata.get('musicbrainz_recording_id'), metadata.get('artist'),
                     metadata.get('title'), metadata.get('duration_ms'), metadata.get('file'), metadata.get('kodi_dbid'))
                    for metadata, track_key, album_key in batch])
            self._staged_transaction('track-snapshot', write_tracks, metrics, should_abort)
        metrics['phases']['track_snapshots_seconds'] = time.monotonic() - track_started
        self._wal_sample('track-snapshots', metrics['wal'])
        phase('WritingAlbums')
        album_started = time.monotonic()
        album_rows = []
        for album_index, (album_key, members) in enumerate(by_album.items()):
            if should_abort and album_index % 250 == 0 and should_abort():
                raise CatalogRefreshAborted('media playback started during album snapshot preparation')
            first = members[0]
            release_id = first.get('musicbrainz_release_id')
            try:
                fingerprint = catalog_fingerprint(first.get('album_artist') or first.get('artist'), first.get('album'),
                                                  release_id, int(first.get('year') or 0), members) if release_id else None
            except (TypeError, ValueError):
                fingerprint = None
            source_root_id, expected_path = None, None
            if source_root:
                relative_files = [relative_path(item.get('file'), source_root.get('physical_source_root')) for item in members]
                if all(relative_files):
                    source_root_id = source_root.get('source_root_id')
                    expected_path = common_album_path(relative_files)
            album_rows.append((generation, album_key, release_id, source_root_id, expected_path, first.get('file'),
                               fingerprint, len(members), first.get('album_artist') or first.get('artist'),
                               first.get('album'), first.get('year')))
        for start in range(0, len(album_rows), batch_size):
            batch = album_rows[start:start + batch_size]
            self._staged_transaction('album-snapshot', lambda batch=batch: self.connection.executemany(
                """INSERT INTO catalog_album_snapshot(generation_id,album_key,release_id,source_root_id,expected_path,
                    current_path,catalog_fingerprint,track_count,artist,title,original_year) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                batch), metrics, should_abort)
        metrics['phases']['album_snapshots_seconds'] = time.monotonic() - album_started
        self._wal_sample('album-snapshots', metrics['wal'])
        phase('Projecting')
        projection_started = time.monotonic()
        projection = self._build_candidate_logical_projection(generation, source_root, should_abort, batch_size)
        metrics['phases']['logical_projection_seconds'] = time.monotonic() - projection_started
        metrics['candidate_projection'] = projection
        self._wal_sample('logical-projection', metrics['wal'])
        phase('Validating')
        validation_started = time.monotonic()
        metrics['validation'] = self._validate_candidate_catalog(generation, should_abort)
        metrics['phases']['validation_seconds'] = time.monotonic() - validation_started
        self._wal_sample('validation', metrics['wal'])
        phase('Promoting')
        promotion_started = time.monotonic()
        promotion_metrics = {'transactions': 0, 'max_transaction_seconds': 0.0, 'max_commit_seconds': 0.0}
        try:
            self._promote_candidate_generation(generation, previous[0] if previous else None, requested_refresh,
                                               promotion_metrics, should_abort)
        except CatalogRefreshAborted as exc:
            state = self.connection.execute('SELECT state FROM catalog_generation WHERE generation_id=?',
                                            (generation,)).fetchone()
            if state is None or state['state'] != 'complete':
                raise
            # The commit succeeded; only later maintenance must yield.  The
            # durable cleanup marker was committed with authority promotion.
            metrics['cleanup_deferred'] = str(exc)
            self.log('catalog staged generation %d promoted; cleanup deferred: %s' %
                     (generation, exc))
            return generation, metrics
        metrics['phases']['promotion_seconds'] = time.monotonic() - promotion_started
        metrics['promotion'] = promotion_metrics
        self._wal_sample('promotion', metrics['wal'])
        phase('Cleaning')
        cleanup_started = time.monotonic()
        cleanup_metrics = {'transactions': 0, 'max_transaction_seconds': 0.0, 'max_commit_seconds': 0.0}
        try:
            self._cleanup_obsolete_candidate_generations(generation, cleanup_metrics, should_abort,
                                                         batch_size)
        except CatalogRefreshAborted as exc:
            # Authority is already committed.  The durable cleanup marker is
            # retained and service idle maintenance resumes it later.
            metrics['cleanup_deferred'] = str(exc)
            self.log('catalog staged generation %d promoted; cleanup deferred: %s' %
                     (generation, exc))
            return generation, metrics
        metrics['phases']['cleanup_seconds'] = time.monotonic() - cleanup_started
        metrics['cleanup'] = cleanup_metrics
        self._wal_sample('cleanup', metrics['wal'])
        with self.connection:
            self.connection.execute("DELETE FROM schema_meta WHERE key='catalog_cleanup_pending_generation' AND value=?",
                                    (str(generation),))
        metrics['total_seconds'] = time.monotonic() - started
        self._rebuild_catalog_path_index()
        self.log('catalog staged generation %d complete in %.3fs' % (generation, metrics['total_seconds']))
        return generation, metrics

    def replace_album_catalog_staged(self, rows, requested_refresh=False, source_root=None, should_abort=None,
                                     batch_size=500, phase_callback=None):
        """Run staged work; an interrupted candidate never becomes authority."""
        try:
            return self._replace_album_catalog_staged_contents(
                rows, requested_refresh, source_root, should_abort, batch_size, phase_callback)
        except Exception:
            # Preserve request intent.  Mark only the newest still-building
            # candidate failed, so a later idle retry starts from a clear,
            # auditable lifecycle state.
            row = self.connection.execute("""SELECT generation_id FROM catalog_generation
                WHERE state='building' ORDER BY generation_id DESC LIMIT 1""").fetchone()
            if row is not None:
                with self.connection:
                    self.connection.execute("UPDATE catalog_generation SET state='failed',completed_at=? "
                                            "WHERE generation_id=? AND state='building'",
                                            (int(time.time()), row['generation_id']))
            raise

    def replace_album_catalog(self, rows, requested_refresh=False, source_root=None, should_abort=None):
        """Build one complete trusted snapshot without rewriting unchanged current metadata."""
        if self.schema_version() >= 12:
            return self.replace_album_catalog_staged(rows, requested_refresh, source_root, should_abort)
        now = int(time.time())
        started = time.monotonic()
        with self.connection:
            previous = self.latest_current_catalog_generation()
            generation = self.connection.execute("INSERT INTO catalog_generation(started_at,state,source) VALUES(?,?,?)", (now, "building", "kodi_audio_library")).lastrowid
        try:
            if should_abort and should_abort():
                raise CatalogRefreshAborted('media playback started before catalog preparation')
            current_tracks = {row[0]: tuple(row[1:]) for row in self.connection.execute("SELECT identity_key,identity_kind,recording_id,release_id,disc,track_number,canonical_path,artist,album,title,kodi_dbid_last_seen,recording_key,duration_ms FROM track_identity")}
            current_albums = {row[0]: tuple(row[1:]) for row in self.connection.execute("SELECT album_key,identity_kind,release_id,artist,title,year,kodi_album_dbid FROM album_identity")}
            current_memberships = set(tuple(row) for row in self.connection.execute("SELECT album_key,track_key FROM album_track"))
            current_catalog = {tuple(row[:2]): tuple(row[2:]) for row in self.connection.execute("SELECT album_key,track_key,kodi_song_dbid,disc,track_number FROM album_track_catalog")}
            track_writes = []
            album_writes = []
            membership_writes = []
            new_tracks = 0
            updated_tracks = 0
            new_albums = 0
            updated_albums = 0
            desired_catalog = {}
            by_album = {}
            track_snapshots = []
            for metadata in rows:
                if should_abort and len(track_snapshots) % 250 == 0 and should_abort():
                    raise CatalogRefreshAborted('media playback started during catalog preparation')
                track_key, track_kind = track_identity(metadata)
                recording_key, unused = recording_identity(metadata)
                album_key, album_kind = album_identity(metadata)
                artwork = metadata.get("artwork_json")
                existing_track = current_tracks.get(track_key)
                # Artwork is presentation data, not catalog-continuity data.  Existing
                # artwork remains untouched unless a new identity is being inserted.
                # Preserve an established identity kind for an existing durable key.
                effective_track_kind = existing_track[0] if existing_track else track_kind
                track_value = (effective_track_kind, metadata.get("musicbrainz_recording_id"), metadata.get("musicbrainz_release_id"), metadata.get("disc"), metadata.get("track"), metadata.get("file"), metadata.get("artist"), metadata.get("album"), metadata.get("title"), metadata.get("kodi_dbid"), recording_key or None, metadata.get("duration_ms"))
                if existing_track != track_value:
                    track_writes.append((track_key,) + track_value[:10] + (now, now) + track_value[10:] + (artwork if not existing_track else None,))
                    if existing_track:
                        updated_tracks += 1
                    else:
                        new_tracks += 1
                existing_album = current_albums.get(album_key)
                effective_album_kind = existing_album[0] if existing_album else album_kind
                year = metadata.get("year")
                normalized_year = str(year) if year not in (None, "") else None
                album_value = (effective_album_kind, metadata.get("musicbrainz_release_id"), metadata.get("album_artist") or metadata.get("artist"), metadata.get("album"), normalized_year, metadata.get("kodi_album_dbid"))
                if existing_album != album_value:
                    album_writes.append((album_key,) + album_value[:5] + (now, now) + album_value[5:] + (artwork if not existing_album else None,))
                    if existing_album:
                        updated_albums += 1
                    else:
                        new_albums += 1
                if (album_key, track_key) not in current_memberships:
                    membership_writes.append((album_key, track_key, metadata.get("disc"), metadata.get("track")))
                desired_catalog[(album_key, track_key)] = (metadata.get("kodi_dbid"), metadata.get("disc"), metadata.get("track"))
                by_album.setdefault(album_key, []).append(metadata)
                track_snapshots.append((generation, album_key, track_key, metadata.get("musicbrainz_release_id"), metadata.get("disc"), metadata.get("track"), metadata.get("musicbrainz_recording_id"), metadata.get("artist"), metadata.get("title"), metadata.get("duration_ms"), metadata.get("file"), metadata.get("kodi_dbid")))
            catalog_deletes = [key for key, value in current_catalog.items() if desired_catalog.get(key) != value]
            catalog_writes = [key + value for key, value in desired_catalog.items() if current_catalog.get(key) != value]
            album_snapshots = []
            for index, (album_key, members) in enumerate(by_album.items()):
                if should_abort and index % 100 == 0 and should_abort():
                    raise CatalogRefreshAborted('media playback started during album snapshot preparation')
                first = members[0]
                release_id = first.get("musicbrainz_release_id")
                fingerprint = None
                try:
                    if release_id:
                        fingerprint = catalog_fingerprint(first.get("album_artist") or first.get("artist"), first.get("album"), release_id, int(first.get("year") or 0), members)
                except (TypeError, ValueError):
                    pass
                source_root_id = None
                expected_path = None
                if source_root:
                    relative_files = [relative_path(item.get("file"), source_root.get("physical_source_root")) for item in members]
                    if all(relative_files):
                        source_root_id = source_root.get("source_root_id")
                        expected_path = common_album_path(relative_files)
                album_snapshots.append((generation, album_key, release_id, source_root_id, expected_path, first.get("file"), fingerprint, len(members), first.get("album_artist") or first.get("artist"), first.get("album"), first.get("year")))
            prepared = time.monotonic()
            self.log("catalog preparation complete: %d tracks, %d albums in %.3fs; writes tracks=%d (new=%d update=%d) albums=%d (new=%d update=%d) memberships=%d catalog_delete=%d catalog_write=%d" % (len(rows), len(by_album), prepared - started, len(track_writes), new_tracks, updated_tracks, len(album_writes), new_albums, updated_albums, len(membership_writes), len(catalog_deletes), len(catalog_writes)))
            with self.connection:
                phase = time.monotonic()
                if should_abort and should_abort():
                    raise CatalogRefreshAborted('media playback started before catalog write')
                self.connection.executemany("""INSERT INTO track_identity(identity_key,identity_kind,recording_id,release_id,disc,track_number,canonical_path,artist,album,title,kodi_dbid_last_seen,created_at,updated_at,recording_key,duration_ms,artwork_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(identity_key) DO UPDATE SET identity_kind=excluded.identity_kind,recording_id=excluded.recording_id,recording_key=excluded.recording_key,release_id=excluded.release_id,disc=excluded.disc,track_number=excluded.track_number,canonical_path=COALESCE(excluded.canonical_path,track_identity.canonical_path),artist=COALESCE(excluded.artist,track_identity.artist),album=COALESCE(excluded.album,track_identity.album),title=COALESCE(excluded.title,track_identity.title),kodi_dbid_last_seen=COALESCE(excluded.kodi_dbid_last_seen,track_identity.kodi_dbid_last_seen),duration_ms=COALESCE(excluded.duration_ms,track_identity.duration_ms),artwork_json=COALESCE(excluded.artwork_json,track_identity.artwork_json),updated_at=excluded.updated_at""", track_writes)
                self.log("catalog phase track identities %.3fs" % (time.monotonic() - phase)); phase = time.monotonic()
                if should_abort and should_abort():
                    raise CatalogRefreshAborted('media playback started after track identity write')
                self.connection.executemany("""INSERT INTO album_identity(album_key,identity_kind,release_id,artist,title,year,created_at,updated_at,kodi_album_dbid,artwork_json) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(album_key) DO UPDATE SET identity_kind=excluded.identity_kind,release_id=excluded.release_id,artist=COALESCE(excluded.artist,album_identity.artist),title=COALESCE(excluded.title,album_identity.title),year=COALESCE(excluded.year,album_identity.year),kodi_album_dbid=COALESCE(excluded.kodi_album_dbid,album_identity.kodi_album_dbid),artwork_json=COALESCE(excluded.artwork_json,album_identity.artwork_json),updated_at=excluded.updated_at""", album_writes)
                self.log("catalog phase album identities %.3fs" % (time.monotonic() - phase)); phase = time.monotonic()
                if should_abort and should_abort():
                    raise CatalogRefreshAborted('media playback started after album identity write')
                self.connection.executemany("INSERT INTO album_track(album_key,track_key,disc,track_number) VALUES(?,?,?,?)", membership_writes)
                self.log("catalog phase historical memberships %.3fs" % (time.monotonic() - phase)); phase = time.monotonic()
                if should_abort and should_abort():
                    raise CatalogRefreshAborted('media playback started after historical membership write')
                self.connection.executemany("DELETE FROM album_track_catalog WHERE album_key=? AND track_key=?", catalog_deletes)
                self.connection.executemany("INSERT INTO album_track_catalog(album_key,track_key,kodi_song_dbid,disc,track_number) VALUES(?,?,?,?,?)", catalog_writes)
                self.log("catalog phase current memberships %.3fs" % (time.monotonic() - phase)); phase = time.monotonic()
                if should_abort and should_abort():
                    raise CatalogRefreshAborted('media playback started after current membership write')
                self.connection.executemany("INSERT INTO catalog_album_snapshot(generation_id,album_key,release_id,source_root_id,expected_path,current_path,catalog_fingerprint,track_count,artist,title,original_year) VALUES(?,?,?,?,?,?,?,?,?,?,?)", album_snapshots)
                self.log("catalog phase album snapshots %.3fs" % (time.monotonic() - phase)); phase = time.monotonic()
                if should_abort and should_abort():
                    raise CatalogRefreshAborted('media playback started after album snapshot write')
                self.connection.executemany("INSERT INTO catalog_track_snapshot(generation_id,album_key,track_key,release_id,disc,track_number,recording_id,artist,title,duration_ms,current_path,kodi_song_dbid) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", track_snapshots)
                self.log("catalog phase track snapshots %.3fs" % (time.monotonic() - phase)); phase = time.monotonic()
                if should_abort and should_abort():
                    raise CatalogRefreshAborted('media playback started after track snapshot write')
                # Projection must succeed before this generation is promoted.
                # Keeping the whole promotion and prune sequence in this one
                # transaction means an interruption cannot discard the last
                # complete generation or leave a permanent full snapshot.
                self._rebuild_logical_projection_contents(source_root, generation, should_abort)
                self.log("catalog phase logical projection %.3fs" % (time.monotonic() - phase)); phase = time.monotonic()
                if should_abort and should_abort():
                    raise CatalogRefreshAborted('media playback started after logical projection rebuild')
                if previous:
                    self.connection.execute("UPDATE catalog_generation SET state='superseded' WHERE generation_id=? AND state='complete'", (previous[0],))
                self.connection.execute("UPDATE catalog_generation SET completed_at=?,state='complete' WHERE generation_id=?", (int(time.time()), generation))
                pruned = self._prune_catalog_snapshots_contents(generation)
                self.log("catalog phase snapshot prune tracks=%d albums=%d %.3fs" % (pruned[0], pruned[1], time.monotonic() - phase)); phase = time.monotonic()
                self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('catalog_updated_at',?)", (str(now),))
                self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('rankings_dirty','1')")
                if requested_refresh:
                    self.connection.execute("DELETE FROM schema_meta WHERE key='catalog_refresh_requested_at'")
                self.log("catalog phase completion metadata %.3fs" % (time.monotonic() - phase))
            self.log("catalog snapshot write complete in %.3fs" % (time.monotonic() - prepared))
            self.log("catalog generation %d complete in %.3fs" % (generation, time.monotonic() - started))
            self._rebuild_catalog_path_index()
        except Exception:
            with self.connection:
                self.connection.execute("UPDATE catalog_generation SET completed_at=?,state='failed' WHERE generation_id=?", (int(time.time()), generation))
            raise

    def catalog_track_count(self):
        return self.connection.execute("SELECT count(*) FROM album_track_catalog").fetchone()[0]

    def import_baseline_rows(self, rows, approval_token):
        """Future explicit importer. Caller must pass a reviewed preview token; never called by service."""
        if not approval_token:
            raise ValueError("baseline import requires explicit reviewed approval token")
        keys = set()
        for row in rows:
            if int(row.get("playcount") or 0) <= 0:
                continue
            track_key, unused_kind = track_identity(row)
            if track_key in keys:
                raise ValueError("baseline preview contains duplicate library-track identities; resolve before import")
            keys.add(track_key)
        imported = 0
        with self.connection:
            for row in rows:
                count = int(row.get("playcount") or 0)
                if count <= 0:
                    continue
                track_key, album_key = self.ensure_identity(row)
                event_key = "baseline:%s:%s" % (approval_token, track_key)
                cursor = self.connection.execute(
                    "INSERT OR IGNORE INTO play_event(event_key,track_key,album_key,source,play_count,occurred_at) VALUES(?,?,?,?,?,?)",
                    (event_key, track_key, album_key, "kodi_baseline_import", count, int(time.time())))
                if cursor.rowcount == 1:
                    self.connection.execute("INSERT INTO track_totals(track_key,imported_plays,total_plays) VALUES(?,?,?) ON CONFLICT(track_key) DO UPDATE SET imported_plays=imported_plays+excluded.imported_plays,total_plays=total_plays+excluded.total_plays", (track_key, count, count))
                    imported += count
            self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('rankings_dirty','1')")
        return imported

    def test_history_reset_preview(self):
        """Report the Stage 2A-only reset scope; this method never changes data."""
        row = self.connection.execute("SELECT count(*), count(DISTINCT track_key), count(DISTINCT album_key) FROM play_event WHERE source='live_tracking'").fetchone()
        return {"live_events": row[0], "affected_tracks": row[1], "affected_albums": row[2],
                "preserves_baseline_import_events": True, "requires_explicit_production_reset_token": True}

    def reset_stage2a_test_history(self, approval_token):
        """Explicit pre-production reset. Never call after real history begins."""
        if approval_token != "RESET_STAGE2A_TEST_HISTORY":
            raise ValueError("explicit Stage 2A reset token required")
        with self.connection:
            self.connection.execute("DELETE FROM play_event WHERE source='live_tracking'")
            self.connection.execute("DELETE FROM track_totals")
            self.connection.execute("""INSERT INTO track_totals(track_key,imported_plays,observed_plays,total_plays,last_played_at)
                SELECT track_key,
                       sum(CASE WHEN source='kodi_baseline_import' THEN play_count ELSE 0 END),
                       0,
                       sum(CASE WHEN source='kodi_baseline_import' THEN play_count ELSE 0 END),
                       NULL
                FROM play_event GROUP BY track_key""")
            self.connection.execute("DELETE FROM album_track WHERE NOT EXISTS (SELECT 1 FROM play_event e WHERE e.track_key=album_track.track_key)")
            self.connection.execute("DELETE FROM track_identity WHERE NOT EXISTS (SELECT 1 FROM play_event e WHERE e.track_key=track_identity.identity_key)")
            self.connection.execute("DELETE FROM album_identity WHERE NOT EXISTS (SELECT 1 FROM album_track a WHERE a.album_key=album_identity.album_key)")
            self.connection.execute("DELETE FROM album_rank_cache")
            self.connection.execute("DELETE FROM track_rank_cache")
            self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('rankings_dirty','1')")

    def reset_fresh_history(self, approval_token):
        """Explicit Schema-7 production reset; preserve catalog and identity data."""
        if approval_token != "RESET_SCHEMA7_FRESH_HISTORY_V1":
            raise ValueError("explicit Schema-7 fresh-history reset token required")
        if self.schema_version() != 7:
            raise RuntimeError("fresh-history reset requires schema 7")
        with self.connection:
            self.connection.execute("DELETE FROM play_event")
            self.connection.execute("DELETE FROM track_totals")
            self.connection.execute("DELETE FROM logical_song_totals")
            self.connection.execute("DELETE FROM track_rank_cache")
            self.connection.execute("DELETE FROM album_rank_cache")
            self.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('rankings_dirty','1')")
