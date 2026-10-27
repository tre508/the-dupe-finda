"""Thin, robust wrappers around ffprobe / ffmpeg.

We shell out instead of using a binding (PyAV/OpenCV) on purpose: a corrupt
or exotic file then crashes a short-lived child process, never the app.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Hard timeouts so one pathological file can never wedge a scan.
PROBE_TIMEOUT = 30
FRAME_TIMEOUT = 30


class FFmpegMissing(RuntimeError):
    pass


def check_tools() -> list[str]:
    """Return a list of missing required binaries (empty == all present)."""
    missing = []
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            missing.append(tool)
    return missing


def ffprobe_metadata(path: str) -> dict:
    """Return {duration,width,height,vcodec,container} or {} on failure."""
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", path,
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT
        )
        if out.returncode != 0:
            err = (out.stderr or "").strip()
            if err:
                log.info("ffprobe failed for %s: %s", path, err[:500])
            return {}
        data = json.loads(out.stdout or "{}")
    except subprocess.TimeoutExpired:
        log.warning("ffprobe timed out for %s", path)
        return {}
    except (json.JSONDecodeError, OSError):
        log.exception("ffprobe metadata failed for %s", path)
        return {}

    fmt = data.get("format", {})
    vstream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
        {},
    )
    duration = None
    try:
        duration = float(fmt.get("duration"))
    except (TypeError, ValueError):
        try:
            duration = float(vstream.get("duration"))
        except (TypeError, ValueError):
            duration = None

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return {
        "duration": duration,
        "width": _int(vstream.get("width")),
        "height": _int(vstream.get("height")),
        "vcodec": vstream.get("codec_name"),
        "container": (fmt.get("format_name") or "").split(",")[0] or None,
    }


def extract_frame(path: str, timestamp: float, out_path: str,
                  width: int = 64) -> bool:
    """Extract ONE frame at `timestamp` seconds, scaled to `width`px wide.

    `-ss` before `-i` = fast (keyframe) seek; precision is irrelevant for
    perceptual hashing and this is enormously faster on long files.
    """
    cmd = [
        "ffmpeg", "-nostdin", "-y", "-ss", f"{max(timestamp, 0):.3f}",
        "-i", path, "-frames:v", "1",
        "-vf", f"scale={width}:-1",
        out_path,
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, timeout=FRAME_TIMEOUT
        )
        return r.returncode == 0 and Path(out_path).exists()
    except subprocess.TimeoutExpired:
        log.warning("ffmpeg frame extraction timed out for %s", path)
        return False
    except OSError:
        log.exception("ffmpeg frame extraction failed for %s", path)
        return False


def make_thumbnail(path: str, out_path: str, duration: Optional[float],
                   width: int) -> bool:
    """Grab a representative frame ~20% in for the results grid."""
    seek = (duration * 0.20) if duration and duration > 1 else 1.0
    return extract_frame(path, seek, out_path, width=width)


def make_preview_clip(path: str, out_path: str, seconds: int) -> bool:
    """Re-mux the first N seconds into a small mp4 for in-app review."""
    cmd = [
        "ffmpeg", "-nostdin", "-y", "-i", path,
        "-t", str(seconds),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-c:a", "aac", "-movflags", "+faststart",
        out_path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        return r.returncode == 0 and Path(out_path).exists()
    except subprocess.TimeoutExpired:
        log.warning("ffmpeg preview timed out for %s", path)
        return False
    except OSError:
        log.exception("ffmpeg preview failed for %s", path)
        return False
