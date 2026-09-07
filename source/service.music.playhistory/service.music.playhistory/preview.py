# -*- coding: utf-8 -*-
"""Read-only, manual baseline-preview entry point.

Run only when explicitly requested after deployment; it never imports data.
"""
from __future__ import absolute_import

import json
import xbmc

from resources.lib.baseline import BaselinePreview


def log(message, level=xbmc.LOGINFO):
    xbmc.log("[service.music.playhistory] %s" % message, level)


if __name__ == "__main__":
    preview = BaselinePreview.from_kodi(log)
    log("BASELINE PREVIEW (no data imported): %s" % json.dumps(preview.summary(), sort_keys=True))
