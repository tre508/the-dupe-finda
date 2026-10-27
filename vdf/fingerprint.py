"""Near-duplicate fingerprinting via per-frame perceptual hashing.

Strategy (kept deliberately simple for a solo developer):
  * Sample FRAME_SAMPLES frames spread evenly across the ENTIRE duration,
    not just the first seconds. Whole-duration sampling is what makes this
    robust to differing intros / black leader / ad bumpers.
  * pHash each frame -> 64-bit int. A file's fingerprint is that ordered
    vector of ints.
  * Two files match if their mean per-frame Hamming distance is low AND
    durations are within tolerance.
"""
from __future__ import annotations

from dataclasses import dataclass
import tempfile
from pathlib import Path

import imagehash
from PIL import Image

from . import config, ffmpeg

HashSample = tuple[float, int]
MASK_64 = (1 << 64) - 1


@dataclass(frozen=True)
class FingerprintComparison:
    mean_distance: float
    compared_frames: int
    max_distance: int


def _hamming(a: int, b: int) -> int:
    return ((a ^ b) & MASK_64).bit_count()


def _sqlite_i64(value: int) -> int:
    """Convert an unsigned 64-bit pHash to SQLite's signed INTEGER range."""
    value &= MASK_64
    if value >= (1 << 63):
        value -= (1 << 64)
    return value


def fingerprint(path: str, duration: float) -> list[HashSample]:
    """Return [(fraction_of_duration, phash_int), ...].

    Returns [] if frames could not be extracted (caller should mark error
    and continue the scan).
    """
    if not duration or duration <= 0:
        return []

    n = config.FRAME_SAMPLES
    # Evenly spaced fractions, e.g. n=12 -> 1/13 .. 12/13 (avoid the very
    # first and last frames which are often black / leader / credits).
    fractions = [(i + 1) / (n + 1) for i in range(n)]

    out: list[HashSample] = []
    with tempfile.TemporaryDirectory(prefix="vdf_fp_") as tmp:
        for frac in fractions:
            ts = duration * frac
            img_path = str(Path(tmp) / f"f_{frac:.4f}.png")
            if not ffmpeg.extract_frame(path, ts, img_path, width=64):
                continue
            try:
                with Image.open(img_path) as im:
                    ph = imagehash.phash(im)  # 8x8 -> 64 bits
                out.append((round(frac, 4), _sqlite_i64(int(str(ph), 16))))
            except (OSError, ValueError):
                continue
    return out


def durations_compatible(d1: float, d2: float) -> bool:
    if not d1 or not d2:
        return False
    tol = max(config.DURATION_TOLERANCE_ABS,
              max(d1, d2) * config.DURATION_TOLERANCE_PCT)
    return abs(d1 - d2) <= tol


def compare(fp1: list[HashSample],
            fp2: list[HashSample],
            min_pairs: int = 3) -> FingerprintComparison | None:
    """Compare overlapping frame hashes.

    Returns None if there is nothing comparable.
    """
    if not fp1 or not fp2:
        return None
        
    # Match frames strictly by their timestamp fraction to avoid comparing
    # misaligned frames if some extractions failed.
    dict2 = {frac: val for frac, val in fp2}
    
    distances: list[int] = []
    
    for frac, val1 in fp1:
        if frac in dict2:
            distances.append(_hamming(val1, dict2[frac]))
            
    # Require a minimum number of valid overlapping frames to confidently compare.
    # Sparse overlap is often black frames, leaders, or extraction fallout.
    if len(distances) < min_pairs:
        return None
        
    return FingerprintComparison(
        mean_distance=sum(distances) / len(distances),
        compared_frames=len(distances),
        max_distance=max(distances),
    )


def mean_distance(fp1: list[HashSample],
                  fp2: list[HashSample]) -> float | None:
    """Mean per-frame Hamming distance over overlapping samples."""
    result = compare(fp1, fp2, min_pairs=3)
    return result.mean_distance if result else None
