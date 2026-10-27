"""Main application window.

Pragmatic deviation from the design plan: the plan specified a custom
QAbstractItemModel. For a personal manual-review tool a QTreeWidget is
dramatically less code, is still virtualized enough for realistic group
counts, and makes the never-empty-a-group guard trivial to enforce on
itemChanged. The model can be upgraded later without touching the
scanner/grouping/deletion layers.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from PySide6.QtCore import Qt, QThread, QSettings, QSize
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QCheckBox, QFileDialog, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QProgressBar, QPlainTextEdit, QPushButton,
    QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .. import config, db, ffmpeg, grouping
from ..deletion import DeletionPlan, GuardError, build_manifest, execute, validate
from ..scanner import Source
from .workers import GroupWorker, ScanWorker, ThumbWorker

if TYPE_CHECKING:
    from ..grouping import Group

(
    COL_PREVIEW, COL_ACTION, COL_NAME, COL_PATH, COL_SIZE, COL_DUR, COL_RES,
    COL_MOD, COL_TYPE, COL_REASON, COL_CONF, COL_REVIEW, COL_HASH,
) = range(13)
SOURCE_RECURSIVE_ROLE = int(Qt.ItemDataRole.UserRole)
ROLE_FILEID = SOURCE_RECURSIVE_ROLE + 1
ROLE_PATH = SOURCE_RECURSIVE_ROLE + 2
ROLE_SIZE = SOURCE_RECURSIVE_ROLE + 3
ROLE_GROUPKEY = SOURCE_RECURSIVE_ROLE + 4


def human(n: int) -> str:
    f = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or u == "TB":
            return f"{f:.1f} {u}"
        f /= 1024
    return f"{f:.1f} TB"


def fmt_dur(d) -> str:
    if not d:
        return "—"
    d = int(d)
    return f"{d // 3600:d}:{(d % 3600) // 60:02d}:{d % 60:02d}"


def fmt_mtime(ts) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (OSError, TypeError, ValueError):
        return "—"


def parse_size_mb(text: str) -> int:
    try:
        mb = float(text.strip() or "0")
    except ValueError:
        return 0
    return max(0, int(mb * 1024 * 1024))


def hash_status(member, group: "Group") -> str:
    if member.full_hash:
        return "Full"
    if member.fast_hash:
        return "Partial"
    if group.match_type == "exact":
        return "Missing"
    return "Not needed"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("The File Hashing Slasher")
        icon_path = config.best_icon_path()
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path.resolve())))
        self.resize(1180, 760)
        self.settings = QSettings(config.APP_ID, config.APP_ID)

        self._scan_thread: QThread | None = None
        self._scan_worker: ScanWorker | None = None
        self._thumb_thread: QThread | None = None
        self._thumb_worker: ThumbWorker | None = None
        self._g_thread: QThread | None = None
        self._g_worker: GroupWorker | None = None
        self._groups: dict[str, "Group"] = {}
        self._all_groups: list["Group"] = []
        self._active_scan_id: int | None = None
        self._guard_busy = False

        usable_db = db.verify_or_recreate()
        db.init_db()
        conn = db.connect()
        try:
            interrupted_roots = db.recover_interrupted_scans(conn)
            conn.commit()
        finally:
            conn.close()
        self._build_ui()
        self._load_sources()
        self._check_tools()
        if not usable_db:
            QMessageBox.warning(
                self,
                "Database recovered",
                "The saved database failed SQLite integrity checks. A backup "
                "was written under the app state backups folder and a clean "
                "database was created.",
            )
        if interrupted_roots:
            msg = QMessageBox(self)
            msg.setWindowTitle("Previous scan did not finish")
            msg.setIcon(QMessageBox.Icon.Warning)
            msg.setText(
                f"{len(interrupted_roots)} interrupted scan source(s) were "
                "found and marked as crashed."
            )
            msg.setInformativeText(
                "The app will not automatically resume them. You can load the "
                "folders back into the source list, then press Scan when ready."
            )
            load_btn = msg.addButton(
                "Load Folders",
                QMessageBox.ButtonRole.AcceptRole,
            )
            msg.addButton("Clear State", QMessageBox.ButtonRole.DestructiveRole)
            msg.addButton(QMessageBox.StandardButton.Cancel)
            msg.exec()
            if msg.clickedButton() == load_btn:
                self._load_interrupted_sources(interrupted_roots)

    def _load_interrupted_sources(self, roots: list[str]) -> None:
        existing = {self.src_list.item(i).text() for i in range(self.src_list.count())}
        for root in roots:
            if root in existing:
                continue
            item = QListWidgetItem(root)
            item.setData(SOURCE_RECURSIVE_ROLE, self.recursive_cb.isChecked())
            self.src_list.addItem(item)
        self._save_sources()
        self._log("Loaded interrupted scan folders. Press Scan to resume safely.")

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # --- Sources row ---
        src_box = QHBoxLayout()
        self.src_list = QListWidget()
        self.src_list.setMaximumHeight(110)
        src_box.addWidget(self.src_list, 3)

        src_btns = QVBoxLayout()
        add_btn = QPushButton("Add folder…")
        add_btn.clicked.connect(self._add_folder)
        rm_btn = QPushButton("Remove selected")
        rm_btn.clicked.connect(self._remove_folder)
        self.recursive_cb = QCheckBox("Include subfolders")
        self.recursive_cb.setChecked(True)
        self.hidden_cb = QCheckBox("Ignore hidden folders")
        self.hidden_cb.setChecked(config.DEFAULT_IGNORE_HIDDEN)
        src_btns.addWidget(add_btn)
        src_btns.addWidget(rm_btn)
        src_btns.addWidget(self.recursive_cb)
        src_btns.addWidget(self.hidden_cb)
        src_btns.addStretch()
        src_box.addLayout(src_btns, 1)
        root.addLayout(src_box)

        ext_row = QHBoxLayout()
        ext_row.addWidget(QLabel("Extensions:"))
        self.ext_edit = QLineEdit(" ".join(config.DEFAULT_EXTENSIONS))
        ext_row.addWidget(self.ext_edit)
        ext_row.addWidget(QLabel("Ignore smaller than MB:"))
        self.min_size_edit = QLineEdit(str(config.DEFAULT_MIN_SIZE_MB))
        self.min_size_edit.setMaximumWidth(80)
        ext_row.addWidget(self.min_size_edit)
        self.near_cb = QCheckBox("Find likely same videos (slower)")
        self.near_cb.setChecked(config.DEFAULT_NEAR_DUPLICATES)
        ext_row.addWidget(self.near_cb)
        self.review_cb = QCheckBox("Show possible matches")
        self.review_cb.setChecked(False)
        self.review_cb.setToolTip(
            "Show low/medium-confidence manual-review candidates."
        )
        self.review_cb.toggled.connect(self._refresh_visible_groups)
        ext_row.addWidget(self.review_cb)
        root.addLayout(ext_row)

        # --- Action row ---
        act = QHBoxLayout()
        self.scan_btn = QPushButton("Scan")
        self.scan_btn.clicked.connect(self._start_scan)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_scan)
        act.addWidget(self.scan_btn)
        act.addWidget(self.cancel_btn)
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        act.addWidget(self.progress, 1)
        root.addLayout(act)

        # --- Splitter: results tree / log ---
        split = QSplitter(Qt.Orientation.Vertical)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(13)
        self.tree.setHeaderLabels(
            ["Preview", "Action", "Name", "Path", "Size", "Duration",
             "Resolution", "Modified", "Match type", "Reason", "Confidence",
             "Review", "Hash status"]
        )
        self.tree.setIconSize(QSize(140, 79))
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(False)
        self.tree.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.tree.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.tree.itemChanged.connect(self._on_item_changed)
        hdr = self.tree.header()
        hdr.setStretchLastSection(False)
        for col in range(self.tree.columnCount()):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(COL_PREVIEW, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(COL_ACTION, QHeaderView.ResizeMode.Fixed)
        self.tree.setColumnWidth(COL_PREVIEW, 154)
        self.tree.setColumnWidth(COL_ACTION, 104)
        self.tree.setColumnWidth(COL_NAME, 380)
        self.tree.setColumnWidth(COL_PATH, 560)
        self.tree.setColumnWidth(COL_SIZE, 96)
        self.tree.setColumnWidth(COL_DUR, 96)
        self.tree.setColumnWidth(COL_RES, 110)
        self.tree.setColumnWidth(COL_MOD, 150)
        self.tree.setColumnWidth(COL_TYPE, 210)
        self.tree.setColumnWidth(COL_REASON, 520)
        self.tree.setColumnWidth(COL_CONF, 110)
        self.tree.setColumnWidth(COL_REVIEW, 84)
        self.tree.setColumnWidth(COL_HASH, 110)

        # Increase font sizes for readability
        self.setStyleSheet("""
            QTreeWidget { font-size: 11pt; }
            QTreeWidget::item { padding: 4px; }
            QHeaderView::section { font-size: 11pt; font-weight: bold; }
            QPushButton { font-size: 11pt; padding: 6px; }
            QLabel { font-size: 11pt; }
            QCheckBox { font-size: 11pt; }
            QLineEdit { font-size: 11pt; }
        """)
        split.addWidget(self.tree)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(150)
        split.addWidget(self.log_view)
        split.setStretchFactor(0, 1)
        root.addWidget(split, 1)

        # --- Selection / delete row ---
        bottom = QHBoxLayout()
        keep_large = QPushButton("Select all but largest")
        keep_large.clicked.connect(lambda: self._smart_select("size"))
        keep_res = QPushButton("Select all but highest-res")
        keep_res.clicked.connect(lambda: self._smart_select("res"))
        clear_btn = QPushButton("Clear selection")
        clear_btn.clicked.connect(self._clear_selection)
        bottom.addWidget(keep_large)
        bottom.addWidget(keep_res)
        bottom.addWidget(clear_btn)
        bottom.addStretch()

        self.perm_cb = QCheckBox("Permanently delete (skip Trash)")
        self.perm_cb.setVisible(False)
        self.perm_cb.setEnabled(False)
        self.perm_cb.toggled.connect(self._update_summary)
        bottom.addWidget(self.perm_cb)
        self.summary_lbl = QLabel("Nothing selected")
        bottom.addWidget(self.summary_lbl)
        self.del_btn = QPushButton("Move selected to Trash")
        self.del_btn.clicked.connect(self._delete_selected)
        bottom.addWidget(self.del_btn)
        root.addLayout(bottom)

    # ------------------------------------------------------------ helpers
    def _log(self, msg: str):
        self.log_view.appendPlainText(msg)

    def _check_tools(self):
        missing = ffmpeg.check_tools()
        if missing:
            QMessageBox.warning(
                self, "Missing dependencies",
                "These required tools were not found on PATH:\n\n"
                f"  {', '.join(missing)}\n\n"
                "Install with:\n  sudo apt install ffmpeg",
            )

    # ------------------------------------------------------------ sources
    def _add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Add folder to scan")
        if d:
            item = QListWidgetItem(d)
            item.setData(SOURCE_RECURSIVE_ROLE, self.recursive_cb.isChecked())
            self.src_list.addItem(item)
            self._save_sources()

    def _remove_folder(self):
        for it in self.src_list.selectedItems():
            self.src_list.takeItem(self.src_list.row(it))
        self._save_sources()

    def _save_sources(self):
        recursive = self.recursive_cb.isChecked()
        data = []
        for i in range(self.src_list.count()):
            item = self.src_list.item(i)
            item.setData(SOURCE_RECURSIVE_ROLE, recursive)
            data.append({"root": item.text(), "recursive": recursive})
        self.settings.setValue("sources", json.dumps(data))
        self.settings.setValue("recursive", recursive)
        self.settings.setValue("extensions", self.ext_edit.text())
        self.settings.setValue("min_size_mb", self.min_size_edit.text())
        self.settings.setValue("ignore_hidden", self.hidden_cb.isChecked())
        self.settings.setValue("near_duplicates", self.near_cb.isChecked())
        self.settings.setValue("show_possible_matches", self.review_cb.isChecked())

    def _load_sources(self):
        recursive = str(self.settings.value(
            "recursive", self.recursive_cb.isChecked()
        )).lower() not in ("false", "0")
        self.recursive_cb.setChecked(recursive)
        raw = self.settings.value("sources", "")
        if raw:
            try:
                for entry in json.loads(raw):
                    it = QListWidgetItem(entry["root"])
                    it.setData(SOURCE_RECURSIVE_ROLE,
                               entry.get("recursive", recursive))
                    self.src_list.addItem(it)
            except (ValueError, TypeError):
                pass
        ext = self.settings.value("extensions", "")
        if ext:
            self.ext_edit.setText(ext)
        self.min_size_edit.setText(str(self.settings.value(
            "min_size_mb", str(config.DEFAULT_MIN_SIZE_MB)
        )))
        self.hidden_cb.setChecked(str(self.settings.value(
            "ignore_hidden", config.DEFAULT_IGNORE_HIDDEN
        )).lower() not in ("false", "0"))
        self.near_cb.setChecked(str(self.settings.value(
            "near_duplicates", config.DEFAULT_NEAR_DUPLICATES
        )).lower() in ("true", "1"))
        self.review_cb.setChecked(str(self.settings.value(
            "show_possible_matches", False
        )).lower() in ("true", "1"))

    def _sources(self) -> list[Source]:
        # Split by space or comma, and handle case-insensitivity at input.
        raw_exts = self.ext_edit.text().replace(",", " ").lower().split()
        exts = [e if e.startswith(".") else f".{e}" for e in raw_exts if e]
        min_size = parse_size_mb(self.min_size_edit.text())
        ignore_hidden = self.hidden_cb.isChecked()
        near = self.near_cb.isChecked()
        # Use global recursive checkbox state to avoid confusing "state at time of add" behavior
        is_recursive = self.recursive_cb.isChecked()
        out = []
        for i in range(self.src_list.count()):
            it = self.src_list.item(i)
            out.append(Source(it.text(),
                              is_recursive,
                              exts or list(config.DEFAULT_EXTENSIONS),
                              min_size,
                              ignore_hidden,
                              near))
        return out

    # ------------------------------------------------------------- scan
    def _clear_active_results(self):
        self.tree.clear()
        self._groups.clear()
        self._all_groups.clear()
        self._active_scan_id = None
        self._guard_busy = False
        self._update_summary()

    def _start_scan(self):
        sources = self._sources()
        if not sources:
            QMessageBox.information(self, "No folders",
                                    "Add at least one folder to scan.")
            return
        self._save_sources()
        if self._thumb_worker:
            self._thumb_worker.cancel()
        self._clear_active_results()
        roots = ", ".join(str(Path(src.root).expanduser()) for src in sources)
        self._log(f"Starting scan for: {roots}")
        self.scan_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.del_btn.setEnabled(False)
        self.progress.setRange(0, 0)

        self._scan_thread = QThread()
        self._scan_worker = ScanWorker(sources)
        self._scan_worker.moveToThread(self._scan_thread)
        self._scan_thread.started.connect(self._scan_worker.run)
        self._scan_worker.progress.connect(self._on_progress)
        self._scan_worker.log.connect(self._log)
        self._scan_worker.finished.connect(self._on_scan_done)
        self._scan_thread.start()

    def _cancel_scan(self):
        if self._scan_worker:
            self._scan_worker.cancel()
            self._log("Cancel requested — finishing current file…")
            self.cancel_btn.setEnabled(False)

    def _on_progress(self, stage: str, done: int, total: int):
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
            self.progress.setFormat(f"{stage}: %v/%m")
        else:
            self.progress.setRange(0, 0)
            self.progress.setFormat(f"{stage}: {done}")

    def _on_scan_done(self, status: str, scan_id: int | None):
        self._log(status)
        if self._scan_thread:
            self._scan_thread.quit()
            self._scan_thread.wait()
        self._scan_thread = None
        self._scan_worker = None
        self.cancel_btn.setEnabled(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        if not scan_id or not status.startswith("Scan complete:"):
            self.scan_btn.setEnabled(True)
            self.progress.setFormat("Done")
            self._log("No active results loaded because the scan did not complete.")
            return
        self._active_scan_id = scan_id
        self.progress.setFormat("Building groups…")
        self._build_groups(scan_id)

    # ----------------------------------------------------------- grouping
    def _build_groups(self, scan_id: int | None = None):
        self._g_thread = QThread()
        self._g_worker = GroupWorker(scan_id)
        self._g_worker.moveToThread(self._g_thread)
        self._g_thread.started.connect(self._g_worker.run)
        self._g_worker.log.connect(self._log)
        self._g_worker.finished.connect(self._populate)
        self._g_thread.start()

    def _populate(self, groups):
        if self._g_thread:
            self._g_thread.quit()
            self._g_thread.wait()
            self._g_thread = None
            self._g_worker = None
        self._all_groups = list(groups)
        self._render_groups(self._all_groups, log_summary=True)
        self.scan_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)

    def _refresh_visible_groups(self):
        self._save_sources()
        if self._all_groups:
            self._render_groups(self._all_groups, log_summary=False)

    def _visible_groups(self, groups):
        if self.review_cb.isChecked():
            return list(groups)
        return [g for g in groups if not g.manual_review]

    def _render_groups(self, groups, log_summary: bool):
        visible_groups = self._visible_groups(groups)
        self.tree.clear()
        self._groups = {g.key: g for g in visible_groups}
        self._guard_busy = True
        thumb_items: list[tuple[int, str, float]] = []
        summary = grouping.summarize(groups)
        if log_summary:
            self._log(
                f"Results: {summary.confirmed} confirmed duplicate group(s), "
                f"{summary.possible} possible manual-review group(s), "
                f"{summary.hardlink} hardlink group(s)"
            )
            if summary.total == 0:
                self._log("No duplicates found in the current scan.")
        hidden = summary.possible if not self.review_cb.isChecked() else 0
        if hidden:
            self._log(
                f"Hiding {hidden} possible manual-review group(s). "
                "Enable 'Show possible matches' to inspect them."
            )

        for g in visible_groups:
            top = QTreeWidgetItem(self.tree)
            top.setFirstColumnSpanned(True)
            if g.match_type == "hardlink":
                review_text = "same file"
            else:
                review_text = (
                    "manual review required" if g.manual_review else "confirmed"
                )
            confidence_text = (
                g.confidence if g.match_type == "hardlink"
                else f"{g.confidence} confidence"
            )
            header = (
                f"{grouping.BADGE.get(g.match_type, g.match_type)}  ·  "
                f"{confidence_text}  ·  {review_text}  ·  "
                f"{len(g.members)} files  ·  "
                f"reclaim {human(g.reclaimable)} of {human(g.total_size)}  ·  "
                f"{g.reason}"
            )
            top.setText(COL_PREVIEW, header)
            top.setToolTip(COL_PREVIEW, header)
            top.setData(COL_PREVIEW, ROLE_GROUPKEY, g.key)
            top.setFlags(top.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            top.setExpanded(True)
            # suggest keeping the largest member
            keeper_id = max(g.members, key=lambda m: m.size).file_id
            for m in g.members:
                child = QTreeWidgetItem(top)
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(COL_NAME, Qt.CheckState.Unchecked)
                
                btn = QPushButton("Select")
                btn.setCheckable(True)
                btn.setMinimumWidth(78)
                btn.setMaximumWidth(92)
                btn.setFixedHeight(30)
                if m.file_id == keeper_id:
                    btn.setToolTip(
                        "Largest file in this group; usually keep this one."
                    )
                else:
                    btn.setToolTip("Select this file for removal.")
                # Keep button state in sync when clicked directly
                btn.clicked.connect(
                    lambda checked, c=child: self._set_child_selected(c, checked)
                )
                btn_box = QWidget()
                btn_layout = QHBoxLayout(btn_box)
                btn_layout.setContentsMargins(4, 0, 4, 0)
                btn_layout.addWidget(btn, 0, Qt.AlignmentFlag.AlignCenter)
                self.tree.setItemWidget(child, COL_ACTION, btn_box)
                
                name = Path(m.path).name
                child.setText(COL_NAME, name)
                child.setToolTip(COL_NAME, f"{name}\n{m.path}")
                child.setText(COL_PATH, m.path)
                child.setToolTip(COL_PATH, m.path)
                child.setText(COL_SIZE, human(m.size))
                child.setText(COL_DUR, fmt_dur(m.duration))
                res = (f"{m.width}×{m.height}"
                       if m.width and m.height else "—")
                child.setText(COL_RES, res)
                child.setText(COL_MOD, fmt_mtime(m.mtime))
                child.setText(COL_TYPE,
                              grouping.BADGE.get(g.match_type, g.match_type))
                child.setText(COL_REASON, g.reason)
                child.setToolTip(COL_REASON, g.reason)
                child.setText(COL_CONF, g.confidence)
                child.setText(COL_REVIEW, "Yes" if g.manual_review else "No")
                child.setText(COL_HASH, hash_status(m, g))
                child.setData(COL_NAME, ROLE_FILEID, m.file_id)
                child.setData(COL_NAME, ROLE_PATH, m.path)
                child.setData(COL_NAME, ROLE_SIZE, m.size)
                child.setData(COL_NAME, ROLE_GROUPKEY, g.key)
                thumb_items.append((m.file_id, m.path, m.duration or 0.0))

        self._guard_busy = False
        self.progress.setFormat("Done")
        self._update_summary()
        if thumb_items:
            self._start_thumbs(thumb_items)

    # ----------------------------------------------------------- thumbs
    def _start_thumbs(self, items):
        self._thumb_thread = QThread()
        self._thumb_worker = ThumbWorker(items)
        self._thumb_worker.moveToThread(self._thumb_thread)
        self._thumb_thread.started.connect(self._thumb_worker.run)
        self._thumb_worker.one_ready.connect(self._apply_thumb)
        self._thumb_worker.finished.connect(self._thumbs_done)
        self._thumb_thread.start()

    def _apply_thumb(self, file_id: int, path: str):
        it = self._iter_children()
        for child in it:
            if child.data(COL_NAME, ROLE_FILEID) == file_id:
                pm = QPixmap(path)
                if not pm.isNull():
                    child.setIcon(COL_PREVIEW, QIcon(pm))
                break

    def _thumbs_done(self):
        if self._thumb_thread:
            self._thumb_thread.quit()
            self._thumb_thread.wait()
            self._thumb_thread = None
            self._thumb_worker = None

    # ----------------------------------------------- iteration utilities
    def _iter_groups(self):
        for i in range(self.tree.topLevelItemCount()):
            yield self.tree.topLevelItem(i)

    def _iter_children(self):
        for top in self._iter_groups():
            for j in range(top.childCount()):
                yield top.child(j)

    # --------------------------------------------- selection handling
    def _select_button(self, item: QTreeWidgetItem) -> QPushButton | None:
        widget = self.tree.itemWidget(item, COL_ACTION)
        if widget is None:
            return None
        if isinstance(widget, QPushButton):
            return widget
        return widget.findChild(QPushButton)

    def _sync_select_button(self, item: QTreeWidgetItem) -> None:
        btn = self._select_button(item)
        if btn is None:
            return
        btn.setChecked(item.checkState(COL_NAME) == Qt.CheckState.Checked)

    def _set_child_selected(self, item: QTreeWidgetItem, selected: bool) -> None:
        item.setCheckState(
            COL_NAME,
            Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked,
        )
        self._sync_select_button(item)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int):
        if self._guard_busy or column != COL_NAME:
            return
            
        # Sync the explicit Select button state
        self._sync_select_button(item)

        self._update_summary()

    def _update_summary(self):
        count = bytes_ = 0
        for child in self._iter_children():
            if child.checkState(COL_NAME) == Qt.CheckState.Checked:
                count += 1
                bytes_ += child.data(COL_NAME, ROLE_SIZE) or 0
        self.summary_lbl.setText(
            f"Selected {count} files · {human(bytes_)}"
            if count else "Nothing selected"
        )
        self.del_btn.setEnabled(count > 0)
        self.del_btn.setText(
            "Permanently DELETE selected" if self.perm_cb.isChecked()
            else "Move selected to Trash"
        )

    # ------------------------------------------------- smart selection
    def _smart_select(self, criterion: str):
        self._guard_busy = True
        for top in self._iter_groups():
            grp = self._groups.get(top.data(COL_PREVIEW, ROLE_GROUPKEY))
            if grp and (grp.manual_review or grp.match_type == "hardlink"):
                continue
            kids = [top.child(j) for j in range(top.childCount())]
            if len(kids) < 2:
                continue
            if criterion == "size":
                keeper = max(kids,
                             key=lambda c: c.data(COL_NAME, ROLE_SIZE) or 0)
            else:  # resolution
                def res_key(c):
                    txt = c.text(COL_RES)
                    if "×" in txt:
                        w, h = txt.split("×")
                        try:
                            return int(w) * int(h)
                        except ValueError:
                            return 0
                    return 0
                keeper = max(kids, key=res_key)
            for c in kids:
                self._set_child_selected(c, c is not keeper)
        self._guard_busy = False
        self._update_summary()

    def _clear_selection(self):
        self._guard_busy = True
        for child in self._iter_children():
            self._set_child_selected(child, False)
        self._guard_busy = False
        self._update_summary()

    # --------------------------------------------------------- delete
    def _delete_selected(self):
        by_group: dict[str, list[tuple[int, str, int]]] = {}
        for child in self._iter_children():
            if child.checkState(COL_NAME) == Qt.CheckState.Checked:
                gk = cast(str, child.data(COL_NAME, ROLE_GROUPKEY))
                by_group.setdefault(gk, []).append((
                    cast(int, child.data(COL_NAME, ROLE_FILEID)),
                    cast(str, child.data(COL_NAME, ROLE_PATH)),
                    cast(int, child.data(COL_NAME, ROLE_SIZE) or 0),
                ))
        plan = DeletionPlan(by_group, permanent=self.perm_cb.isChecked())

        # Warning if trying to delete all copies
        fully_selected = []
        for gkey, items in plan.paths_by_group.items():
            grp = self._groups.get(gkey)
            if grp and len(items) >= len(grp.members):
                fully_selected.append(grp)
                
        if fully_selected:
            QMessageBox.critical(
                self, "Blocked: All Copies Selected",
                "You have selected EVERY copy in one or more groups.\n"
                "At least one file must remain in each group."
            )
            return

        try:
            validate(plan, self._groups)
        except GuardError as exc:
            QMessageBox.critical(self, "Blocked", str(exc))
            return

        manual_review_selected = [
            self._groups[gkey]
            for gkey in plan.paths_by_group
            if self._groups.get(gkey)
            and self._groups[gkey].manual_review
        ]
        if manual_review_selected:
            warn = QMessageBox.warning(
                self,
                "Manual review required",
                "One or more selected groups are possible matches, not "
                "confirmed duplicates. Review the files manually before "
                "removing anything.\n\nProceed with these selections?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if warn != QMessageBox.StandardButton.Yes:
                return

        manifest = build_manifest(plan, self._groups)
        dlg = QMessageBox(self)
        dlg.setWindowTitle("Confirm deletion")
        dlg.setIcon(QMessageBox.Icon.Warning)
        dlg.setText(
            "PERMANENT deletion — files will NOT be recoverable."
            if plan.permanent else
            "Files will be moved to the system Trash (recoverable)."
        )
        dlg.setDetailedText(manifest)
        dlg.setStandardButtons(
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes
        )
        dlg.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if dlg.exec() != QMessageBox.StandardButton.Yes:
            return

        if plan.permanent:
            ok = QMessageBox.question(
                self, "Are you absolutely sure?",
                f"This will permanently destroy {len(plan.all_paths)} "
                "file(s) with no Trash safety net.\n\nProceed?",
                QMessageBox.StandardButton.Cancel
                | QMessageBox.StandardButton.Yes,
                QMessageBox.StandardButton.Cancel,
            )
            if ok != QMessageBox.StandardButton.Yes:
                return

        done, errors = execute(plan, self._groups)
        self._log(f"Removed {done} file(s).")
        for e in errors:
            self._log(f"[delete-error] {e}")
        if errors:
            QMessageBox.warning(
                self, "Completed with errors",
                f"{done} removed, {len(errors)} failed. See log.",
            )
        else:
            QMessageBox.information(
                self, "Done", f"{done} file(s) processed successfully.")
        # Refresh from the (now updated) database.
        self.scan_btn.setEnabled(False)
        self.progress.setFormat("Building groups…")
        self._build_groups(self._active_scan_id)

    def closeEvent(self, event):
        for w in (self._scan_worker, self._thumb_worker):
            if w:
                w.cancel()
        for t in (self._scan_thread, self._thumb_thread, self._g_thread):
            if t:
                t.quit()
                t.wait(2000)
        super().closeEvent(event)


def main():
    import sys
    from ..logging_config import install_exception_hooks, setup_logging

    config.ensure_dirs()
    setup_logging()
    install_exception_hooks()
    app = QApplication(sys.argv)
    app.setApplicationName(config.APP_NAME)
    app.setDesktopFileName(config.APP_ID)
    icon_path = config.best_icon_path()
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path.resolve())))
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
