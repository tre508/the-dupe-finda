# The File Hashing Slasher (video-dedupe)

A personal Linux desktop app for safely deduplicating a large video library. Scans folders, finds exact and near-duplicate videos, shows thumbnails + metadata in a reviewable tree, and moves selected copies to the system Trash after an explicit confirmation — never emptying a group by accident.

![App screenshot concept: QTreeWidget with thumbnail previews, duplicate badges (Exact / High-Confidence / Manual Review / Hardlink), file size/duration/resolution columns, and a trash confirmation dialog](https://img.shields.io/badge/status-active-brightgreen)

---

## Quick Start

```bash
# 1. Setup (installs ffmpeg + Python virtualenv + desktop shortcut)
./setup.sh

# 2. Launch
./run.sh
```

Then in the app: **Add folder…** → choose scan options → **Scan** → review groups → tick copies to remove → **Move selected to Trash**.

---

## What It Does

### Scanning Pipeline (3 tiers, never freezes the UI)

| Tier | Work | Cost |
|---|---|---|
| **0 — Index** | `os.walk` with hidden/symlink filters; SQLite `files` table (`path`, `size`, `mtime`, `inode`, `device`) | Very cheap (`stat` only) |
| **1a — Metadata** | `ffprobe` for duration, codec, container, resolution | Cheap |
| **1b — Exact hash** | `blake2b` partial (head/mid/tail + size) and full `sha256` streaming hash; only runs when `count_same_size > 1` | Expensive, deferred until duplicates are possible |
| **2 — Near-duplicate** | Whole-duration `imagehash.pHash` (12 frames evenly sampled); strict duration/size pre-filters; mean Hamming distance thresholds (`PHASH_LIKELY_SAME = 1`, `PHASH_REVIEW = 3`) | Expensive, opt-in (`--near-duplicates`) |

- **Exact duplicates** confirmed by byte-identical full `sha256` hashes.
- **Near-duplicates** (re-encodes, different quality/bitrate) use perceptual fingerprinting, not filename similarity.
- **Manual review** matches hidden by default; enable **Show possible matches** to audit weaker hints.
- **Hardlinks** (`same inode/device`, zero reclaimable space) shown with a distinct badge; never trigger unnecessary deletion.

### Safe Deletion (never-empty-a-group guard)

- **Trash by default** (`send2trash`). Permanent delete only when explicitly requested (`Permanently delete` checkbox).
- **Independent validation layer** (`vdf/deletion.py`): `validate()` raises `GuardError` if a group would be fully emptied; enforced independently of the UI.
- **Literal pre-delete manifest**: every removal listed by group, file name, action (`Trash` / `PERMANENTLY DELETE`), and reclaimed bytes.
- **Append-only deletion log** (`~/.local/state/duplicate-video-finder/deletions.log`) plus SQLite `deletions` table for audit/recovery.

### Performance & Caching

- **SQLite WAL mode** (`vdf/db.py`) so the GUI thread reads while worker threads write; one connection per thread.
- **Cache key**: `(path, size, mtime, inode, device)`. Unchanged files skip expensive hashing on rescan. Inode/device changes invalidate identity even when size/mtime match.
- **Background scanning** with `QThread` workers (`ScanWorker`, `GroupWorker`, `ThumbWorker`). UI never freezes; progress bar updates in real time.
- **Cancel/re-run resume**: interrupted scans recover cleanly (`db.recover_interrupted_scans`); cancelled files reset to `ST_INDEXED` so they can be reprocessed.

---

## Data Locations (XDG Base Directory)

| What | Path |
|---|---|
| Database | `~/.local/state/duplicate-video-finder/library.sqlite3` |
| Deletion log | `~/.local/state/duplicate-video-finder/deletions.log` |
| Thumbnail cache | `~/.cache/duplicate-video-finder/thumbs/` |
| App config/state | `~/.config/duplicate-video-finder/` |

Re-running a scan reuses everything cached for unchanged files (`path + size + mtime + inode + device`).

---

## Requirements

- **Linux** (tested on Ubuntu / Zorin OS / Debian-based)
- **Python 3.12+**
- `ffmpeg` + `ffprobe` (system package `ffmpeg`)
- Python packages (installed by `setup.sh` into `.venv`):
  - `PySide6>=6.6` (Qt 6 GUI)
  - `Pillow>=10.0` (thumbnails)
  - `ImageHash>=4.3` (perceptual pHash)
  - `send2trash>=1.8` (safe Trash)

---

## Installation

```bash
git clone <repo-url>
cd video-dedupe
./setup.sh        # sudo apt install ffmpeg + python3-venv; creates .venv
```

The `setup.sh` script also installs a desktop shortcut (`duplicate-video-finder.desktop`) and icon (`~/.local/share/icons/`) so the app appears in the application menu.

---

## Usage

1. Launch: `./run.sh` (or use the desktop shortcut)
2. **Add folder…** — select one or more video directories
3. Toggle **Include subfolders**, **Ignore hidden folders**, extension filter, minimum size
4. Toggle **Find likely same videos (slower)** for near-duplicate fingerprinting
5. Press **Scan**
6. Review groups in the tree widget:
   - Confirmed duplicates (`Exact Duplicate`, `High-Confidence Video Match`) have strong evidence.
   - `Possible Match / Manual Review` groups require inspection; hidden by default.
7. Tick files to remove (largest kept by default; smart-select buttons help)
8. Press **Move selected to Trash** (or **Permanently DELETE selected** after checking the checkbox)

---

## Architecture Overview

```
User (QMainWindow / QTreeWidget UI)
    ↓  (click → select / delete / scan / cancel)
Controllers (main_window.py: MainWindow)
    ↓  (Scan / Group / Thumb / Deletion)
Workers (ui/workers.py: ScanWorker / GroupWorker / ThumbWorker)
    ↓  (QThread, cooperative cancellation)
Scanner / Grouping / Deletion (vdf/scanner.py, grouping.py, deletion.py)
    ↓  (SQLite persistence)
Persistence (vdf/db.py: SQLite with WAL, XDG paths)
    ↓  (cached hashes / metadata / fingerprints / thumbnails)
External Tools (ffprobe / ffmpeg / PIL / imagehash)
```

- **UI layer**: `PySide6.QtWidgets` (`QTreeWidget`, `QProgressBar`, `QFileDialog`, `QMessageBox`). A pragmatic `QTreeWidget` (not a custom `QAbstractItemModel`) for manual-review simplicity.
- **Worker layer**: Background threads with `threading.Event` cancellation and progress/log callbacks.
- **Scanner layer**: Tiered pipeline (`index → metadata → exact hash → perceptual fingerprint`). Per-file errors are isolated; the batch continues.
- **Persistence layer**: SQLite (`library.sqlite3`) with WAL mode; `deletions.log` append-only audit; thumbnail cache (`thumbs/`).
- **Deletion layer**: Independent `validate()` guard (`deletion.py`) ensures no group is fully emptied; `build_manifest()` produces a literal pre-delete list; `execute()` uses `send2trash` by default.

---

## Key Technical Choices

- **Shell out to `ffmpeg`/`ffprobe`** instead of Python bindings (`PyAV`). Corrupt or exotic files crash a short-lived child process, never the app.
- **Per-file error isolation** (`try/except` around `_process_one`). Any failure is logged to SQLite (`scan_errors` table) and the log; the scan continues.
- **Partial + full hash strategy** (`hashing.py`): `blake2b` partial (head + mid + tail + size) for quick diagnostics; full `sha256` streaming only when `count_same_size > 1` so expensive work is deferred until duplicates are actually possible.
- **Perceptual fingerprinting** (`fingerprint.py`): 12 evenly-spread frames across the **entire duration** (not just first seconds), `imagehash.pHash` (64-bit), mean Hamming distance with strict duration/size tolerances (`config.py`). Filename/folder similarity is **not** duplicate evidence.
- **Safe deletion**: Trash by default, `GuardError` prevents emptying a group, manifest confirms exactly what will be removed.

---

## Running Tests

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m unittest discover -s tests
# or
pytest tests/
```

---

## Project Structure

```
video-dedupe/
├── vdf/                  # Main Python package
│   ├── scanner.py         # Tiered scan pipeline (index, hash, fingerprint)
│   ├── grouping.py        # Duplicate group creation + summary
│   ├── deletion.py        # Safe deletion (guard + manifest + execute)
│   ├── hashing.py         # Partial/full file hashing
│   ├── fingerprint.py     # Perceptual pHash near-duplicate detection
│   ├── thumbnails.py      # Thumbnail generation / cache
│   ├── ffmpeg.py          # ffprobe / ffmpeg wrappers
│   ├── db.py              # SQLite persistence (WAL, XDG)
│   ├── config.py          # XDG paths + constants
│   └── ui/
│       ├── main_window.py # MainWindow (QTreeWidget UI)
│       └── workers.py     # Background QThread workers
├── resources/             # App icon (SVG), desktop file
├── tests/                 # Unit tests
├── setup.sh              # Install ffmpeg + .venv + desktop shortcut
├── run.sh                # Launch app
└── README.md             # This file
```

---

## License & Author

Personal project by the repository owner. See `.git` history for full provenance.
