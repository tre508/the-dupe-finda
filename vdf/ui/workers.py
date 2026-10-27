"""Background workers.

All heavy work lives off the GUI thread. We use QThread + worker objects
(moveToThread pattern) and communicate exclusively via signals so the Qt
event loop is never blocked and the table never freezes.

Note (deviation from the design plan): the plan suggested a
ProcessPoolExecutor for CPU-bound hashing. For a first working,
easy-to-maintain solo-dev build we use a single worker thread instead.
The real wall-clock cost here is FFmpeg subprocesses (already separate
processes), so the GIL is not the practical bottleneck. Swapping the
per-file loop onto a ProcessPoolExecutor is a localized change in
scanner._run() if you later need more CPU throughput.
"""
from __future__ import annotations

import threading
import logging

from PySide6.QtCore import QObject, Signal

from .. import db, grouping, thumbnails
from ..scanner import Scanner, Source

log = logging.getLogger(__name__)


class ScanWorker(QObject):
    progress = Signal(str, int, int)     # stage, done, total
    log = Signal(str)                    # human-readable line
    finished = Signal(str, object)       # final status text, scan_id

    def __init__(self, sources: list[Source]):
        super().__init__()
        self.sources = sources
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        try:
            log.info("ScanWorker starting with %s source(s)", len(self.sources))
            scanner = Scanner(
                self.sources,
                progress=lambda s, d, t: self.progress.emit(s, d, t),
                log=lambda m: self.log.emit(m),
                cancel_event=self._cancel,
            )
            status = scanner.run()
            scan_id = scanner.scan_id
        except Exception as exc:  # noqa: BLE001
            log.exception("ScanWorker failed")
            status = f"Scan failed: {exc!r}"
            scan_id = None
        log.info("ScanWorker finished: %s", status)
        self.finished.emit(status, scan_id)


class GroupWorker(QObject):
    """Builds duplicate groups after a scan (own DB connection/thread)."""
    finished = Signal(object)            # list[grouping.Group]
    log = Signal(str)

    def __init__(self, scan_id: int | None = None):
        super().__init__()
        self.scan_id = scan_id

    def run(self):
        conn = db.connect()
        try:
            groups = grouping.build_groups(conn, scan_id=self.scan_id)
            summary = grouping.summarize(groups)
            self.log.emit(
                f"Built {summary.confirmed} confirmed duplicate group(s), "
                f"{summary.possible} possible manual-review group(s), "
                f"{summary.hardlink} hardlink group(s)"
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Grouping failed")
            self.log.emit(f"Grouping failed: {exc!r}")
            groups = []
        finally:
            conn.close()
        self.finished.emit(groups)


class ThumbWorker(QObject):
    """Generates thumbnails lazily and reports each one as it's ready."""
    one_ready = Signal(int, str)         # file_id, thumb_path
    finished = Signal()

    def __init__(self, items: list[tuple[int, str, float]]):
        super().__init__()
        self.items = items               # (file_id, path, duration)
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        for fid, path, dur in self.items:
            if self._cancel.is_set():
                break
            try:
                tp = thumbnails.ensure_thumb(fid, path, dur)
            except Exception as exc:  # noqa: BLE001
                log.exception("Thumbnail failed for %s", path)
                tp = None
            if tp:
                self.one_ready.emit(fid, tp)
        self.finished.emit()
