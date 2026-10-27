"""Turn processed files into reviewable duplicate groups.

Group types (badges shown in the UI):
  exact        - byte-identical full-file hash match
  likely_same  - strong video fingerprint with strict size/duration support
  review       - weak or borderline evidence; manual review required
  hardlink     - same inode/device, no extra disk space to reclaim

Filename, folder, extension, creator/category words, and metadata-only signals
are intentionally not duplicate evidence.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import config, db, fingerprint, hashing

log = logging.getLogger(__name__)

BADGE = {
    "exact": "Exact Duplicate",
    "likely_same": "High-Confidence Video Match",
    "review": "Possible Match / Manual Review",
    "hardlink": "Hardlinked Same File",
}

CONFIRMED_MATCH_TYPES = {"exact", "likely_same"}


@dataclass
class Member:
    file_id: int
    path: str
    size: int
    mtime: float | None
    duration: float | None
    width: int | None
    height: int | None
    vcodec: str | None
    container: str | None
    fast_hash: str | None
    full_hash: str | None


@dataclass
class Group:
    key: str
    match_type: str
    members: list[Member] = field(default_factory=list)
    reason: str = ""
    confidence: str = "High"
    manual_review: bool = False

    @property
    def total_size(self) -> int:
        return sum(m.size for m in self.members)

    @property
    def reclaimable(self) -> int:
        """Bytes freed if we keep the single largest member."""
        if len(self.members) < 2:
            return 0
        if self.match_type == "hardlink":
            return 0
        return self.total_size - max(m.size for m in self.members)

    @property
    def is_confirmed(self) -> bool:
        return self.match_type in CONFIRMED_MATCH_TYPES and not self.manual_review


@dataclass(frozen=True)
class GroupSummary:
    confirmed: int
    possible: int
    hardlink: int
    total: int


@dataclass(frozen=True)
class PairEvidence:
    a: int
    b: int
    match_type: str
    reason: str
    confidence: str
    manual_review: bool
    distance: float
    compared_frames: int
    max_distance: int
    size_ratio: float
    duration_diff: float

    @property
    def key(self) -> tuple[int, int]:
        return (min(self.a, self.b), max(self.a, self.b))


def summarize(groups: list[Group]) -> GroupSummary:
    confirmed = possible = hardlink = 0
    for group in groups:
        if group.match_type == "hardlink":
            hardlink += 1
        elif group.is_confirmed:
            confirmed += 1
        elif group.manual_review:
            possible += 1
    return GroupSummary(
        confirmed=confirmed,
        possible=possible,
        hardlink=hardlink,
        total=len(groups),
    )


def _mk_member(r) -> Member:
    return Member(r["id"], r["path"], r["size"], r["mtime"], r["duration"],
                  r["width"], r["height"], r["vcodec"], r["container"],
                  r["fast_hash"], r["full_hash"])


def _duration_within(
    d1: float | None,
    d2: float | None,
    abs_tol: float,
    pct_tol: float,
) -> bool:
    if not d1 or not d2:
        return False
    return abs(d1 - d2) <= max(abs_tol, max(d1, d2) * pct_tol)


def _duration_diff(d1: float | None, d2: float | None) -> float:
    if not d1 or not d2:
        return float("inf")
    return abs(d1 - d2)


def _size_ratio(size1: int | None, size2: int | None) -> float:
    s1 = int(size1 or 0)
    s2 = int(size2 or 0)
    denom = max(s1, s2)
    if denom <= 0:
        return 1.0
    return abs(s1 - s2) / denom


def _resolution_quality(r1, r2) -> tuple[bool, bool]:
    """Return (likely_ok, review_ok) for resolution/aspect evidence."""
    w1, h1 = r1["width"], r1["height"]
    w2, h2 = r2["width"], r2["height"]
    if not (w1 and h1 and w2 and h2):
        return False, True

    pixels1 = w1 * h1
    pixels2 = w2 * h2
    if pixels1 <= 0 or pixels2 <= 0:
        return False, True

    aspect1 = w1 / h1
    aspect2 = w2 / h2
    aspect_delta = abs(aspect1 - aspect2) / max(aspect1, aspect2)
    pixel_ratio = max(pixels1, pixels2) / min(pixels1, pixels2)

    likely_ok = w1 == w2 and h1 == h2
    review_ok = aspect_delta <= 0.03 and pixel_ratio <= 2.25
    return likely_ok, review_ok


def _near_pair_evidence(r1, r2, fp1, fp2) -> PairEvidence | None:
    if not _duration_within(
        r1["duration"],
        r2["duration"],
        config.DURATION_TOLERANCE_ABS,
        config.DURATION_TOLERANCE_PCT,
    ):
        return None

    size_ratio = _size_ratio(r1["size"], r2["size"])
    if size_ratio > config.REVIEW_SIZE_TOLERANCE_PCT:
        return None

    likely_resolution_ok, review_resolution_ok = _resolution_quality(r1, r2)
    if not review_resolution_ok:
        return None

    comparison = fingerprint.compare(
        fp1,
        fp2,
        min_pairs=config.MIN_FINGERPRINT_MATCH_FRAMES,
    )
    if comparison is None:
        return None
    if comparison.mean_distance > config.PHASH_REVIEW:
        return None
    if comparison.max_distance > config.PHASH_REVIEW_MAX_FRAME_DISTANCE:
        return None

    duration_diff = _duration_diff(r1["duration"], r2["duration"])
    likely_duration_ok = _duration_within(
        r1["duration"],
        r2["duration"],
        config.LIKELY_DURATION_TOLERANCE_ABS,
        config.LIKELY_DURATION_TOLERANCE_PCT,
    )
    is_likely = (
        comparison.mean_distance <= config.PHASH_LIKELY_SAME
        and comparison.max_distance <= config.PHASH_LIKELY_MAX_FRAME_DISTANCE
        and comparison.compared_frames >= config.MIN_FINGERPRINT_MATCH_FRAMES
        and likely_duration_ok
        and size_ratio <= config.LIKELY_SIZE_TOLERANCE_PCT
        and likely_resolution_ok
    )

    if is_likely:
        return PairEvidence(
            a=r1["id"],
            b=r2["id"],
            match_type="likely_same",
            reason=_format_near_reason(
                "Strong fingerprint match",
                comparison,
                duration_diff,
                size_ratio,
            ),
            confidence="High",
            manual_review=False,
            distance=comparison.mean_distance,
            compared_frames=comparison.compared_frames,
            max_distance=comparison.max_distance,
            size_ratio=size_ratio,
            duration_diff=duration_diff,
        )

    return PairEvidence(
        a=r1["id"],
        b=r2["id"],
        match_type="review",
        reason=_format_near_reason(
            "Borderline fingerprint match",
            comparison,
            duration_diff,
            size_ratio,
        ),
        confidence="Medium",
        manual_review=True,
        distance=comparison.mean_distance,
        compared_frames=comparison.compared_frames,
        max_distance=comparison.max_distance,
        size_ratio=size_ratio,
        duration_diff=duration_diff,
    )


def _format_near_reason(
    label: str,
    comparison: fingerprint.FingerprintComparison,
    duration_diff: float,
    size_ratio: float,
) -> str:
    return (
        f"{label}: mean pHash distance {comparison.mean_distance:.1f} "
        f"across {comparison.compared_frames} frames "
        f"(max {comparison.max_distance}); duration diff "
        f"{duration_diff:.1f}s; size diff {size_ratio:.1%}"
    )


def _combine_edge_reason(edges: list[PairEvidence], label: str) -> str:
    if len(edges) == 1:
        return edges[0].reason
    return (
        f"{label}: mean pHash distance "
        f"{min(e.distance for e in edges):.1f}-"
        f"{max(e.distance for e in edges):.1f}; at least "
        f"{min(e.compared_frames for e in edges)} matched frames; max frame "
        f"distance {max(e.max_distance for e in edges)}; max duration diff "
        f"{max(e.duration_diff for e in edges):.1f}s; max size diff "
        f"{max(e.size_ratio for e in edges):.1%}"
    )


def _edge_groups(
    prefix: str,
    edges: list[PairEvidence],
    by_id: dict[int, object],
    consumed: set[int],
) -> list[Group]:
    """Build disjoint all-pairs-supported groups from pair evidence.

    This avoids unsafe transitive clusters where A matches B and B matches C
    but A does not actually match C.
    """
    out: list[Group] = []
    edges = sorted(
        edges,
        key=lambda e: (
            e.distance,
            e.max_distance,
            e.size_ratio,
            e.duration_diff,
            e.a,
            e.b,
        ),
    )
    edge_by_pair = {edge.key: edge for edge in edges}
    candidate_ids = sorted({edge.a for edge in edges} | {edge.b for edge in edges})

    for edge in edges:
        if edge.a in consumed or edge.b in consumed:
            continue

        ids = [edge.a, edge.b]
        group_edges = [edge]

        while True:
            best = None
            for candidate_id in candidate_ids:
                if candidate_id in consumed or candidate_id in ids:
                    continue
                needed_edges: list[PairEvidence] = []
                for existing_id in ids:
                    pair = (min(candidate_id, existing_id),
                            max(candidate_id, existing_id))
                    candidate_edge = edge_by_pair.get(pair)
                    if candidate_edge is None:
                        needed_edges = []
                        break
                    needed_edges.append(candidate_edge)
                if not needed_edges:
                    continue
                score = (
                    max(e.distance for e in needed_edges),
                    max(e.max_distance for e in needed_edges),
                    max(e.size_ratio for e in needed_edges),
                    max(e.duration_diff for e in needed_edges),
                    candidate_id,
                )
                if best is None or score < best[0]:
                    best = (score, candidate_id, needed_edges)

            if best is None:
                break

            _, candidate_id, needed_edges = best
            ids.append(candidate_id)
            group_edges.extend(needed_edges)

        ids = sorted(ids)
        consumed.update(ids)
        match_type = edge.match_type
        label = (
            "Strong fingerprint match"
            if match_type == "likely_same"
            else "Borderline fingerprint match; manual review required"
        )
        out.append(
            Group(
                f"{prefix}:{'-'.join(str(i) for i in ids)}",
                match_type,
                [_mk_member(by_id[i]) for i in ids],
                reason=_combine_edge_reason(group_edges, label),
                confidence=edge.confidence,
                manual_review=edge.manual_review,
            )
        )

    return out


def _partial_hash_review_groups(rows, consumed: set[int]) -> list[Group]:
    """Return low-confidence groups for sampled-hash-only evidence."""
    out: list[Group] = []
    fast_map: dict[str, list] = {}
    for row in rows:
        if row["id"] in consumed or not row["fast_hash"] or not row["full_hash"]:
            continue
        fast_map.setdefault(row["fast_hash"], []).append(row)

    for fast_hash, members in fast_map.items():
        members = [m for m in members if m["id"] not in consumed]
        if len(members) < 2:
            continue
        if len({m["size"] for m in members}) != 1:
            continue
        if len({m["full_hash"] for m in members}) <= 1:
            continue

        ids = sorted(m["id"] for m in members)
        out.append(
            Group(
                f"partial:{fast_hash[:16]}",
                "review",
                [_mk_member(m) for m in sorted(members, key=lambda r: r["id"])],
                reason=(
                    "Same size and sampled partial hash, but full-file hashes "
                    "differ. This is not an exact duplicate."
                ),
                confidence="Low",
                manual_review=True,
            )
        )
        consumed.update(ids)

    return out


def _current_hardlink_stat(row) -> tuple[int, float, int, int] | None:
    """Return current stat evidence used for hardlink grouping."""
    current = hashing.stat_tuple(row["path"])
    if current is None:
        return None
    size, mtime, inode, device = current
    if inode <= 0:
        return None
    return size, mtime, inode, device


def _matches_cached_identity(row) -> bool:
    """Return True when cached identity still describes the current file."""
    current = hashing.stat_tuple(row["path"])
    if current is None:
        log.info("Skipping missing current-scan file: %s", row["path"])
        return False
    size, mtime, inode, device = current
    if (
        row["size"] != size
        or abs(row["mtime"] - mtime) >= 1e-6
        or row["inode"] != inode
        or row["device"] != device
    ):
        log.info("Skipping changed current-scan file: %s", row["path"])
        return False
    return True


def build_groups(conn, scan_id: int | None = None) -> list[Group]:
    rows = db.all_done_files(conn, scan_id=scan_id)
    if scan_id is not None:
        before = len(rows)
        rows = [row for row in rows if _matches_cached_identity(row)]
        skipped = before - len(rows)
        if skipped:
            log.info(
                "Skipped %d current-scan file(s) with stale cached identity",
                skipped,
            )
    by_id = {r["id"]: r for r in rows}
    groups: list[Group] = []
    consumed: set[int] = set()

    scope = f"scan {scan_id}" if scan_id is not None else "all cached files"
    log.info("Building groups from %d processed files in %s", len(rows), scope)

    # --- Hardlink groups (same inode+device) --------------------------------
    ino_map: dict[tuple[int, int], list[tuple[object, int, float]]] = {}
    for row in rows:
        current = _current_hardlink_stat(row)
        if current is None:
            continue
        size, mtime, inode, device = current
        ino_map.setdefault((device, inode), []).append((row, size, mtime))
    for (dev, ino), current_members in ino_map.items():
        members = [member[0] for member in current_members]
        if len(members) >= 2:
            sizes = {size for _row, size, _mtime in current_members}
            mtimes = {round(mtime, 6) for _row, _size, mtime in current_members}
            if len(sizes) != 1 or len(mtimes) != 1:
                log.warning(
                    "Skipping unreliable hardlink candidate %s:%s with "
                    "inconsistent current size/mtime",
                    dev,
                    ino,
                )
                continue
            groups.append(
                Group(
                    f"hl:{dev}:{ino}",
                    "hardlink",
                    [_mk_member(m) for m in members],
                    reason=(
                        "Current filesystem stats show the same device and "
                        "inode; these paths reference the same file and do "
                        "not represent duplicate disk usage."
                    ),
                    confidence="Same file",
                    manual_review=False,
                )
            )
            consumed.update(m["id"] for m in members)

    hardlink_groups = sum(1 for group in groups if group.match_type == "hardlink")
    if hardlink_groups:
        log.info("Found %d hardlink group(s)", hardlink_groups)

    # --- Exact hash groups ---------------------------------------------------
    hash_map: dict[str, list] = {}
    for row in rows:
        if row["id"] in consumed or not row["full_hash"]:
            continue
        hash_map.setdefault(row["full_hash"], []).append(row)

    exact_before = len(groups)
    for full_hash, members in hash_map.items():
        if len(members) >= 2:
            groups.append(
                Group(
                    f"hash:{full_hash[:16]}",
                    "exact",
                    [_mk_member(m) for m in members],
                    reason="Full-file SHA-256 hash match.",
                    confidence="High",
                    manual_review=False,
                )
            )
            consumed.update(m["id"] for m in members)
            log.debug(
                "Exact hash group: %d files with hash %s...",
                len(members),
                full_hash[:8],
            )

    exact_groups = len(groups) - exact_before
    if exact_groups:
        log.info("Found %d exact duplicate group(s)", exact_groups)

    # --- Perceptual near-duplicate evidence ---------------------------------
    candidates = [row for row in rows if row["id"] not in consumed and row["duration"]]
    fps = {row["id"]: db.frames_for(conn, row["id"]) for row in candidates}
    candidates = [row for row in candidates if fps[row["id"]]]
    log.info("Checking %d files for perceptual near-duplicates", len(candidates))

    pair_edges: list[PairEvidence] = []
    candidates.sort(key=lambda r: r["duration"])
    for i, row_i in enumerate(candidates):
        for row_j in candidates[i + 1:]:
            if not _duration_within(
                row_i["duration"],
                row_j["duration"],
                config.DURATION_TOLERANCE_ABS,
                config.DURATION_TOLERANCE_PCT,
            ):
                break

            evidence = _near_pair_evidence(
                row_i,
                row_j,
                fps[row_i["id"]],
                fps[row_j["id"]],
            )
            if evidence is not None:
                pair_edges.append(evidence)

    likely_edges = [edge for edge in pair_edges if edge.match_type == "likely_same"]
    likely_groups = _edge_groups("near", likely_edges, by_id, consumed)
    groups.extend(likely_groups)
    if likely_groups:
        log.info("Found %d high-confidence video match group(s)", len(likely_groups))

    review_edges = [
        edge for edge in pair_edges
        if edge.match_type == "review"
        and edge.a not in consumed
        and edge.b not in consumed
    ]
    review_groups = _edge_groups("review", review_edges, by_id, consumed)
    groups.extend(review_groups)
    if review_groups:
        log.info("Found %d fingerprint manual-review group(s)", len(review_groups))

    # --- Partial hash review hints ------------------------------------------
    partial_groups = _partial_hash_review_groups(rows, consumed)
    groups.extend(partial_groups)
    if partial_groups:
        log.info("Found %d partial-hash manual-review group(s)", len(partial_groups))

    # Biggest space win first, but keep hardlinks/review clearly labeled.
    groups.sort(key=lambda group: group.reclaimable, reverse=True)
    summary = summarize(groups)
    log.info(
        "Returning %d reviewable group(s): %d confirmed duplicate, "
        "%d manual-review, %d hardlink",
        summary.total,
        summary.confirmed,
        summary.possible,
        summary.hardlink,
    )
    return groups
