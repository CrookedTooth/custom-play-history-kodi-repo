# -*- coding: utf-8 -*-
"""Explicit logical-to-physical music-source configuration."""
from __future__ import absolute_import


DEFAULT_SOURCE_ROOT_ID = ""
DEFAULT_PHYSICAL_SOURCE_ROOT = ""
PRESERVATION_MARKER = "continuity_source_root_preserved_v1"


def _setting(addon, key, default):
    try:
        value = addon.getSettingString(key)
    except AttributeError:
        value = addon.getSetting(key)
    return (value or default).strip()


def _set_setting(addon, key, value):
    """Persist a setting across both Kodi 20/21 setting APIs."""
    try:
        addon.setSettingString(key, value)
    except AttributeError:
        addon.setSetting(key, value)


def _preserve_effective_legacy_mapping(addon, source_root_id, physical_source_root):
    """Persist one effective pre-neutral-default mapping, if one exists.

    During the short compatibility bridge an installed add-on still exposes its
    old package defaults through Kodi's settings API.  Capturing those values
    first makes the subsequent neutral package update non-destructive.  A
    genuinely fresh or deliberately blank install has no values to capture.
    """
    if _setting(addon, PRESERVATION_MARKER, "") == "1":
        return
    if not source_root_id and not physical_source_root:
        return
    try:
        _set_setting(addon, "continuity_source_root_id", source_root_id)
        _set_setting(addon, "continuity_physical_source_root", physical_source_root)
        _set_setting(addon, PRESERVATION_MARKER, "1")
    except (AttributeError, TypeError, ValueError):
        # A read-only or older settings implementation remains usable; it is
        # simply not eligible for automatic default-preservation.
        return


def configured_source_root(addon):
    """Return the user-visible, add-on-owned continuity source mapping."""
    source_root_id = _setting(addon, "continuity_source_root_id", DEFAULT_SOURCE_ROOT_ID)
    physical_source_root = _setting(addon, "continuity_physical_source_root", DEFAULT_PHYSICAL_SOURCE_ROOT)
    _preserve_effective_legacy_mapping(addon, source_root_id, physical_source_root)
    return {"source_root_id": source_root_id, "physical_source_root": physical_source_root}


def _path(value):
    if not value:
        return ""
    return str(value).replace("\\", "/").strip()


def relative_path(physical_path, physical_root):
    """Return a slash-normalized child path, or None outside the configured root."""
    path = _path(physical_path)
    root = _path(physical_root).rstrip("/")
    if not path or not root:
        return None
    folded_path = path.casefold()
    folded_root = root.casefold()
    prefix = folded_root + "/"
    if not folded_path.startswith(prefix):
        return None
    result = path[len(root):].lstrip("/")
    return result or None


def normalize_relative_path(value):
    """Normalize only separators and boundary slashes; preserve path spelling."""
    return _path(value).strip("/")


def common_album_path(relative_files):
    """Shared album-directory path for one album's source-root-relative files."""
    directories = []
    for value in relative_files:
        value = normalize_relative_path(value)
        if not value or "/" not in value:
            return None
        directories.append(value.rsplit("/", 1)[0].split("/"))
    if not directories:
        return None
    prefix = directories[0]
    for parts in directories[1:]:
        length = 0
        for left, right in zip(prefix, parts):
            if left.casefold() != right.casefold():
                break
            length += 1
        prefix = prefix[:length]
        if not prefix:
            return None
    return "/".join(prefix)
