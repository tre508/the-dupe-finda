"""File stat and hashing helpers.

The scanner uses a cheap stat pass first, then stores both a sampled hash and
a full-content hash. The sampled hash is useful for quick diagnostics/cache
metadata; grouping relies on the full hash for byte-identical duplicates.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

from . import config

StatTuple = tuple[int, float, int, int]


def stat_tuple(path: str) -> Optional[StatTuple]:
    """Return (size, mtime, inode, device), or None if stat fails."""
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime, st.st_ino, st.st_dev


def partial_hash(path: str, size: int | None = None) -> str:
    """Hash head/middle/tail chunks plus size.

    This does not prove files are identical, but it is stable and cheap for
    large files. `full_hash()` is still used for exact duplicate grouping.
    """
    file_size = Path(path).stat().st_size if size is None else size
    chunk = config.PARTIAL_CHUNK
    h = hashlib.blake2b(digest_size=32)
    h.update(str(file_size).encode("ascii"))
    h.update(b"\0")

    if file_size <= 0:
        return h.hexdigest()

    offsets = _sample_offsets(file_size, chunk)
    with open(path, "rb") as f:
        for offset in offsets:
            f.seek(offset)
            data = f.read(min(chunk, file_size - offset))
            h.update(offset.to_bytes(8, "big", signed=False))
            h.update(len(data).to_bytes(4, "big", signed=False))
            h.update(data)
    return h.hexdigest()


def full_hash(path: str) -> str:
    """Return a streaming SHA-256 hash for the entire file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            data = f.read(config.STREAM_CHUNK)
            if not data:
                break
            h.update(data)
    return h.hexdigest()


def _sample_offsets(size: int, chunk: int) -> list[int]:
    if size <= chunk:
        return [0]

    offsets = {
        0,
        max(0, (size // 2) - (chunk // 2)),
        max(0, size - chunk),
    }
    return sorted(offsets)
