"""Thumbnail cache helpers."""
from __future__ import annotations

from pathlib import Path

from . import config, ffmpeg


def ensure_thumb(file_id: int, path: str, duration: float | None) -> str | None:
    """Return a cached thumbnail path, generating it if needed."""
    config.ensure_dirs()
    out = config.THUMB_DIR / f"{file_id}.jpg"
    if _is_fresh(out, path):
        return str(out)

    tmp = out.with_suffix(".tmp.jpg")
    try:
        if tmp.exists():
            tmp.unlink()
        ok = ffmpeg.make_thumbnail(
            path, str(tmp), duration, width=config.THUMB_WIDTH
        )
        if not ok:
            return None
        tmp.replace(out)
        return str(out)
    except OSError:
        return None


def _is_fresh(thumb: Path, source: str) -> bool:
    try:
        return (
            thumb.exists()
            and thumb.stat().st_mtime >= Path(source).stat().st_mtime
        )
    except OSError:
        return False
