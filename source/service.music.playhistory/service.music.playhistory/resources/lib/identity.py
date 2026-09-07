# -*- coding: utf-8 -*-
"""Conservative stable identifiers for music history records."""
from __future__ import absolute_import

import hashlib
import os
import re
import unicodedata


def text(value):
    return "" if value is None else str(value).strip()


def normalized_text(value):
    value = unicodedata.normalize("NFKC", text(value)).casefold()
    # These are presentation-equivalent punctuation variants, not a general
    # punctuation-stripping rule.  Preserve all other meaningful text.
    value = value.translate(str.maketrans({
        u"\u2018": "'", u"\u2019": "'", u"\u201b": "'", u"\u02bc": "'", u"\uff07": "'",
        u"\u2010": "-", u"\u2011": "-", u"\u2012": "-", u"\u2013": "-", u"\u2014": "-", u"\u2212": "-",
    }))
    return re.sub(r"\s+", " ", value).strip()


def normalized_path(value):
    value = text(value).replace("\\", "/")
    value = re.sub(r"/+", "/", value)
    return value.casefold().rstrip("/")


def _hash(parts):
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _number(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def recording_identity(metadata):
    """Return recording-level metadata only; it never identifies a play event."""
    recording = normalized_text(metadata.get("musicbrainz_recording_id"))
    if recording:
        return "mb-recording:" + recording, "musicbrainz_recording"
    return "", ""


def track_identity(metadata):
    """Return the schema-5 durable, release-specific Identity v2 key."""
    # A recording may appear on multiple albums, remasters, compilations, or
    # separate files. It is intentionally not used as this primary key.
    release = normalized_text(metadata.get("musicbrainz_release_id"))
    disc = _number(metadata.get("disc"), 1)
    track = _number(metadata.get("track"))
    recording = normalized_text(metadata.get("musicbrainz_recording_id"))
    if release and recording:
        return "mb-release-track-recording:%s:%d:%d:%s" % (release, disc, track, recording), "musicbrainz_release_track_recording"
    if release:
        return "mb-release-track-metadata:%s:%d:%d:%s:%s" % (release, disc, track, normalized_text(metadata.get("artist")), normalized_text(metadata.get("title"))), "musicbrainz_release_track_metadata"

    path = normalized_path(metadata.get("file"))
    if path:
        return "path:" + path, "file_path"

    parts = [
        normalized_text(metadata.get("artist")), normalized_text(metadata.get("album")),
        str(disc), str(track), normalized_text(metadata.get("title")),
    ]
    return "composite:" + _hash(parts), "conservative_composite"


def album_identity(metadata):
    release = normalized_text(metadata.get("musicbrainz_release_id"))
    if release:
        return "mb-release:" + release, "musicbrainz_release"
    parts = [
        normalized_text(metadata.get("album_artist") or metadata.get("artist")),
        normalized_text(metadata.get("album")), normalized_text(metadata.get("year")),
    ]
    return "album-composite:" + _hash(parts), "conservative_composite"


def logical_song_identity(metadata, collision_discriminator=None):
    """Logical listening identity, refined only for proven catalog collisions.

    ``collision_discriminator`` is deliberately optional.  The ordinary
    artist/title identity keeps its Schema-7 key byte-for-byte; Schema 8 adds
    a stable discriminator only after catalog evidence has established that a
    particular artist/title group contains distinct recordings.
    """
    artist = normalized_text(metadata.get("artist"))
    title = normalized_text(metadata.get("title"))
    if not artist or not title:
        return None, artist, title, "missing_artist_or_title"
    discriminator = normalized_text(collision_discriminator)
    parts = [artist, title]
    if discriminator:
        parts.append(discriminator)
    return "logical-song:" + _hash(parts), artist, title, None


def logical_album_identity(path, source_root_id, physical_source_root):
    """Folder identity: artist folder + album folder minus terminal [quality]."""
    path = normalized_path(path)
    root = normalized_path(physical_source_root)
    if not path or not root or not path.startswith(root + "/"):
        return None
    relative = path[len(root) + 1:].split("/")
    if len(relative) < 3:
        return None
    artist_folder, album_folder = relative[0], relative[1]
    album_folder = re.sub(r"\s*\[[^\]]+\]\s*$", "", album_folder).strip()
    artist_key, album_key = normalized_text(artist_folder), normalized_text(album_folder)
    if not artist_key or not album_key:
        return None
    return ("logical-album:" + _hash([normalized_text(source_root_id), artist_key, album_key]),
            artist_folder, album_folder, artist_key, album_key)


def metadata_artist_identity(value):
    """Stable ranking identity for one performing artist label.

    This deliberately keeps Kodi's supplied artist label intact apart from the
    existing conservative text normalisation.  It never splits collaborators or
    featured-artist separators into additional artist credits.
    """
    artist = normalized_text(value)
    return ("metadata-artist:" + _hash([artist]), artist) if artist else (None, "")


def metadata_album_identity(album_artist, artist, album):
    """Stable metadata album identity; AlbumArtist wins when Kodi supplies it."""
    credited = normalized_text(album_artist) or normalized_text(artist)
    title = normalized_text(album)
    if not credited or not title:
        return None, credited, title
    return "metadata-album:" + _hash([credited, title]), credited, title


def metadata_track_identity(artist, album, title, disc=None, track=None,
                            require_slot=False):
    """Stable ranking identity for Artist + Album + Title.

    Disc/track are used only for a group already proven to contain duplicate
    titles within one release; ordinary LP/WEB/remaster variants remain merged.
    """
    credited = normalized_text(artist)
    album_title = normalized_text(album)
    track_title = normalized_text(title)
    if not credited or not album_title or not track_title:
        return None, credited, album_title, track_title, None
    parts = [credited, album_title, track_title]
    slot = None
    if require_slot:
        slot = "%d:%d" % (_number(disc, 0), _number(track, 0))
        parts.append(slot)
    return "metadata-track:" + _hash(parts), credited, album_title, track_title, slot
