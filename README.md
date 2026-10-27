# The File Hashing Slasher

A personal Linux desktop app (Zorin OS / Ubuntu-based) for safely
deduplicating a large video library. Scans folders, finds exact and
near-duplicate videos, shows thumbnails + metadata, and moves selected
copies to the Trash after an explicit confirmation.

## Quick start

```bash
cd video-dedupe
./setup.sh        # installs ffmpeg + Python deps into .venv
./run.sh          # launches the GUI
```

Then in the app: **Add folder…** → choose scan options → **Scan** →
review groups → tick the copies to remove → **Move selected to Trash**.

## What it does

- Multi-folder scan, subfolder toggle, extension filter, hidden-folder
  filtering, and minimum-size filtering
- Exact duplicates: size grouping -> partial hash -> full hash confirmation
- Optional near-duplicates: strict duration/size checks + whole-duration
  frame pHash. Filename/folder similarity is not duplicate evidence.
- Match badges separate confirmed duplicates from possible manual-review
  candidates, with reason, confidence, review requirement, and hash status.
- Possible/manual-review matches are hidden by default; enable **Show possible
  matches** when you want to audit weaker hints.
- Cached thumbnails, background scanning (UI never freezes), cancel/re-run
  resume
- Safe deletion: Trash by default, never-empty-a-group guard, literal
  pre-delete manifest, SQLite + text deletion log

## Data locations (XDG)

| What | Path |
|---|---|
| Database | `~/.local/state/duplicate-video-finder/library.sqlite3` |
| Deletion log | `~/.local/state/duplicate-video-finder/deletions.log` |
| Thumbnails | `~/.cache/duplicate-video-finder/thumbs/` |

Re-running a scan reuses everything cached for unchanged files
(`path + size + mtime`). Unique-size files skip full hashing until another
same-size file appears, so large libraries spend expensive work where exact
duplicates are actually possible.

See `IMPLEMENTATION_GUIDE.md` for the full build/architecture writeup.
