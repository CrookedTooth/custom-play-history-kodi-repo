# -*- coding: utf-8 -*-
"""Kodi service entry point for service.music.playhistory."""
from __future__ import absolute_import

import xbmc
import xbmcgui
import xbmcaddon
import json
import time

from resources.lib.database import HistoryDatabase
from resources.lib.ranking import RankingEngine
from resources.lib.tracker import PlaybackTracker
from resources.lib.continuity import InboxProcessor
from resources.lib.catalog import rows_from_kodi, CatalogRefreshAborted
from resources.lib.maintenance import PlaybackAwareCatalogGate, PlaybackStateProbe
from resources.lib.source_root import configured_source_root


ADDON_ID = "service.music.playhistory"
CATALOG_SYNC_DEBOUNCE_SECONDS = 15.0
CATALOG_IDLE_GRACE_SECONDS = 15.0


def log(message, level=xbmc.LOGINFO):
    xbmc.log("[%s] %s" % (ADDON_ID, message), level)


class ServiceMonitor(xbmc.Monitor):
    def __init__(self):
        xbmc.Monitor.__init__(self)
        self._catalog_sync_due_at = None

    def onNotification(self, sender, method, data):
        # Only schedule.  The normal idle loop owns the potentially expensive
        # catalog refresh, preventing callback work and refresh loops.
        if method == "AudioLibrary.OnScanFinished":
            self._catalog_sync_due_at = time.monotonic() + CATALOG_SYNC_DEBOUNCE_SECONDS

    def consume_catalog_sync_due(self):
        due_at = self._catalog_sync_due_at
        if due_at is None or time.monotonic() < due_at:
            return False
        self._catalog_sync_due_at = None
        return True


def rpc(method, params):
    request = {'jsonrpc': '2.0', 'id': 'playhistory-targeted-continuity', 'method': method, 'params': params}
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
        if response.get('error'):
            log('targeted continuity JSON-RPC %s failed: %s' % (method, response['error']), xbmc.LOGWARNING)
            return None
        return response.get('result')
    except Exception as exc:
        log('targeted continuity JSON-RPC %s failed: %s' % (method, exc), xbmc.LOGWARNING)
        return None


def run():
    database = HistoryDatabase(ADDON_ID, log)
    database.initialize()
    log("started; schema version %s" % database.schema_version())

    monitor = ServiceMonitor()
    tracker = PlaybackTracker(database, log)
    rankings = RankingEngine(database, log)
    inbox = InboxProcessor(database, ADDON_ID, log)
    source_root = configured_source_root(xbmcaddon.Addon(ADDON_ID))
    log("continuity source root configured: %s -> %s" % (source_root["source_root_id"], source_root["physical_source_root"]))
    inbox.ensure_directories()
    # Schema 12 reads generation-owned logical album state directly; do not
    # rebuild the legacy global compatibility tables on startup.
    if database.schema_version() < 12:
        database.rebuild_logical_album_current(source_root)
    next_inbox_check = 0
    # One initial full snapshot is explicit idle maintenance, never playback
    # or widget work.  Later refresh policy remains separately controlled.
    catalog_due = database.connection.execute("SELECT COUNT(*) FROM catalog_generation WHERE state='complete'").fetchone()[0] == 0
    playback_state = PlaybackStateProbe()
    catalog_gate = PlaybackAwareCatalogGate(
        playback_state, time.monotonic, CATALOG_IDLE_GRACE_SECONDS)
    try:
        while not monitor.abortRequested():
            tracker.tick()
            rankings.maybe_rebuild_when_idle(tracker.isPlayingAudio())
            if monitor.consume_catalog_sync_due():
                if database.schema_version() >= 13:
                    # Metadata-ranking history is event driven.  A library
                    # scan never initiates an exhaustive catalog generation.
                    log("AudioLibrary.OnScanFinished observed; metadata rankings need no catalog refresh")
                else:
                    database.request_catalog_refresh()
                    log("queued catalog synchronization after AudioLibrary.OnScanFinished")
            refresh_requested = database.catalog_refresh_requested()
            cleanup_generation = database.catalog_cleanup_pending_generation()
            if catalog_gate.ready(catalog_due or refresh_requested or cleanup_generation is not None):
                refresh_started = time.monotonic()
                try:
                    home = xbmcgui.Window(10000)
                    home.setProperty('MusicPlayHistory.CatalogRefreshing', 'true')
                    if cleanup_generation is not None:
                        home.setProperty('MusicPlayHistory.CatalogRefreshPhase', 'Cleaning')
                        log("resuming deferred cleanup for catalog generation %s after media-idle grace" %
                            cleanup_generation)
                        database.resume_catalog_cleanup(
                            should_abort=playback_state,
                            phase_callback=lambda phase: home.setProperty(
                                'MusicPlayHistory.CatalogRefreshPhase', phase))
                    else:
                        home.setProperty('MusicPlayHistory.CatalogRefreshPhase', 'Reading')
                        log("starting %s catalog generation after media-idle grace" % (
                            "requested refresh" if refresh_requested else "initial"))
                        rows = rows_from_kodi(log, should_abort=playback_state)
                        try:
                            # rows_from_kodi has bounded probes, but this explicit
                            # checkpoint prevents any DB phase after a read-time
                            # media start.
                            if playback_state():
                                raise CatalogRefreshAborted('media playback started after catalog read')
                            database.replace_album_catalog_staged(
                                rows, requested_refresh=refresh_requested,
                                source_root=source_root, should_abort=playback_state,
                                phase_callback=lambda phase: home.setProperty(
                                    'MusicPlayHistory.CatalogRefreshPhase', phase))
                        finally:
                            # Explicit repair maintenance must not retain the
                            # full Kodi-song materialization in this long-lived
                            # service frame.
                            del rows
                        catalog_due = False
                    catalog_gate.reset()
                except CatalogRefreshAborted as exc:
                    # The persistent request remains present; a failed candidate
                    # retains no promoted snapshots and is retried after idle grace.
                    candidate = database.connection.execute(
                        "SELECT generation_id FROM catalog_generation "
                        "ORDER BY generation_id DESC LIMIT 1").fetchone()
                    generation = cleanup_generation if cleanup_generation is not None else (
                        candidate[0] if candidate else "preparation")
                    log("catalog playback gate triggered: generation=%s phase=%s "
                        "source=%s elapsed=%.1fs" % (
                            generation, exc, playback_state.last_reason,
                            time.monotonic() - refresh_started))
                    log("catalog refresh deferred for active media: %s" % exc)
                    catalog_gate.reset()
                finally:
                    home = xbmcgui.Window(10000)
                    home.clearProperty('MusicPlayHistory.CatalogRefreshing')
                    home.clearProperty('MusicPlayHistory.CatalogRefreshPhase')
            # Receipt work is deliberately idle-only and never part of the
            # five-second playback sampling path.
            if not tracker.isPlayingAudio() and next_inbox_check <= 0:
                inbox.process_once()
                # Schema 7 retains receipts as audit evidence only.  Release
                # lineage no longer controls logical play aggregation.
                next_inbox_check = 30
            elif next_inbox_check > 0:
                next_inbox_check -= 1
            monitor.waitForAbort(1.0)
    except Exception as exc:
        log("service error: %s" % exc, xbmc.LOGERROR)
    finally:
        tracker.shutdown()
        database.close()
        log("stopped")


if __name__ == "__main__":
    run()
