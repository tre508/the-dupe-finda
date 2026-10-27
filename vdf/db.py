"""SQLite persistence layer.

Design choices that matter:
  * WAL mode so the GUI thread can read while a worker thread writes.
  * The natural cache key is (path, size, mtime, inode, device). If those
    are unchanged since a prior scan, every expensive stage is skipped on
    rescan. Inode/device changes invalidate cached identity evidence even
    when size/mtime happen to be unchanged.
  * One connection PER THREAD. sqlite3 connections are not safe to share
    across threads; callers obtain a connection via connect() in the
    thread that uses it.
"""
from __future__ import annotations

import sqlite3
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    inode       INTEGER,
    device      INTEGER,
    ext         TEXT,
    duration    REAL,
    width       INTEGER,
    height      INTEGER,
    vcodec      TEXT,
    container   TEXT,
    fast_hash   TEXT,
    full_hash   TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    error       TEXT,
    last_seen   REAL
);
CREATE INDEX IF NOT EXISTS idx_files_size     ON files(size);
CREATE INDEX IF NOT EXISTS idx_files_dur      ON files(duration);
CREATE INDEX IF NOT EXISTS idx_files_fullhash ON files(full_hash);

CREATE TABLE IF NOT EXISTS frame_hashes (
    file_id   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    position  REAL NOT NULL,
    phash     INTEGER NOT NULL,
    PRIMARY KEY (file_id, position)
);

CREATE TABLE IF NOT EXISTS deletions (
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,
    path      TEXT NOT NULL,
    size      INTEGER,
    action    TEXT NOT NULL,
    group_key TEXT
);

CREATE TABLE IF NOT EXISTS scans (
    id           INTEGER PRIMARY KEY,
    root_paths   TEXT NOT NULL,
    status       TEXT NOT NULL,
    started_at   REAL NOT NULL,
    finished_at  REAL,
    files_seen   INTEGER NOT NULL DEFAULT 0,
    files_done   INTEGER NOT NULL DEFAULT 0,
    files_failed INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT
);

CREATE TABLE IF NOT EXISTS scan_files (
    scan_id   INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    file_id   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    PRIMARY KEY (scan_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_scan_files_file ON scan_files(file_id);

CREATE TABLE IF NOT EXISTS scan_errors (
    id        INTEGER PRIMARY KEY,
    scan_id   INTEGER REFERENCES scans(id) ON DELETE CASCADE,
    path      TEXT NOT NULL,
    stage     TEXT NOT NULL,
    error     TEXT NOT NULL,
    ts        REAL NOT NULL
);
"""

# Status lifecycle for a file row.
ST_PENDING = "pending"
ST_INDEXED = "indexed"          # path/size/mtime known, nothing computed
ST_DONE = "done"                # probe + hash (+ fingerprint) complete
ST_ERROR = "error"
ST_RUNNING = "running"


def connect() -> sqlite3.Connection:
    """Return a new connection for the CURRENT thread. WAL + sane pragmas."""
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def backup_db(label: str) -> Path:
    config.ensure_dirs()
    backup_dir = config.STATE_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"{config.DB_PATH.name}.{label}.{stamp}"
    shutil.copy2(config.DB_PATH, backup)
    return backup


def verify_or_recreate() -> bool:
    """Return True when the existing DB was usable, False if recreated."""
    config.ensure_dirs()
    if not config.DB_PATH.exists():
        init_db()
        return True

    try:
        conn = connect()
        result = conn.execute("PRAGMA integrity_check;").fetchone()[0]
        conn.close()
        if str(result).lower() != "ok":
            raise RuntimeError(f"integrity_check returned {result!r}")
        return True
    except Exception:
        if config.DB_PATH.exists():
            backup_db("corrupt")
            config.DB_PATH.unlink(missing_ok=True)
        init_db()
        return False


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def recover_interrupted_scans(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT root_paths
        FROM scans
        WHERE status IN ('running', 'cancelling')
        ORDER BY id DESC
        """
    ).fetchall()
    cur = conn.execute(
        """
        UPDATE scans
        SET status='crashed',
            finished_at=?,
            last_error='App restarted while this scan was still running.'
        WHERE status IN ('running', 'cancelling')
        """,
        (time.time(),),
    )
    conn.execute(
        "UPDATE files SET status=?, error=? WHERE status=?",
        (ST_INDEXED, "Interrupted scan reset this file for safe retry.", ST_RUNNING),
    )
    roots: list[str] = []
    for row in rows:
        roots.extend(p for p in str(row["root_paths"]).splitlines() if p)
    return roots


def create_scan(conn: sqlite3.Connection, root_paths: str) -> int:
    cur = conn.execute(
        "INSERT INTO scans(root_paths,status,started_at) VALUES(?,?,?)",
        (root_paths, "running", time.time()),
    )
    return int(cur.lastrowid)


def update_scan_progress(conn: sqlite3.Connection, scan_id: int,
                         seen: int, done: int, failed: int) -> None:
    conn.execute(
        """
        UPDATE scans
        SET files_seen=?, files_done=?, files_failed=?
        WHERE id=?
        """,
        (seen, done, failed, scan_id),
    )


def finish_scan(conn: sqlite3.Connection, scan_id: int, status: str,
                error: str | None = None) -> None:
    conn.execute(
        """
        UPDATE scans
        SET status=?, finished_at=?, last_error=?
        WHERE id=?
        """,
        (status, time.time(), error, scan_id),
    )


def record_scan_error(conn: sqlite3.Connection, scan_id: int | None,
                      path: str, stage: str, error: str) -> None:
    conn.execute(
        "INSERT INTO scan_errors(scan_id,path,stage,error,ts) VALUES(?,?,?,?,?)",
        (scan_id, path, stage, error[:2000], time.time()),
    )


# --- Index pass --------------------------------------------------------------
def upsert_index_row(conn: sqlite3.Connection, path: str, size: int,
                     mtime: float, inode: int, device: int,
                     ext: str) -> tuple[int, bool]:
    """Insert or refresh a file row from a cheap stat() walk.

    Returns (file_id, reused), where reused is True if the file is unchanged
    and already fully processed.
    """
    now = time.time()
    row = conn.execute(
        "SELECT id, size, mtime, inode, device, status FROM files WHERE path=?",
        (path,),
    ).fetchone()

    if row is None:
        cur = conn.execute(
            "INSERT INTO files(path,size,mtime,inode,device,ext,status,last_seen)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (path, size, mtime, inode, device, ext, ST_INDEXED, now),
        )
        return int(cur.lastrowid), False

    unchanged = (
        row["size"] == size
        and abs(row["mtime"] - mtime) < 1e-6
        and row["inode"] == inode
        and row["device"] == device
    )
    if unchanged and row["status"] == ST_DONE:
        conn.execute(
            "UPDATE files SET ext=?, last_seen=? WHERE id=?",
            (ext, now, row["id"]),
        )
        return int(row["id"]), True

    # Changed on disk, including file identity changes with the same size/mtime
    # -> invalidate cached compute and reset to indexed.
    conn.execute(
        "UPDATE files SET size=?,mtime=?,inode=?,device=?,ext=?,"
        "duration=NULL,width=NULL,height=NULL,vcodec=NULL,container=NULL,"
        "fast_hash=NULL,full_hash=NULL,status=?,error=NULL,last_seen=? "
        "WHERE id=?",
        (size, mtime, inode, device, ext, ST_INDEXED, now, row["id"]),
    )
    conn.execute("DELETE FROM frame_hashes WHERE file_id=?", (row["id"],))
    return int(row["id"]), False


def upsert_index(conn: sqlite3.Connection, path: str, size: int,
                 mtime: float, inode: int, device: int, ext: str) -> bool:
    """Insert or refresh a file row from a cheap stat() walk.

    Returns True if the file is unchanged and already fully processed.
    """
    _file_id, reused = upsert_index_row(
        conn,
        path,
        size,
        mtime,
        inode,
        device,
        ext,
    )
    return reused


def record_scan_file(conn: sqlite3.Connection, scan_id: int, file_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO scan_files(scan_id,file_id) VALUES(?,?)",
        (scan_id, file_id),
    )


def pending_files(conn: sqlite3.Connection,
                  scan_id: int | None = None) -> list[sqlite3.Row]:
    """Files that still need probe/hash/fingerprint work."""
    if scan_id is not None:
        return conn.execute(
            """
            SELECT f.*
            FROM files f
            JOIN scan_files sf ON sf.file_id=f.id
            WHERE sf.scan_id=? AND f.status IN (?,?)
            ORDER BY f.size
            """,
            (scan_id, ST_INDEXED, ST_PENDING),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM files WHERE status IN (?,?) ORDER BY size",
        (ST_INDEXED, ST_PENDING),
    ).fetchall()


def claim_pending_batch(
    conn: sqlite3.Connection,
    limit: int,
    scan_id: int | None = None,
) -> list[sqlite3.Row]:
    """Atomically claim a small batch for processing."""
    conn.execute("BEGIN IMMEDIATE;")
    if scan_id is None:
        rows = conn.execute(
            "SELECT * FROM files WHERE status IN (?,?) ORDER BY size LIMIT ?",
            (ST_INDEXED, ST_PENDING, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT f.*
            FROM files f
            JOIN scan_files sf ON sf.file_id=f.id
            WHERE sf.scan_id=? AND f.status IN (?,?)
            ORDER BY f.size
            LIMIT ?
            """,
            (scan_id, ST_INDEXED, ST_PENDING, limit),
        ).fetchall()
    if rows:
        conn.executemany(
            "UPDATE files SET status=? WHERE id=?",
            [(ST_RUNNING, r["id"]) for r in rows],
        )
    conn.commit()
    return rows


def count_pending(conn: sqlite3.Connection, scan_id: int | None = None) -> int:
    if scan_id is not None:
        return int(conn.execute(
            """
            SELECT COUNT(*)
            FROM files f
            JOIN scan_files sf ON sf.file_id=f.id
            WHERE sf.scan_id=? AND f.status IN (?,?)
            """,
            (scan_id, ST_INDEXED, ST_PENDING),
        ).fetchone()[0])
    return int(conn.execute(
        "SELECT COUNT(*) FROM files WHERE status IN (?,?)",
        (ST_INDEXED, ST_PENDING),
    ).fetchone()[0])


def count_same_size(conn: sqlite3.Connection, size: int,
                    scan_id: int | None = None) -> int:
    if scan_id is not None:
        return int(conn.execute(
            """
            SELECT COUNT(*)
            FROM files f
            JOIN scan_files sf ON sf.file_id=f.id
            WHERE sf.scan_id=? AND f.size=?
            """,
            (scan_id, size),
        ).fetchone()[0])
    return int(conn.execute(
        "SELECT COUNT(*) FROM files WHERE size=?",
        (size,),
    ).fetchone()[0])


def mark_unhashed_size_candidates(
    conn: sqlite3.Connection,
    scan_id: int | None = None,
) -> int:
    """Requeue unchanged rows that became exact-duplicate candidates.

    A file can be unique during one scan, so we intentionally skip its full
    hash. If a later scan adds another file with the same size, the old row
    must be processed again so exact grouping has confirmed hashes for both
    copies.
    """
    if scan_id is not None:
        cur = conn.execute(
            """
            UPDATE files
            SET status=?, error=NULL
            WHERE id IN (
                SELECT f.id
                FROM files f
                JOIN scan_files sf ON sf.file_id=f.id
                WHERE sf.scan_id=?
                  AND f.status=?
                  AND f.full_hash IS NULL
                  AND f.size IN (
                      SELECT f2.size
                      FROM files f2
                      JOIN scan_files sf2 ON sf2.file_id=f2.id
                      WHERE sf2.scan_id=?
                      GROUP BY f2.size
                      HAVING COUNT(*) > 1
                  )
            )
            """,
            (ST_INDEXED, scan_id, ST_DONE, scan_id),
        )
        return int(cur.rowcount or 0)
    cur = conn.execute(
        """
        UPDATE files
        SET status=?, error=NULL
        WHERE status=?
          AND full_hash IS NULL
          AND size IN (
              SELECT size
              FROM files
              GROUP BY size
              HAVING COUNT(*) > 1
          )
        """,
        (ST_INDEXED, ST_DONE),
    )
    return int(cur.rowcount or 0)


def mark_unfingerprinted_candidates(
    conn: sqlite3.Connection,
    min_frames: int = 1,
    scan_id: int | None = None,
) -> int:
    """Requeue unchanged rows with missing or incomplete fingerprints.
    
    If a user runs a scan without near-duplicate detection, files get ST_DONE 
    but have no frames. If they later enable near-duplicate detection, those
    files must be requeued so they get fingerprinted. Rows with too few frame
    hashes are also retried because sparse cached fingerprints are not safe
    evidence for duplicate grouping.
    """
    if scan_id is not None:
        cur = conn.execute(
            """
            UPDATE files
            SET status=?, error=NULL
            WHERE id IN (
                SELECT f.id
                FROM files f
                JOIN scan_files sf ON sf.file_id=f.id
                WHERE sf.scan_id=?
                  AND f.status=?
                  AND f.duration > 0
                  AND (
                      SELECT COUNT(*)
                      FROM frame_hashes
                      WHERE file_id=f.id
                  ) < ?
            )
            """,
            (ST_INDEXED, scan_id, ST_DONE, min_frames),
        )
        return int(cur.rowcount or 0)
    cur = conn.execute(
        """
        UPDATE files
        SET status=?, error=NULL
        WHERE status=?
          AND duration > 0
          AND (
              SELECT COUNT(*)
              FROM frame_hashes
              WHERE file_id=files.id
          ) < ?
        """,
        (ST_INDEXED, ST_DONE, min_frames),
    )
    return int(cur.rowcount or 0)


def set_metadata(conn: sqlite3.Connection, file_id: int, meta: dict) -> None:
    conn.execute(
        "UPDATE files SET duration=?,width=?,height=?,vcodec=?,container=? "
        "WHERE id=?",
        (meta.get("duration"), meta.get("width"), meta.get("height"),
         meta.get("vcodec"), meta.get("container"), file_id),
    )


def set_hashes(conn: sqlite3.Connection, file_id: int,
               fast_hash: Optional[str], full_hash: Optional[str]) -> None:
    conn.execute(
        "UPDATE files SET fast_hash=?, full_hash=? WHERE id=?",
        (fast_hash, full_hash, file_id),
    )


def set_frames(conn: sqlite3.Connection, file_id: int,
               frames: list[tuple[float, int]]) -> None:
    conn.execute("DELETE FROM frame_hashes WHERE file_id=?", (file_id,))
    conn.executemany(
        "INSERT INTO frame_hashes(file_id,position,phash) VALUES(?,?,?)",
        [(file_id, pos, ph) for pos, ph in frames],
    )


def mark_status(conn: sqlite3.Connection, file_id: int,
                status: str, error: str | None = None) -> None:
    conn.execute(
        "UPDATE files SET status=?, error=? WHERE id=?",
        (status, error, file_id),
    )


# --- Read helpers used by grouping & UI -------------------------------------
def all_done_files(conn: sqlite3.Connection,
                   scan_id: int | None = None) -> list[sqlite3.Row]:
    if scan_id is not None:
        return conn.execute(
            """
            SELECT f.*
            FROM files f
            JOIN scan_files sf ON sf.file_id=f.id
            WHERE sf.scan_id=? AND f.status=?
            ORDER BY f.size DESC
            """,
            (scan_id, ST_DONE),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM files WHERE status=? ORDER BY size DESC", (ST_DONE,)
    ).fetchall()


def frames_for(conn: sqlite3.Connection, file_id: int) -> list[tuple[float, int]]:
    rows = conn.execute(
        "SELECT position, phash FROM frame_hashes WHERE file_id=? "
        "ORDER BY position", (file_id,)
    ).fetchall()
    return [(r["position"], r["phash"]) for r in rows]


def prune_missing(conn: sqlite3.Connection, existing_paths: set[str]) -> int:
    """Drop rows whose files no longer exist on disk. Returns count removed."""
    rows = conn.execute("SELECT id, path FROM files").fetchall()
    gone = [r["id"] for r in rows if r["path"] not in existing_paths
            and not Path(r["path"]).exists()]
    if gone:
        conn.executemany("DELETE FROM files WHERE id=?", [(i,) for i in gone])
    return len(gone)


def prune_missing_since(conn: sqlite3.Connection, scan_started: float) -> int:
    rows = conn.execute(
        "SELECT id, path FROM files WHERE last_seen < ? LIMIT 5000",
        (scan_started,),
    ).fetchall()
    gone = [r["id"] for r in rows if not Path(r["path"]).exists()]
    if gone:
        conn.executemany("DELETE FROM files WHERE id=?", [(i,) for i in gone])
    return len(gone)


def log_deletion(conn: sqlite3.Connection, path: str, size: int,
                 action: str, group_key: str) -> None:
    conn.execute(
        "INSERT INTO deletions(ts,path,size,action,group_key) VALUES(?,?,?,?,?)",
        (time.strftime("%Y-%m-%d %H:%M:%S"), path, size, action, group_key),
    )


def remove_file_row(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("DELETE FROM files WHERE path=?", (path,))
