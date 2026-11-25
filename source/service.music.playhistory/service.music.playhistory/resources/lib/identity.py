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


# Artist-ranking normalization is intentionally separate from
# ``metadata_artist_identity`` above.  The latter is durable source metadata
# and must continue to preserve Kodi's supplied credit verbatim.  These rules
# apply only while aggregating the derived Most Played Artists cache.
_FEATURED_ARTIST_SUFFIX = re.compile(
    r"\s+(?:feat(?:\.|uring)?|ft\.?)\s+.+$", re.IGNORECASE)

# Exact aliases only.  Do not use prefixes, substrings, or fuzzy matching:
# artist names containing an ampersand are ordinary legitimate credits.
_ARTIST_RANKING_ALIASES = {
    "g. love": "G. Love & Special Sauce",
    "g. love & special sauce": "G. Love & Special Sauce",
}


def _is_reliable_album_artist(value):
    """True only for an explicit non-compilation Album Artist credit."""
    return normalized_text(value) not in ("", "various artists")


def metadata_primary_artist_identity(performer, album_artist):
    """Return the conservative primary credit for derived artist rankings.

    Kodi exposes multi-value music credits as a list; the tracker preserves
    that list as a literal ``" / "`` join.  We use Album Artist only when it
    exactly confirms the performer, or exactly confirms that first Kodi list
    member.  This is not general delimiter splitting: ampersands, ``and``,
    pluses, commas, and unconfirmed slash text remain intact.
    """
    original = text(performer)
    if not original:
        return None, "", ""

    # A terminal, explicitly labelled feature is unambiguous.  Do not remove
    # unlabelled collaborators or any other part of the credit.
    primary = _FEATURED_ARTIST_SUFFIX.sub("", original).strip() or original
    chosen = primary
    album_credit = text(album_artist)
    if _is_reliable_album_artist(album_credit):
        if normalized_text(album_credit) == normalized_text(primary):
            chosen = album_credit
        # This exact separator is emitted by our own Kodi list handling.  It
        # is therefore safe to inspect the first list member only when Album
        # Artist independently confirms it.
        elif " / " in original:
            first_credit = original.split(" / ", 1)[0].strip()
            if normalized_text(first_credit) == normalized_text(album_credit):
                chosen = album_credit

    # G. Love is an intentionally explicit, narrowly scoped display alias.
    # It never applies to an unrelated name merely containing these words.
    display = _ARTIST_RANKING_ALIASES.get(normalized_text(chosen), chosen)
    normalized = normalized_text(display)
    if not normalized:
        return None, "", ""
    return "metadata-artist:" + _hash([normalized]), normalized, display


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
