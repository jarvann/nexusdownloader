"""
Reusable phase-progress panel for the NexusDownloader GUI.

A single widget that every long-running phase (Download, Install, Deploy) can
reuse instead of each tab hand-rolling its own progress bar + status + list +
log. Keeps the live "grid" visibility the UX relies on while centralizing the
look and behavior in one place.

Usage:
    panel = PhasePanel("Deploy")
    panel.start("Hard-linking files...")
    panel.set_progress(done, total)            # drives the bar + "(d/t)"
    panel.add_item("textures/foo.dds", "ok")   # live grid row (ok|fail|active)
    panel.log("INFO", "...")                   # collapsible detail log
    panel.finish("Deployed 728,071 files", ok=True)
"""

from typing import Optional

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar, QListWidget,
    QListWidgetItem, QTextEdit, QToolButton, QGroupBox,
)

_STATUS_COLORS = {
    "ok": QColor(40, 120, 40),
    "fail": QColor(150, 40, 40),
    "active": QColor(40, 70, 130),
    "skip": QColor(90, 90, 90),
}


class PhasePanel(QGroupBox):
    """One phase's live progress: bar + status + item grid + collapsible log."""

    def __init__(self, title: str, parent: Optional[QWidget] = None,
                 show_grid: bool = True, monitor: bool = False,
                 monitor_header: str = "File"):
        super().__init__(title, parent)
        self._build(show_grid, monitor, monitor_header)

    def _build(self, show_grid: bool, monitor: bool = False,
               monitor_header: str = "File"):
        root = QVBoxLayout(self)

        self.bar = QProgressBar()
        self.bar.setTextVisible(True)
        root.addWidget(self.bar)

        self.status = QLabel("Idle")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        # Optional live per-thread activity table (same widget the install tab
        # uses). Hidden until the first set_active() so ops that don't feed it
        # (purge/verify/reset) still show just the bar + grid.
        self.monitor = None
        if monitor:
            from gui.install_monitor import InstallMonitorWidget
            self.monitor = InstallMonitorWidget(item_header=monitor_header, show_tool=False)
            self.monitor.setVisible(False)
            root.addWidget(self.monitor, 1)

        if show_grid:
            self.grid = QListWidget()
            self.grid.setUniformItemSizes(True)          # cheap with many rows
            self.grid.setMaximumHeight(220)
            root.addWidget(self.grid)
        else:
            self.grid = None

        # Collapsible details log (hidden by default to keep things uncluttered)
        self._log_toggle = QToolButton()
        self._log_toggle.setText("Show details")
        self._log_toggle.setCheckable(True)
        self._log_toggle.setStyleSheet("QToolButton { border: none; }")
        self._log_toggle.toggled.connect(self._on_toggle_log)
        row = QHBoxLayout()
        row.addWidget(self._log_toggle)
        row.addStretch()
        root.addLayout(row)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.document().setMaximumBlockCount(2000)
        self.log_view.setVisible(False)
        self.log_view.setMaximumHeight(160)
        root.addWidget(self.log_view)

    def _on_toggle_log(self, on: bool):
        self.log_view.setVisible(on)
        self._log_toggle.setText("Hide details" if on else "Show details")

    # --- public API ------------------------------------------------------- #
    def reset(self):
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.status.setText("Idle")
        if self.grid is not None:
            self.grid.clear()
        if self.monitor is not None:
            self.monitor.reset()
            self.monitor.setVisible(False)
        self.log_view.clear()

    def set_active(self, payload):
        """Feed the live per-thread activity table (shown on first call). payload is
        the same dict the install monitor consumes: {active, done, failed,
        max_threads, files_per_sec}."""
        if self.monitor is None:
            return
        if not self.monitor.isVisible():
            self.monitor.setVisible(True)
        self.monitor.update_view(payload)

    def start(self, message: str = "Working..."):
        self.bar.setRange(0, 0)        # indeterminate until first set_progress
        self.status.setText(message)

    def set_progress(self, done: int, total: int, message: str = ""):
        if total > 0:
            self.bar.setRange(0, total)
            self.bar.setValue(min(done, total))
            self.bar.setFormat(f"%p%  ({done:,}/{total:,})")
        if message:
            self.status.setText(message)

    def add_item(self, text: str, status: str = "active", detail: str = ""):
        """Add a live grid row. status in {ok, fail, active, skip}."""
        if self.grid is None:
            return
        item = QListWidgetItem(text if not detail else f"{text}  —  {detail}")
        color = _STATUS_COLORS.get(status)
        if color is not None:
            item.setForeground(color)
        self.grid.addItem(item)
        self.grid.scrollToBottom()
        # Keep the grid bounded so huge collections don't bloat memory.
        if self.grid.count() > 400:
            self.grid.takeItem(0)

    def log(self, level: str, message: str):
        self.log_view.append(f"[{level}] {message}")

    def finish(self, message: str, ok: bool = True):
        if self.bar.maximum() == 0:           # was indeterminate
            self.bar.setRange(0, 1)
            self.bar.setValue(1)
        else:
            self.bar.setValue(self.bar.maximum())
        self.status.setText(("✓ " if ok else "✗ ") + message)
