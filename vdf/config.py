"""Central configuration: XDG paths, constants, tunable thresholds.

Everything that a user might reasonably want to tweak lives here so the
rest of the codebase never hard-codes magic numbers.
"""
from __future__ import annotations

import os
from pathlib import Path

APP_ID = "duplicate-video-finder"
APP_NAME = "The File Hashing Slasher"
LEGACY_APP_NAME = "video-dedupe"

# --- XDG-compliant locations -------------------------------------------------
def _xdg(env: str, default: Path) -> Path:
    val = os.environ.get(env)
    return Path(val) if val else default


HOME = Path.home()
CONFIG_DIR = _xdg("XDG_CONFIG_HOME", HOME / ".config") / APP_ID
CACHE_DIR = _xdg("XDG_CACHE_HOME", HOME / ".cache") / APP_ID
STATE_DIR = _xdg("XDG_STATE_HOME", HOME / ".local" / "state") / APP_ID
DATA_DIR = _xdg("XDG_DATA_HOME", HOME / ".local" / "share") / APP_ID

DB_PATH = STATE_DIR / "library.sqlite3"
THUMB_DIR = CACHE_DIR / "thumbs"
LOG_PATH = STATE_DIR / "deletions.log"
LOG_FILE = STATE_DIR / "app.log"
FATAL_LOG_FILE = STATE_DIR / "fatal-crash.log"
USER_ICON_SVG = (
    _xdg("XDG_DATA_HOME", HOME / ".local" / "share")
    / "icons"
    / "hicolor"
    / "scalable"
    / "apps"
    / f"{APP_ID}.svg"
)


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, CACHE_DIR, STATE_DIR, DATA_DIR, THUMB_DIR):
        d.mkdir(parents=True, exist_ok=True)


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def bundled_icon_svg() -> Path:
    return project_root() / "resources" / "icons" / f"{APP_ID}.svg"


def best_icon_path() -> Path:
    for path in (USER_ICON_SVG, bundled_icon_svg()):
        if path.exists():
            return path
    return bundled_icon_svg()


# --- Scanning defaults -------------------------------------------------------
DEFAULT_EXTENSIONS = [
    ".mp4", ".mkv", ".avi", ".mov", ".webm",
    ".m4v", ".wmv", ".flv", ".ts",
]

# Skip in-progress / partial download artefacts.
SKIP_GLOBS = ("*.part", "*.crdownload", "*.!ut")
SCAN_BATCH_SIZE = 50
DEFAULT_IGNORE_HIDDEN = True
DEFAULT_MIN_SIZE_MB = 0
DEFAULT_NEAR_DUPLICATES = False

# --- Hashing -----------------------------------------------------------------
PARTIAL_CHUNK = 1 * 1024 * 1024          # 1 MiB head/mid/tail samples
STREAM_CHUNK = 1 * 1024 * 1024           # 1 MiB streaming read for full hash

# --- Near-duplicate detection ------------------------------------------------
# Sample this many frames spread evenly across the WHOLE duration.
FRAME_SAMPLES = 12
# Two files are only ever compared as near-dupes if their durations are within
# this tolerance. This is the cheap pre-filter that keeps large libraries sane.
DURATION_TOLERANCE_PCT = 0.02            # 2%
DURATION_TOLERANCE_ABS = 2.0             # or 2 seconds
# Strict tolerances for high-confidence video matches. These are deliberately
# conservative: uncertain matches belong in the manual-review bucket.
LIKELY_DURATION_TOLERANCE_PCT = 0.002    # 0.2%
LIKELY_DURATION_TOLERANCE_ABS = 0.5      # or 0.5 seconds
LIKELY_SIZE_TOLERANCE_PCT = 0.05         # 5%
REVIEW_SIZE_TOLERANCE_PCT = 0.10         # 10%
# Mean per-frame Hamming distance (on a 64-bit pHash) thresholds.
# These are tuned for catching re-encodes (different quality/bitrate) while
# minimizing false positives. Distance is measured on 64-bit perceptual hashes.
PHASH_LIKELY_SAME = 1                    # <= this  -> "Likely same video"
PHASH_REVIEW = 3                         # <= this  -> "Needs manual review"
# > PHASH_REVIEW                          -> not considered a pair
PHASH_LIKELY_MAX_FRAME_DISTANCE = 4
PHASH_REVIEW_MAX_FRAME_DISTANCE = 8
MIN_FINGERPRINT_MATCH_FRAMES = 10

# --- Thumbnails --------------------------------------------------------------
THUMB_WIDTH = 240                        # px; height auto (keeps aspect)
THUMB_SEEK_FRACTION = 0.20               # grab a frame ~20% into the video

# --- UI ----------------------------------------------------------------------
PREVIEW_SECONDS = 15                     # length of the review preview clip
