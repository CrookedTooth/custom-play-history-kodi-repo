# -*- coding: utf-8 -*-
"""Playback-priority gate for expensive catalog maintenance only."""
from __future__ import absolute_import

import json

import xbmc


class CatalogRefreshAborted(RuntimeError):
    """Active media requested that catalog maintenance yield safely."""


class PlaybackStateProbe(object):
    """Redundant, fail-closed media-state probe for catalog maintenance.

    This is deliberately more conservative than normal playback tracking:
    an uncertain result prevents expensive catalog work from starting or
    continuing.  A single positive signal wins immediately.
    """

    def __init__(self, xbmc_module=None):
        self.xbmc = xbmc_module or xbmc
        self.last_reason = "not_checked"

    def _active(self, reason):
        self.last_reason = reason
        return True

    def _unsafe(self, reason):
        self.last_reason = "playback_state_unknown:%s" % reason
        return True

    def _active_players(self):
        request = {
            "jsonrpc": "2.0", "id": "playhistory-maintenance-state",
            "method": "Player.GetActivePlayers"
        }
        response = json.loads(self.xbmc.executeJSONRPC(json.dumps(request)))
        if not isinstance(response, dict) or response.get("error") is not None:
            raise ValueError("invalid Player.GetActivePlayers response")
        players = response.get("result")
        if not isinstance(players, list):
            raise ValueError("Player.GetActivePlayers result is not a list")
        for player in players:
            if not isinstance(player, dict):
                raise ValueError("Player.GetActivePlayers item is not an object")
            if player.get("type") in ("audio", "video"):
                return player.get("type")
        return None

    def __call__(self):
        """Return True unless media is positively established as idle."""
        uncertain = []
        try:
            player = self.xbmc.Player()
            if player is None:
                uncertain.append("player_unavailable")
            else:
                if player.isPlayingAudio():
                    return self._active("Player.isPlayingAudio")
                if player.isPlayingVideo():
                    return self._active("Player.isPlayingVideo")
                if player.isPlaying():
                    return self._active("Player.isPlaying")
        except Exception as exc:
            uncertain.append("Player:%s" % exc.__class__.__name__)

        try:
            active_type = self._active_players()
            if active_type:
                return self._active("Player.GetActivePlayers:%s" % active_type)
        except Exception as exc:
            uncertain.append("Player.GetActivePlayers:%s" % exc.__class__.__name__)

        try:
            if self.xbmc.getCondVisibility("Player.HasAudio"):
                return self._active("Player.HasAudio")
            if self.xbmc.getCondVisibility("Player.HasVideo"):
                return self._active("Player.HasVideo")
        except Exception as exc:
            uncertain.append("condition_visibility:%s" % exc.__class__.__name__)

        if uncertain:
            return self._unsafe(",".join(uncertain))
        self.last_reason = "idle"
        return False


class PlaybackAwareCatalogGate(object):
    """Require continuous Kodi media-idle time before a full catalog build."""

    def __init__(self, is_media_playing, clock, idle_grace_seconds=12.0):
        self.is_media_playing = is_media_playing
        self.clock = clock
        self.idle_grace_seconds = float(idle_grace_seconds)
        self._idle_since = None

    def ready(self, refresh_pending):
        if not refresh_pending:
            self._idle_since = None
            return False
        if self.is_media_playing():
            self._idle_since = None
            return False
        now = self.clock()
        if self._idle_since is None:
            self._idle_since = now
            return False
        return (now - self._idle_since) >= self.idle_grace_seconds

    def reset(self):
        self._idle_since = None
