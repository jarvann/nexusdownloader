"""Live thread-activity monitor: a table of the work each worker thread is doing
right now, plus an aggregate header (active/max threads, done/failed tallies, and
a files/sec throughput). Mirrors the download ProgressMonitorWidget.

Used by BOTH the install tab (columns: Thread | Mod | Tool | Status, phases
extracting -> staging -> verifying) and the deploy tab (Thread | File | Status,
phase 'linking'). Driven by a single dict payload emitted (throttled) from a
worker thread: {active:[{thread,mod,phase,tool,done,total}], done, failed,
max_threads, files_per_sec}.
"""
from __future__ import annotations

from typing import Any, Dict

from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QHeaderView, QSizePolicy,
)

# Phase -> display colour (foreground), so the eye can scan what's happening.
_PHASE_COLORS = {
    "extracting": QColor(120, 170, 255),   # blue
    "staging": QColor(210, 170, 90),       # amber
    "hardlinking": QColor(210, 170, 90),
    "linking": QColor(210, 170, 90),
    "copying": QColor(210, 170, 90),
    "verifying": QColor(180, 130, 220),    # purple
}


class InstallMonitorWidget(QWidget):
    """Table of in-flight per-thread work + an aggregate header. Fed by update_view().

    item_header: label for the 2nd column ("Mod" for installs, "File" for deploy).
    show_tool:   include a "Tool" column (extractor name) -- installs only.
    """

    def __init__(self, initial_max_threads: int = 4, item_header: str = "Mod",
                 show_tool: bool = True):
        super().__init__()
        self.max_threads = initial_max_threads
        self.show_tool = show_tool
        # Column layout: 0=Thread, 1=item, [2=Tool], last=Status.
        cols = ["Thread", item_header] + (["Tool"] if show_tool else []) + ["Status"]
        self._cols = cols
        self._c_item = 1
        self._c_tool = 2 if show_tool else None
        self._c_status = len(cols) - 1
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # --- header: active / max / done / failed / throughput -----------------
        header = QHBoxLayout()
        bold = QFont("Arial", 9, QFont.Bold)
        self.active_label = QLabel("Active: 0")
        self.active_label.setFont(bold)
        self.max_label = QLabel(f"Max: {self.max_threads}")
        self.done_label = QLabel("Done: 0")
        self.done_label.setStyleSheet("QLabel { color: #4caf50; }")
        self.failed_label = QLabel("Failed: 0")
        self.failed_label.setStyleSheet("QLabel { color: #e57373; }")
        self.rate_label = QLabel("0 files/s")
        for w in (self.active_label, self.max_label, self.done_label,
                  self.failed_label):
            header.addWidget(w)
        header.addStretch()
        header.addWidget(self.rate_label)
        layout.addLayout(header)

        # --- the live table ----------------------------------------------------
        self.table = QTableWidget()
        self.table.setColumnCount(len(self._cols))
        self.table.setHorizontalHeaderLabels(self._cols)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        hh = self.table.horizontalHeader()
        for c in range(len(self._cols)):
            hh.setSectionResizeMode(
                c, QHeaderView.Stretch if c == self._c_item else QHeaderView.Fixed)
        self.table.setColumnWidth(0, 90)
        if self._c_tool is not None:
            self.table.setColumnWidth(self._c_tool, 90)
        self.table.setColumnWidth(self._c_status, 150)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table, 1)

    def set_max_threads(self, n: int):
        self.max_threads = int(n)
        self.max_label.setText(f"Max: {self.max_threads}")

    def reset(self):
        self.table.setRowCount(0)
        self.active_label.setText("Active: 0")
        self.done_label.setText("Done: 0")
        self.failed_label.setText("Failed: 0")
        self.rate_label.setText("0 files/s")

    def update_view(self, payload: Dict[str, Any]):
        """Render one snapshot; rows are sorted by thread for stable ordering."""
        active = payload.get("active", []) or []
        self.active_label.setText(f"Active: {len(active)}")
        if payload.get("max_threads"):
            self.set_max_threads(payload["max_threads"])
        self.done_label.setText(f"Done: {payload.get('done', 0):,}")
        self.failed_label.setText(f"Failed: {payload.get('failed', 0):,}")
        fps = payload.get("files_per_sec", 0) or 0
        self.rate_label.setText(f"{fps:,.0f} files/s")

        rows = sorted(active, key=lambda r: r.get("thread", 0))
        sb = self.table.verticalScrollBar()
        pos = sb.value()
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(str(r.get("thread", ""))[-5:]))
            item = QTableWidgetItem(r.get("mod", ""))
            item.setToolTip(r.get("mod", ""))
            self.table.setItem(i, self._c_item, item)
            if self._c_tool is not None:
                self.table.setItem(i, self._c_tool, QTableWidgetItem(r.get("tool") or ""))
            phase = (r.get("phase") or "").lower()
            done, total = r.get("done"), r.get("total")
            if total:
                label = f"{phase} {done}/{total}" if done is not None else f"{phase} ({total:,})"
            else:
                label = phase or "working"
            st = QTableWidgetItem(label)
            color = _PHASE_COLORS.get(phase)
            if color:
                st.setForeground(color)
            self.table.setItem(i, self._c_status, st)
        self.table.setUpdatesEnabled(True)
        sb.setValue(pos)
