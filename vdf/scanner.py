"""Scan orchestration.

Runs in a background worker thread (see ui/workers.py). Communicates
progress via plain callbacks so it has no Qt dependency and is testable
on its own.

Pipeline per the design plan:
  Tier 0  index walk           (stat only, cheap)
  Tier 1  probe metadata + exact hashing (partial then full)
  Tier 2  perceptual fingerprint for files with a usable duration

Cancellation is cooperative: a threading.Event is checked between files,
and per-file status is persisted so a cancelled/crashed scan resumes with
zero recomputation.
"""
from __future__ import annotations

import os
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import config, db, ffmpeg, fingerprint, hashing

log = logging.getLogger(__name__)


@dataclass
class Source:
    root: str
    recursive: bool = True
    extensions: list[str] = field(default_factory=lambda: list(config.DEFAULT_EXTENSIONS))
    min_size: int = 0
    ignore_hidden: bool = config.DEFAULT_IGNORE_HIDDEN
    detect_near_duplicates: bool = config.DEFAULT_NEAR_DUPLICATES


ProgressCb = Callable[[str, int, int], None]   # (stage, done, total)
LogCb = Callable[[str], None]                  # human-readable line
ScanErrorCb = Callable[[str, str, str], None]  # (path, stage, error)


class Scanner:
    def __init__(self, sources: list[Source],
                 progress: ProgressCb, log: LogCb,
                 cancel_event: threading.Event):
        self.sources = sources
        self.progress = progress
        self.log = log
        self.cancel = cancel_event
        self.detect_near_duplicates = any(s.detect_near_duplicates for s in sources)
        self.scan_id: int | None = None

    # --- Tier 0: index walk -------------------------------------------------
    def _iter_files(self, record_error: ScanErrorCb | None = None):
        """Yield every matching file under each source.

        The walk is recursive at every depth when requested, but directory
        symlinks are deliberately not followed. That keeps overlapping sources
        and symlink cycles from indexing the same tree forever.
        """
        seen_paths: set[str] = set()
        for src in self.sources:
            if self.cancel.is_set():
                break
            root = _normalize_path(Path(src.root).expanduser())
            exts = _normalize_extensions(src.extensions or config.DEFAULT_EXTENSIONS)
            self.log(f"[debug] scanning {root} with extensions: {sorted(exts)}")
            if not root.is_dir():
                self.log(f"[skip] source is not a folder: {root}")
                log.warning("Scan source is not a directory: %s", root)
                continue

            def onerror(exc: OSError) -> None:
                path = getattr(exc, "filename", None) or str(root)
                msg = str(exc)
                self.log(f"[skip] cannot read folder: {path}: {msg}")
                log.warning("Cannot read folder %s: %s", path, msg)
                if record_error:
                    record_error(str(path), "walk", msg)

            for dirpath, dirnames, filenames in os.walk(
                root,
                topdown=True,
                onerror=onerror,
                followlinks=False,
            ):
                if self.cancel.is_set():
                    break

                current_dir = Path(dirpath)
                if src.recursive:
                    dirnames[:] = self._descendable_dirs(
                        current_dir, dirnames, src, record_error
                    )
                else:
                    dirnames.clear()

                for filename in sorted(filenames):
                    if self.cancel.is_set():
                        break

                    p = current_dir / filename
                    if src.ignore_hidden and filename.startswith("."):
                        self.log(f"[skip] hidden file: {p}")
                        continue

                    normalized = _normalize_path(p)
                    if any(normalized.match(g) for g in config.SKIP_GLOBS):
                        self.log(f"[skip] matching glob: {normalized}")
                        continue
                    if normalized.suffix.lower() not in exts:
                        self.log(f"[skip] wrong extension: {normalized}")
                        continue

                    path_key = str(normalized)
                    if path_key in seen_paths:
                        self.log(f"[skip] duplicate path already indexed: {normalized}")
                        continue
                    seen_paths.add(path_key)
                    yield path_key, src.min_size

    def _descendable_dirs(
        self,
        parent: Path,
        dirnames: list[str],
        src: Source,
        record_error: ScanErrorCb | None,
    ) -> list[str]:
        out: list[str] = []
        for dirname in sorted(dirnames):
            child = parent / dirname
            if src.ignore_hidden and dirname.startswith("."):
                self.log(f"[skip] hidden folder: {child}")
                continue
            try:
                if child.is_symlink():
                    self.log(f"[skip] symlinked folder: {child}")
                    continue
            except OSError as exc:
                msg = str(exc)
                self.log(f"[skip] cannot inspect folder: {child}: {msg}")
                log.warning("Cannot inspect folder %s: %s", child, msg)
                if record_error:
                    record_error(str(child), "walk", msg)
                continue
            out.append(dirname)
        return out

    def run(self) -> str:
        """Execute the full scan. Returns a short status string."""
        conn = db.connect()
        try:
            return self._run(conn)
        finally:
            conn.close()

    def _run(self, conn) -> str:
        root_paths = "\n".join(src.root for src in self.sources)
        scan_id = db.create_scan(conn, root_paths)
        self.scan_id = scan_id
        conn.commit()
        scan_started = time.time()
        log.info("Scan %s started for %s", scan_id, root_paths)
        done = failed = 0

        # ---- Tier 0: index --------------------------------------------------
        self.progress("Indexing", 0, 0)
        indexed = 0

        def record_index_error(path: str, stage: str, error: str) -> None:
            db.record_scan_error(conn, scan_id, path, stage, error)

        for path, min_size in self._iter_files(record_index_error):
            st = hashing.stat_tuple(path)
            if st is None:
                self.log(f"[error] cannot stat: {path}")
                db.record_scan_error(conn, scan_id, path, "stat", "cannot stat")
                failed += 1
                continue
            size, mtime, inode, device = st
            if min_size and size < min_size:
                continue
            ext = Path(path).suffix.lower()
            file_id, _reused = db.upsert_index_row(
                conn,
                path,
                size,
                mtime,
                inode,
                device,
                ext,
            )
            db.record_scan_file(conn, scan_id, file_id)
            indexed += 1
            if indexed % 200 == 0:
                db.update_scan_progress(conn, scan_id, indexed, done, failed)
                conn.commit()
                self.progress("Indexing", indexed, 0)
        conn.commit()
        if self.cancel.is_set():
            db.finish_scan(conn, scan_id, "cancelled")
            conn.commit()
            log.info("Scan %s cancelled during indexing", scan_id)
            return f"Cancelled during indexing ({indexed} files)."

        removed = db.prune_missing_since(conn, scan_started)
        requeued = db.mark_unhashed_size_candidates(conn, scan_id=scan_id)
        
        requeued_frames = 0
        if self.detect_near_duplicates:
            requeued_frames = db.mark_unfingerprinted_candidates(
                conn,
                config.MIN_FINGERPRINT_MATCH_FRAMES,
                scan_id=scan_id,
            )
            
        conn.commit()
        if removed:
            self.log(f"[prune] removed {removed} stale rows for missing files")
        if requeued:
            self.log(f"[resume] queued {requeued} cached same-size file(s) "
                     "for exact hash confirmation")
        if requeued_frames:
            self.log(f"[resume] queued {requeued_frames} cached file(s) "
                     "for perceptual fingerprinting")

        # ---- Tier 1 + 2: process backlog -----------------------------------
        total = db.count_pending(conn, scan_id=scan_id)
        self.log(f"{indexed} indexed, {total} need processing "
                 f"({indexed - total} reused from cache)")

        i = 0
        batch_size = max(1, min(config.SCAN_BATCH_SIZE, 250))
        while not self.cancel.is_set():
            batch = db.claim_pending_batch(conn, batch_size, scan_id=scan_id)
            if not batch:
                break
            for row in batch:
                if self.cancel.is_set():
                    db.mark_status(
                        conn,
                        row["id"],
                        db.ST_INDEXED,
                        "Cancelled before this file was processed.",
                    )
                    continue
                ok = self._process_one(conn, row, scan_id)
                done += 1 if ok else 0
                failed += 0 if ok else 1
                i += 1
                if i % 10 == 0:
                    self.progress("Processing", i, total)
            db.update_scan_progress(conn, scan_id, indexed, done, failed)
            conn.commit()
            self.progress("Processing", i, total)

        if self.cancel.is_set():
            conn.execute(
                "UPDATE files SET status=?, error=? WHERE status=?",
                (db.ST_INDEXED, "Cancelled scan reset this file.", db.ST_RUNNING),
            )
            db.finish_scan(conn, scan_id, "cancelled")
            db.update_scan_progress(conn, scan_id, indexed, done, failed)
            conn.commit()
            log.info("Scan %s cancelled after %s/%s files", scan_id, i, total)
            return (f"Cancelled — {i}/{total} processed. "
                    f"Re-run to resume.")

        db.finish_scan(conn, scan_id, "completed")
        db.update_scan_progress(conn, scan_id, indexed, done, failed)
        conn.commit()
        log.info(
            "Scan %s completed: indexed=%s processed=%s failed=%s",
            scan_id, indexed, done, failed,
        )
        return f"Scan complete: {indexed} files, {done} processed, {failed} failed."

    # --- per-file work (errors are isolated, never abort the scan) ----------
    def _process_one(self, conn, row, scan_id: int | None = None) -> bool:
        fid = row["id"]
        path = row["path"]
        size = row["size"]
        try:
            if not Path(path).exists():
                db.mark_status(conn, fid, db.ST_ERROR, "file vanished")
                db.record_scan_error(conn, scan_id, path, "exists", "file vanished")
                conn.commit()
                return False

            # Tier 1a: metadata
            meta = ffmpeg.ffprobe_metadata(path)
            db.set_metadata(conn, fid, meta)

            # Tier 1b: exact hashing. Per the plan, full hashing is only
            # needed when size grouping says an exact duplicate is possible.
            fast = full = None
            if db.count_same_size(conn, size, scan_id=scan_id) > 1:
                fast = hashing.partial_hash(path, size)
                full = hashing.full_hash(path)
            db.set_hashes(conn, fid, fast, full)

            # Tier 2: perceptual fingerprint. This is intentionally opt-in
            # because it is far more expensive than exact hash confirmation.
            dur = meta.get("duration")
            if self.detect_near_duplicates and dur and dur > 0:
                fp = fingerprint.fingerprint(path, dur)
                if fp:
                    db.set_frames(conn, fid, fp)
                    if len(fp) < config.MIN_FINGERPRINT_MATCH_FRAMES:
                        self.log(
                            f"[warn] only {len(fp)} usable frame(s) extracted: "
                            f"{Path(path).name}"
                        )
                else:
                    self.log(f"[warn] no frames extracted: {Path(path).name}")

            db.mark_status(conn, fid, db.ST_DONE, None)
            return True
        except Exception as exc:  # noqa: BLE001 - isolate ANY per-file failure
            db.mark_status(conn, fid, db.ST_ERROR, repr(exc))
            db.record_scan_error(conn, scan_id, path, "process", repr(exc))
            log.exception("File processing failed: %s", path)
            self.log(f"[error] {Path(path).name}: {exc}")
            return False

def _normalize_extensions(extensions: list[str]) -> set[str]:
    out: set[str] = set()
    for ext in extensions:
        ext = ext.strip().lower()
        if not ext:
            continue
        out.add(ext if ext.startswith(".") else f".{ext}")
    return out


def _normalize_path(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return path.absolute()
