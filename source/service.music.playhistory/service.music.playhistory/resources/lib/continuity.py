# -*- coding: utf-8 -*-
"""Frozen v1.1 replacement-continuity receipt helpers (Stage 1 only)."""
from __future__ import absolute_import

import datetime
import hashlib
import json
import os
import re
import shutil
import time
import unicodedata
import uuid

import xbmcvfs

from .source_root import normalize_relative_path


class ManifestError(ValueError):
    pass


_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_RFC3339_UTC = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$")


def canonical_json(value):
    """The contract's UTF-8/no-BOM, sorted, compact JSON serialization."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def normalized_fingerprint_text(value):
    if value is None:
        return None
    value = unicodedata.normalize("NFC", str(value))
    return re.sub(r"\s+", " ", value, flags=re.UNICODE).strip().casefold()


def canonical_uuid(value, required=False):
    if value in (None, ""):
        if required:
            raise ManifestError("required UUID is missing")
        return None
    value = str(value).lower()
    if not _UUID.match(value):
        raise ManifestError("invalid UUID: %s" % value)
    return value


def _integer(value, field, allow_none=False):
    if value is None and allow_none:
        return None
    if isinstance(value, bool):
        raise ManifestError("%s must be an integer" % field)
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ManifestError("%s must be an integer" % field)
    if result != value and not isinstance(value, str):
        raise ManifestError("%s must be an integer" % field)
    return result


def catalog_fingerprint(album_artist, album_title, release_id, original_year, tracks):
    """Normative v1.1 fingerprint; duration is intentionally excluded."""
    descriptors = []
    for item in tracks:
        disc = _integer(item.get("disc"), "disc", True)
        track = _integer(item.get("track"), "track", True)
        descriptors.append({
            "artist": normalized_fingerprint_text(item.get("artist")),
            "disc": disc,
            "musicbrainz_recording_id": canonical_uuid(item.get("musicbrainz_recording_id")),
            "title": normalized_fingerprint_text(item.get("title")),
            "track": track,
        })
    descriptors.sort(key=lambda item: (
        0 if item["disc"] is not None else 1, item["disc"] if item["disc"] is not None else 0,
        0 if item["track"] is not None else 1, item["track"] if item["track"] is not None else 0,
        item["artist"] or "", item["title"] or "",
        0 if item["musicbrainz_recording_id"] is not None else 1,
        item["musicbrainz_recording_id"] or ""))
    payload = {
        "album_artist": normalized_fingerprint_text(album_artist),
        "album_title": normalized_fingerprint_text(album_title),
        "musicbrainz_release_id": canonical_uuid(release_id, True),
        "original_year": _integer(original_year, "original_year"),
        "tracks": descriptors,
    }
    return sha256_bytes(canonical_json(payload))


def _require(value, field):
    if value is None or value == "":
        raise ManifestError("missing %s" % field)
    return value


def _rfc3339_utc(value, field):
    _require(value, field)
    if not isinstance(value, str):
        raise ManifestError("%s must be RFC3339 UTC" % field)
    match = _RFC3339_UTC.match(value)
    if not match:
        raise ManifestError("%s must be RFC3339 UTC" % field)
    try:
        datetime.datetime(*[int(part) for part in match.groups()])
    except ValueError:
        raise ManifestError("%s must be RFC3339 UTC" % field)
    return value


def _track_descriptor(value):
    if not isinstance(value, dict):
        raise ManifestError("track descriptor must be an object")
    result = {
        "musicbrainz_release_id": canonical_uuid(value.get("musicbrainz_release_id"), True),
        "disc": _integer(value.get("disc"), "disc", True),
        "track": _integer(value.get("track"), "track", True),
        "musicbrainz_recording_id": canonical_uuid(value.get("musicbrainz_recording_id")),
        "artist": _require(value.get("artist"), "track.artist"),
        "title": _require(value.get("title"), "track.title"),
        "duration_ms": _integer(value.get("duration_ms"), "duration_ms", True),
        "library_relative_path": _require(value.get("library_relative_path"), "track.library_relative_path"),
    }
    return result


def _track_token(track):
    return canonical_json(track).decode("utf-8")


def _album_evidence(value):
    if not isinstance(value, dict):
        raise ManifestError("album evidence must be an object")
    result = dict(value)
    for field in ("source_root_id", "library_relative_path", "album_artist", "album_title", "quality"):
        _require(result.get(field), "album.%s" % field)
    result["musicbrainz_release_id"] = canonical_uuid(result.get("musicbrainz_release_id"), True)
    result["musicbrainz_release_group_id"] = canonical_uuid(result.get("musicbrainz_release_group_id"))
    result["original_year"] = _integer(result.get("original_year"), "album.original_year")
    result["track_count"] = _integer(result.get("track_count"), "album.track_count")
    if result["track_count"] < 1:
        raise ManifestError("album.track_count must be positive")
    quality = result["quality"]
    if not isinstance(quality, dict):
        raise ManifestError("album.quality must be an object")
    codec = quality.get("codec")
    if codec == "FLAC":
        _integer(quality.get("bit_depth"), "quality.bit_depth")
        _integer(quality.get("sample_rate_hz"), "quality.sample_rate_hz")
    elif codec == "MP3_VBR":
        if quality.get("is_vbr") is True:
            if quality.get("bitrate_kbps") is not None:
                raise ManifestError("MP3 VBR bitrate_kbps must be null")
        else:
            raise ManifestError("MP3_VBR requires is_vbr true")
    elif codec == "MP3_CBR":
        if quality.get("is_vbr") is not False:
            raise ManifestError("MP3_CBR requires is_vbr false")
        _integer(quality.get("bitrate_kbps"), "quality.bitrate_kbps")
    else:
        raise ManifestError("unsupported quality codec")
    return result


def validate_manifest_bytes(raw):
    """Parse and fully validate a finalized v1 manifest without activating it."""
    if raw.startswith(b"\xef\xbb\xbf") or raw.strip() != raw or raw.endswith(b"\n"):
        raise ManifestError("manifest must be finalized compact UTF-8 JSON")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ManifestError("malformed JSON: %s" % exc)
    if canonical_json(value) != raw:
        raise ManifestError("manifest bytes are not canonical finalized JSON")
    if not isinstance(value, dict) or value.get("manifest_version") != 1:
        raise ManifestError("unsupported manifest_version")
    replacement_id = canonical_uuid(value.get("replacement_id"), True)
    _rfc3339_utc(value.get("created_at"), "created_at")
    _rfc3339_utc(value.get("approved_at"), "approved_at")
    producer = value.get("producer")
    if not isinstance(producer, dict):
        raise ManifestError("producer must be an object")
    for field in ("application", "version", "build"):
        _require(producer.get(field), "producer.%s" % field)
    if value.get("decision") != "preserve_history":
        raise ManifestError("unsupported decision")
    predecessor = _album_evidence(value.get("predecessor"))
    successor = _album_evidence(value.get("successor"))
    mappings = value.get("track_mappings")
    unmapped = value.get("unmapped_predecessor_tracks")
    bonus = value.get("bonus_successor_tracks")
    if not all(isinstance(items, list) for items in (mappings, unmapped, bonus)):
        raise ManifestError("track classification arrays are required")
    pred_tracks, succ_tracks = [], []
    seen_pred, seen_succ = set(), set()
    for mapping in mappings:
        if not isinstance(mapping, dict):
            raise ManifestError("mapping must be an object")
        pred = _track_descriptor(mapping.get("predecessor"))
        succ = _track_descriptor(mapping.get("successor"))
        ptoken, stoken = _track_token(pred), _track_token(succ)
        if ptoken in seen_pred or stoken in seen_succ:
            raise ManifestError("duplicate or one-to-many track mapping")
        seen_pred.add(ptoken); seen_succ.add(stoken); pred_tracks.append(pred); succ_tracks.append(succ)
    for item in unmapped:
        pred = _track_descriptor(item); token = _track_token(pred)
        if token in seen_pred: raise ManifestError("duplicate predecessor classification")
        seen_pred.add(token); pred_tracks.append(pred)
    for item in bonus:
        succ = _track_descriptor(item); token = _track_token(succ)
        if token in seen_succ: raise ManifestError("duplicate successor classification")
        seen_succ.add(token); succ_tracks.append(succ)
    if len(pred_tracks) != predecessor["track_count"] or len(succ_tracks) != successor["track_count"]:
        raise ManifestError("track classification is not exhaustive")
    if catalog_fingerprint(predecessor["album_artist"], predecessor["album_title"], predecessor["musicbrainz_release_id"], predecessor["original_year"], pred_tracks) != _require(predecessor.get("catalog_fingerprint"), "predecessor.catalog_fingerprint"):
        raise ManifestError("predecessor catalog fingerprint mismatch")
    if catalog_fingerprint(successor["album_artist"], successor["album_title"], successor["musicbrainz_release_id"], successor["original_year"], succ_tracks) != _require(successor.get("catalog_fingerprint"), "successor.catalog_fingerprint"):
        raise ManifestError("successor catalog fingerprint mismatch")
    return value, replacement_id, sha256_bytes(raw)


def _utc_now():
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


class InboxProcessor(object):
    """Idle-only receipt processing; it never activates replacement lineage."""
    def __init__(self, database, addon_id, logger, root=None):
        self.database = database
        self.addon_id = addon_id
        self.log = logger
        self.root = root or xbmcvfs.translatePath("special://profile/addon_data/%s/inbox/" % addon_id)

    def _path(self, *parts):
        return os.path.join(self.root, *parts)

    def ensure_directories(self):
        for parts in ((), ("incoming",), ("processed",), ("rejected",), ("rejected", "conflicts"), ("quarantine",), ("quarantine", "duplicates"), ("acknowledgments",)):
            path = self._path(*parts)
            if not os.path.isdir(path):
                os.makedirs(path)

    def _atomic_json(self, path, value):
        temp = path + ".tmp-" + uuid.uuid4().hex
        with open(temp, "wb") as handle:
            handle.write(canonical_json(value))
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temp, path)

    def _report(self, code, replacement_id, source, raw_sha, message, **extra):
        value = {"error_code": code, "replacement_id": replacement_id, "source_filename": source,
                 "received_at": _utc_now(), "received_manifest_sha256": raw_sha, "message": message}
        value.update(extra)
        return value

    def _quarantine(self, source, raw, message):
        digest = sha256_bytes(raw); first = self._path("quarantine", digest + ".raw")
        report = {"error_code": "invalid_json" if message.startswith("malformed JSON") else "invalid_manifest",
                  "file_sha256": digest, "message": "Manifest JSON is malformed." if message.startswith("malformed JSON") else message,
                  "quarantined_at": _utc_now(), "receipt_state": "quarantined", "replacement_id": None,
                  "source_filename": os.path.basename(source)}
        if not os.path.exists(first):
            shutil.move(source, first)
            self._atomic_json(self._path("quarantine", digest + ".report.json"), report)
        else:
            directory = self._path("quarantine", "duplicates", digest)
            if not os.path.isdir(directory): os.makedirs(directory)
            stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex
            destination = os.path.join(directory, stamp + ".raw")
            shutil.move(source, destination)
            self._atomic_json(os.path.join(directory, stamp + ".report.json"), report)

    def _acknowledgment(self, replacement_id, digest, state, message):
        now = _utc_now()
        return {"replacement_id": replacement_id, "manifest_sha256": digest,
                "receipt_state": state, "receipt_at": now,
                "lifecycle_state": "pending_catalog_validation", "status_revision": 1,
                "status_updated_at": now, "resolved_path_mode": None, "message": message}

    def validate_raw(self, raw):
        """Shared manifest parser for the narrowly scoped preparation path."""
        return validate_manifest_bytes(raw)

    def manifest_for(self, replacement_id):
        row = self.database.connection.execute("SELECT raw_manifest_json FROM manifest_import WHERE replacement_id=? AND receipt_state='accepted'", (replacement_id,)).fetchone()
        if row is None:
            raise ManifestError('accepted replacement manifest is no longer available')
        return validate_manifest_bytes(bytes(row['raw_manifest_json']))

    def trusted_predecessor_generation(self, manifest):
        """Return only complete/superseded, immutable predecessor evidence."""
        predecessor = manifest['predecessor']
        key = 'mb-release:' + predecessor['musicbrainz_release_id'].lower()
        row = self.database.connection.execute("""SELECT s.generation_id
            FROM catalog_album_snapshot s JOIN catalog_generation g ON g.generation_id=s.generation_id
            WHERE s.album_key=? AND s.release_id=? AND s.catalog_fingerprint=? AND s.source_root_id=?
              AND g.state IN ('complete','superseded')
            ORDER BY s.generation_id DESC LIMIT 1""", (key, predecessor['musicbrainz_release_id'], predecessor['catalog_fingerprint'], predecessor['source_root_id'])).fetchone()
        if row is None:
            raise ManifestError('Awaiting trusted predecessor catalog presence.')
        return row['generation_id']

    def process_once(self, maximum=5):
        self.ensure_directories()
        incoming = self._path("incoming")
        names = sorted(name for name in os.listdir(incoming) if name.endswith(".json"))[:maximum]
        handled = 0
        for name in names:
            source = os.path.join(incoming, name)
            try:
                with open(source, "rb") as handle: raw = handle.read()
                manifest, replacement_id, digest = validate_manifest_bytes(raw)
            except ManifestError as exc:
                self._quarantine(source, raw if 'raw' in locals() else b"", str(exc)); handled += 1; continue
            row = self.database.connection.execute("SELECT manifest_sha256 FROM manifest_import WHERE replacement_id=?", (replacement_id,)).fetchone()
            if row and row[0] != digest:
                base = "%s.%s" % (replacement_id, digest)
                destination = self._path("rejected", "conflicts", base + ".json")
                shutil.move(source, destination)
                self._atomic_json(self._path("rejected", "conflicts", base + ".report.json"), {"error_code": "replacement_id_hash_conflict", "existing_manifest_sha256": row[0], "message": "Replacement ID is already registered with different manifest bytes.", "received_at": _utc_now(), "received_manifest_sha256": digest, "replacement_id": replacement_id, "source_filename": name})
                handled += 1; continue
            if not row:
                now = int(time.time())
                ack = self._acknowledgment(replacement_id, digest, "accepted", "Awaiting completed catalog generation.")
                with self.database.connection:
                    self.database.connection.execute("INSERT INTO manifest_import(replacement_id,manifest_sha256,raw_manifest_json,imported_at,receipt_state,lifecycle_state,status_revision,status_updated_at,receipt_at,resolved_path_mode,message) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (replacement_id, digest, raw, now, "accepted", "pending_catalog_validation", 1, now, now, None, ack["message"]))
                self._atomic_json(self._path("acknowledgments", replacement_id + ".ack.json"), ack)
            shutil.move(source, self._path("processed", name))
            handled += 1
        return handled

    def reconcile_pending(self, replacement_id=None):
        """Advance accepted manifests only from trusted completed snapshots.

        This is deliberately called from service maintenance, never playback or
        a directory endpoint.  It does not create catalog generations.
        """
        current = self.database.connection.execute("SELECT generation_id FROM catalog_generation WHERE state='complete' ORDER BY generation_id DESC LIMIT 1").fetchone()
        if not current:
            return 0
        generation = current[0]
        sql = "SELECT replacement_id,manifest_sha256,raw_manifest_json,lifecycle_state,status_revision,receipt_at FROM manifest_import WHERE receipt_state='accepted' AND lifecycle_state IN ('pending_catalog_validation','active','suspended')"
        params = ()
        if replacement_id:
            sql += " AND replacement_id=?"
            params = (replacement_id,)
        rows = self.database.connection.execute(sql, params).fetchall()
        changed = 0
        for row in rows:
            try:
                manifest, replacement_id, digest = validate_manifest_bytes(bytes(row['raw_manifest_json']))
                state, mode, message, mappings, pred_gen, succ_gen, successor_key = self._evaluate(manifest, generation)
            except ManifestError as exc:
                state, mode, message, mappings, pred_gen, succ_gen, successor_key = 'rejected', None, str(exc), [], None, None, None
            if state == row['lifecycle_state']:
                continue
            revision = int(row['status_revision']) + 1
            now = int(time.time())
            with self.database.connection:
                self.database.connection.execute("UPDATE manifest_import SET lifecycle_state=?,status_revision=?,status_updated_at=?,resolved_path_mode=?,message=? WHERE replacement_id=?", (state, revision, now, mode, message, replacement_id))
                if state == 'active':
                    predecessor_key = 'mb-release:' + manifest['predecessor']['musicbrainz_release_id'].lower()
                    self.database.connection.execute("INSERT OR REPLACE INTO album_replacement(replacement_id,predecessor_album_key,successor_album_key,predecessor_release_id,successor_release_id,manifest_sha256,imported_at,approved_at,lifecycle_state,catalog_verified_at,audit_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (replacement_id, predecessor_key, successor_key, manifest['predecessor']['musicbrainz_release_id'], manifest['successor']['musicbrainz_release_id'], digest, now, now, state, now, json.dumps({'predecessor_generation_id':pred_gen,'successor_generation_id':succ_gen})))
                    self.database.connection.execute("DELETE FROM track_replacement WHERE replacement_id=?", (replacement_id,))
                    self.database.connection.executemany("INSERT INTO track_replacement(replacement_id,predecessor_track_key,successor_track_key,mapping_json) VALUES(?,?,?,?)", [(replacement_id, p, s, json.dumps(detail, sort_keys=True)) for p, s, detail in mappings])
                elif row['lifecycle_state'] == 'active':
                    self.database.connection.execute("UPDATE album_replacement SET lifecycle_state=?,suspended_at=? WHERE replacement_id=?", (state, now if state == 'suspended' else None, replacement_id))
                self.database.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('rankings_dirty','1')")
            ack = {"replacement_id": replacement_id, "manifest_sha256": digest, "receipt_state": "accepted", "receipt_at": datetime.datetime.utcfromtimestamp(row['receipt_at']).strftime('%Y-%m-%dT%H:%M:%SZ'), "lifecycle_state": state, "status_revision": revision, "status_updated_at": _utc_now(), "resolved_path_mode": mode, "message": message}
            self._atomic_json(self._path('acknowledgments', replacement_id + '.ack.json'), ack)
            changed += 1
        return changed

    def revoke(self, replacement_id):
        """Explicitly revoke one active or suspended relationship for audit-safe cleanup."""
        replacement_id = canonical_uuid(replacement_id, True)
        self.ensure_directories()
        row = self.database.connection.execute("SELECT * FROM manifest_import WHERE replacement_id=?", (replacement_id,)).fetchone()
        if row is None:
            raise ManifestError('unknown replacement_id')
        state = row['lifecycle_state']
        if state == 'revoked':
            return False
        if state not in ('active', 'suspended'):
            raise ManifestError('replacement lifecycle is not revocable: %s' % state)
        lineage = self.database.connection.execute("SELECT * FROM album_replacement WHERE replacement_id=?", (replacement_id,)).fetchone()
        if lineage is None:
            raise ManifestError('registered replacement has no revocable lineage')
        revision = int(row['status_revision']) + 1
        now = int(time.time())
        audit = {}
        try:
            audit = json.loads(lineage['audit_json'] or '{}')
        except (TypeError, ValueError):
            audit = {}
        audit['revoked_at'] = now
        audit['prior_lifecycle_state'] = state
        audit['acknowledgment_revision'] = revision
        message = 'Continuity revoked by explicit maintenance request.'
        acknowledgment = {
            'replacement_id': replacement_id,
            'manifest_sha256': row['manifest_sha256'],
            'receipt_state': row['receipt_state'],
            'receipt_at': datetime.datetime.utcfromtimestamp(row['receipt_at']).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'lifecycle_state': 'revoked',
            'status_revision': revision,
            'status_updated_at': _utc_now(),
            'resolved_path_mode': row['resolved_path_mode'],
            'message': message,
        }
        with self.database.connection:
            self.database.connection.execute("UPDATE manifest_import SET lifecycle_state='revoked',status_revision=?,status_updated_at=?,message=? WHERE replacement_id=?", (revision, now, message, replacement_id))
            self.database.connection.execute("UPDATE album_replacement SET lifecycle_state='revoked',revoked_at=?,audit_json=? WHERE replacement_id=?", (now, json.dumps(audit, sort_keys=True), replacement_id))
            if state == 'active':
                self.database.connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('rankings_dirty','1')")
            # Atomic replacement of the acknowledgement is deliberately inside
            # the transaction so an acknowledgement write failure rolls back DB state.
            self._atomic_json(self._path('acknowledgments', replacement_id + '.ack.json'), acknowledgment)
        self.log('replacement %s revoked from %s' % (replacement_id, state))
        return True

    def _evaluate(self, manifest, generation):
        pred = manifest['predecessor']; succ = manifest['successor']
        pred_key = 'mb-release:' + pred['musicbrainz_release_id'].lower()
        targeted = self._targeted_successor(manifest)
        # Current successor selection is release-isolated and path-safe. A
        # verified targeted observation is separate, immutable evidence; it is
        # never represented as a full catalog generation.
        successor, mode, resolution_message = self._resolve_successor(succ, generation)
        if targeted:
            successor, mode, resolution_message = targeted, 'targeted_audio_library_scan', 'Targeted successor observation verified.'
        predecessor_current = self.database.connection.execute("SELECT 1 FROM catalog_album_snapshot WHERE generation_id=? AND album_key=? AND source_root_id=?", (generation, pred_key, pred['source_root_id'])).fetchone()
        # Never infer trust from generation order: interrupted generations can
        # retain partial rows after a hard stop and are not catalog evidence.
        historical = self.database.connection.execute("""SELECT s.generation_id
            FROM catalog_album_snapshot s JOIN catalog_generation g ON g.generation_id=s.generation_id
            WHERE s.album_key=? AND s.release_id=? AND s.catalog_fingerprint=? AND s.source_root_id=? AND s.generation_id<?
              AND g.state IN ('complete','superseded')
            ORDER BY s.generation_id DESC LIMIT 1""", (pred_key, pred['musicbrainz_release_id'], pred['catalog_fingerprint'], pred['source_root_id'], generation)).fetchone()
        if predecessor_current and not (targeted and self._predecessor_files_are_absent(pred_key, historical[0] if historical else None)):
            return 'suspended', None, 'Predecessor is present in the current completed catalog.', [], historical[0] if historical else None, generation, None
        if not historical:
            return 'pending_catalog_validation', None, 'Awaiting trusted predecessor catalog presence.', [], None, generation, None
        if successor is None:
            return 'pending_catalog_validation', None, resolution_message, [], historical[0], generation, None
        succ_key = successor['album_key']
        # Map each approved pair to durable release-specific identities.
        mappings=[]
        for item in manifest['track_mappings']:
            p=item['predecessor']; s=item['successor']
            prow=self.database.connection.execute("SELECT track_key FROM catalog_track_snapshot WHERE generation_id=? AND album_key=? AND disc IS ? AND track_number IS ?", (historical[0], pred_key, p['disc'], p['track'])).fetchall()
            if targeted:
                srow=self.database.connection.execute("SELECT track_key FROM targeted_successor_track WHERE replacement_id=? AND disc IS ? AND track_number IS ?", (manifest['replacement_id'], s['disc'], s['track'])).fetchall()
            else:
                srow=self.database.connection.execute("SELECT track_key FROM catalog_track_snapshot WHERE generation_id=? AND album_key=? AND disc IS ? AND track_number IS ?", (generation, successor['album_key'], s['disc'], s['track'])).fetchall()
            if len(prow)!=1 or len(srow)!=1:
                return 'rejected', None, 'Mapped tracks are not uniquely present in trusted catalog snapshots.', [], historical[0], generation, None
            mappings.append((prow[0][0],srow[0][0],item))
        # A predecessor may feed only one active successor; a simple cycle is refused.
        conflict=self.database.connection.execute("SELECT replacement_id FROM album_replacement WHERE predecessor_album_key=? AND lifecycle_state='active'", (pred_key,)).fetchone()
        if conflict:
            return 'rejected', None, 'Predecessor already feeds an active successor.', [], historical[0], generation, None
        cycle=self.database.connection.execute("WITH RECURSIVE chain(x) AS (SELECT successor_album_key FROM album_replacement WHERE predecessor_album_key=? AND lifecycle_state='active' UNION ALL SELECT a.successor_album_key FROM album_replacement a JOIN chain c ON a.predecessor_album_key=c.x WHERE a.lifecycle_state='active') SELECT 1 FROM chain WHERE x=? LIMIT 1", (succ_key,pred_key)).fetchone()
        if cycle:
            return 'rejected', None, 'Replacement lineage cycle refused.', [], historical[0], generation, None
        return 'active', mode, 'Continuity active.', mappings, historical[0], None if targeted else generation, succ_key

    def _targeted_successor(self, manifest):
        """Return one immutable targeted observation, if this schema has one."""
        try:
            row = self.database.connection.execute("""SELECT * FROM targeted_successor_observation
                WHERE replacement_id=? AND state='verified' AND source_root_id=? AND successor_release_id=?
                  AND catalog_fingerprint=? AND expected_path=?""", (manifest['replacement_id'],
                manifest['successor']['source_root_id'], manifest['successor']['musicbrainz_release_id'],
                manifest['successor']['catalog_fingerprint'], normalize_relative_path(manifest['successor']['library_relative_path']))).fetchone()
        except Exception:
            return None
        if row is None:
            return None
        return {'album_key': row['successor_album_key'], 'expected_path': row['expected_path'],
                'current_path': row['current_path']}

    def _predecessor_files_are_absent(self, predecessor_key, generation):
        """Distinguish a stale Kodi row from real physical coexistence.

        This is considered only after the same reachable SMB source produced a
        verified successor observation. Every historically snapshotted
        predecessor file must now be absent; one remaining file keeps lineage
        suspended.
        """
        if generation is None:
            return False
        rows = self.database.connection.execute("SELECT current_path FROM catalog_track_snapshot WHERE generation_id=? AND album_key=?", (generation, predecessor_key)).fetchall()
        if not rows:
            return False
        try:
            return all(path['current_path'] and not xbmcvfs.exists(path['current_path']) for path in rows)
        except Exception:
            return False

    def _resolve_successor(self, successor, generation):
        """Return one identity-qualified current successor and its path mode.

        Candidates are current-generation album snapshots with the manifest's
        release ID, catalog fingerprint, normalized artist/title, original year,
        and track count.  A path never supplies identity evidence: it only
        distinguishes candidates already proven to describe the same release.
        """
        rows = self.database.connection.execute("SELECT * FROM catalog_album_snapshot WHERE generation_id=? AND release_id=? AND catalog_fingerprint=? AND source_root_id=?", (generation, successor['musicbrainz_release_id'], successor['catalog_fingerprint'], successor['source_root_id'])).fetchall()
        candidates = [row for row in rows if
                      normalized_fingerprint_text(row['artist']) == normalized_fingerprint_text(successor['album_artist']) and
                      normalized_fingerprint_text(row['title']) == normalized_fingerprint_text(successor['album_title']) and
                      int(row['original_year']) == int(successor['original_year']) and
                      int(row['track_count']) == int(successor['track_count'])]
        expected = normalize_relative_path(successor['library_relative_path']).casefold()
        expected_candidates = [row for row in candidates if normalize_relative_path(row['expected_path']).casefold() == expected]
        if len(expected_candidates) == 1:
            return expected_candidates[0], 'expected', 'Expected successor path verified.'
        if len(expected_candidates) > 1:
            return None, None, 'Ambiguous expected successor path candidates.'
        # A release aggregated from multiple physical album directories has no
        # one safe expected path.  It cannot be used for path continuity.
        alternates = [row for row in candidates if row['current_path'] and row['expected_path']]
        if len(alternates) == 1:
            return alternates[0], 'alternate_unique', 'One identity-qualified alternate successor path verified.'
        if not alternates:
            return None, None, 'Awaiting one verified successor catalog match.'
        return None, None, 'Ambiguous alternate successor path candidates.'
