"""
Shared collection selector for the NexusDownloader GUI.

A single "Collection:" bar (dropdown of collections found in the staging folder +
Browse + Refresh) that lives above the tabs and drives every tab, so the user
picks the active collection once instead of per-tab. Supports multiple
collections: the dropdown lists each ``<staging>/<folder>/collection.json`` by its
display name; Browse handles collections not yet in staging.
"""

import os
import glob
import json
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QComboBox, QPushButton, QFileDialog,
)


class CollectionSelector(QWidget):
    """Pick the active collection; emits its collection.json path on change."""

    collection_selected = Signal(str)   # absolute path to collection.json

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.staging_path = ""
        self._build()
        self._autodetect_staging()
        self.refresh()

    def _build(self):
        row = QHBoxLayout(self)
        row.addWidget(QLabel("Collection:"))
        self.combo = QComboBox()
        self.combo.setMinimumWidth(380)
        self.combo.currentIndexChanged.connect(self._on_changed)
        row.addWidget(self.combo, 1)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setToolTip("Re-scan the staging folder for collections")
        self.refresh_btn.clicked.connect(self.refresh)
        row.addWidget(self.refresh_btn)
        self.browse_btn = QPushButton("Browse…")
        self.browse_btn.setToolTip("Pick a collection.json not in the staging folder")
        self.browse_btn.clicked.connect(self._browse)
        row.addWidget(self.browse_btn)

    def _autodetect_staging(self):
        try:
            from gui.vortex_detect import find_staging_for
            staging = find_staging_for("skyrimse")
            if staging and os.path.isdir(staging):
                self.staging_path = staging
        except Exception:
            pass

    def refresh(self):
        """Re-scan staging and repopulate the dropdown (keeps current if still there)."""
        current = self.current_path()
        self.combo.blockSignals(True)
        self.combo.clear()
        found = []
        if self.staging_path and os.path.isdir(self.staging_path):
            for cj in glob.glob(os.path.join(self.staging_path, "*", "collection.json")):
                found.append((self._display_name(cj), cj))
        found.sort(key=lambda t: t[0].lower())
        for name, path in found:
            self.combo.addItem(name, path)
        if not found:
            self.combo.addItem("— no collections found (use Browse…) —", "")
        # restore prior selection if it survived the rescan
        if current:
            idx = self.combo.findData(current)
            if idx >= 0:
                self.combo.setCurrentIndex(idx)
        self.combo.blockSignals(False)
        self._on_changed()

    @staticmethod
    def _display_name(collection_json: str) -> str:
        folder = os.path.basename(os.path.dirname(collection_json))
        try:
            with open(collection_json, "r", encoding="utf-8") as fh:
                info = (json.load(fh).get("info") or {})
            name = info.get("name")
            return f"{name}  [{folder}]" if name else folder
        except Exception:
            return folder

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select collection.json", self.staging_path or "",
            "Collection (collection.json);;JSON files (*.json);;All files (*.*)")
        if path:
            idx = self.combo.findData(path)
            if idx < 0:
                self.combo.addItem(self._display_name(path), path)
                idx = self.combo.count() - 1
            self.combo.setCurrentIndex(idx)

    def _on_changed(self, *_):
        path = self.current_path()
        if path:
            self.collection_selected.emit(path)

    def current_path(self) -> str:
        return self.combo.currentData() or ""

    def set_staging(self, staging_path: str):
        """Point the dropdown at a different game's staging folder and rescan."""
        self.staging_path = staging_path or ""
        self.refresh()
