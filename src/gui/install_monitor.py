"""Live install monitor: a table of the mods currently being installed, one row
per active worker thread, mirroring the download ProgressMonitorWidget.

Unlike the old completed-list, this shows what each thread is doing *right now*
(extracting -> staging -> verifying) plus an aggregate header (active thread
count, done/failed tallies, and a files/sec throughput readout). It is driven by
a single dict payload emitted (throttled) from the install worker thread.
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
    "copying": QColor(210, 170, 90),
    "verifying": QColor(180, 130, 220),    # purple
}


class InstallMonitorWidget(QWidget):
    """Table of in-flight mod installs + an aggregate header. Fed by update_view()."""

    def __init__(self, initial_max_threads: int = 4):
        super().__init__()
        self.max_threads = initial_max_threads
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
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Thread", "Mod", "Tool", "Status"])
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Fixed)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.Fixed)
        hh.setSectionResizeMode(3, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 90)
        self.table.setColumnWidth(2, 90)
        self.table.setColumnWidth(3, 150)
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
        """Render one snapshot. payload: {active:[{thread,mod,phase}], done, failed,
        max_threads, files_per_sec}. Rows are sorted by thread for stable ordering."""
        active = payload.get("active", []) or []
        self.active_label.setText(f"Active: {len(active)}")
        if payload.get("max_threads"):
            self.set_max_threads(payload["max_threads"])
        self.done_label.setText(f"Done: {payload.get('done', 0)}")
        self.failed_label.setText(f"Failed: {payload.get('failed', 0)}")
        fps = payload.get("files_per_sec", 0) or 0
        self.rate_label.setText(f"{fps:,.0f} files/s")

        rows = sorted(active, key=lambda r: r.get("thread", 0))
        # Preserve the scroll position across refreshes so the view doesn't jump.
        sb = self.table.verticalScrollBar()
        pos = sb.value()
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            tid = str(r.get("thread", ""))[-5:]   # last digits keep it short
            self.table.setItem(i, 0, QTableWidgetItem(tid))
            mod_item = QTableWidgetItem(r.get("mod", ""))
            mod_item.setToolTip(r.get("mod", ""))
            self.table.setItem(i, 1, mod_item)
            self.table.setItem(i, 2, QTableWidgetItem(r.get("tool") or ""))
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
            self.table.setItem(i, 3, st)
        self.table.setUpdatesEnabled(True)
        sb.setValue(pos)
