"""Safe deletion subsystem.

Rules enforced here regardless of what the UI allows:
  * Trash by default (send2trash). Permanent delete only when explicitly
    requested by the caller.
  * NEVER allow a group to be fully emptied. Validated independently of
    the UI so an edge case cannot bypass it.
  * Every removal is logged to SQLite and an append-only text log.
  * Per-file failures are isolated and reported; the batch continues.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from send2trash import send2trash

from . import config, db
from .grouping import BADGE

if TYPE_CHECKING:
    from .grouping import Group

DeletionItem = tuple[int, str, int]


@dataclass
class DeletionPlan:
    """paths_by_group: group_key -> list of (file_id, path, size) to remove."""
    paths_by_group: dict[str, list[DeletionItem]]
    permanent: bool = False

    @property
    def all_paths(self) -> list[DeletionItem]:
        out: list[DeletionItem] = []
        for items in self.paths_by_group.values():
            out.extend(items)
        return out

    @property
    def total_bytes(self) -> int:
        return sum(s for _, _, s in self.all_paths)


class GuardError(Exception):
    pass


def validate(plan: DeletionPlan, groups: dict[str, "Group"]) -> None:
    """Raise GuardError if the plan is empty or removes every copy."""
    if not plan.all_paths:
        raise GuardError("Nothing selected for deletion.")
    for group_key, items in plan.paths_by_group.items():
        group = groups.get(group_key)
        if group and len(items) >= len(group.members):
            raise GuardError(
                "Refusing to remove every file in a group. Leave at least "
                "one copy unchecked."
            )


def build_manifest(plan: DeletionPlan, groups: dict[str, "Group"]) -> str:
    """Human-readable, literal list of exactly what will happen."""
    lines = []
    action = "PERMANENTLY DELETE" if plan.permanent else "move to Trash"
    lines.append(f"This will {action} {len(plan.all_paths)} file(s), "
                 f"freeing {_human(plan.total_bytes)}.\n")
    for gkey, items in plan.paths_by_group.items():
        grp = groups.get(gkey)
        if not grp:
            continue
        selected_paths = {p for _, p, _ in items}
        kept = [m for m in grp.members
                if m.path not in selected_paths]
        review = "yes" if grp.manual_review else "no"
        lines.append(
            f"── {BADGE.get(grp.match_type, grp.match_type)} "
            f"({grp.confidence} confidence, manual review: {review}) ──"
        )
        if grp.reason:
            lines.append(f"   reason  {grp.reason}")
        for _, p, s in items:
            lines.append(f"   ✗ remove  {p}  ({_human(s)})")
        for m in kept:
            lines.append(f"   ✓ keep    {m.path}")
        lines.append("")
    return "\n".join(lines)


def execute(plan: DeletionPlan,
            groups: dict[str, "Group"]) -> tuple[int, list[str]]:
    """Perform the deletion. Returns (success_count, error_messages).

    Re-validates the guard immediately before touching the disk.
    """
    validate(plan, groups)
    conn = db.connect()
    ok = 0
    errors: list[str] = []
    config.ensure_dirs()
    try:
        with open(config.LOG_PATH, "a", encoding="utf-8") as logf:
            for gkey, items in plan.paths_by_group.items():
                for _fid, path, size in items:
                    try:
                        if not Path(path).exists():
                            raise FileNotFoundError(path)
                        if plan.permanent:
                            os.remove(path)
                            action = "permanent"
                        else:
                            send2trash(path)
                            action = "trash"
                        db.log_deletion(conn, path, size, action, gkey)
                        db.remove_file_row(conn, path)
                        logf.write(
                            f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t"
                            f"{action}\t{size}\t{path}\n"
                        )
                        ok += 1
                    except Exception as exc:  # noqa: BLE001 isolate per file
                        errors.append(f"{path}: {exc}")
            conn.commit()
    finally:
        conn.close()
    return ok, errors


def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"
