"""
Deploy & Play tab for the NexusDownloader GUI.

The final stage after download/install/link: hard-link the staged mods into the
game folder, write the deployment manifest, sort plugins, mark the deployment in
Vortex's DB -- then launch the game. Reuses the shared :class:`PhasePanel` for the
live deploy grid, mirroring the Download/Install phases.
"""

import os
import glob
import re
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QMessageBox,
    QGridLayout, QFileDialog, QGroupBox,
)

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gui.phase_panel import PhasePanel

_REV_FOLDER = re.compile(r"-\d+-\d+-\d+$")


def _find_collection_json(staging: str) -> Optional[str]:
    matches = glob.glob(os.path.join(staging, "*", "collection.json"))
    if not matches:
        return None
    preferred = [m for m in matches if _REV_FOLDER.search(os.path.basename(os.path.dirname(m)))]
    return max(preferred or matches, key=os.path.getmtime)


def _default_localappdata() -> str:
    """The game's %LOCALAPPDATA% dir (for plugins.txt). Skyrim SE default."""
    if os.name == "nt":
        return os.path.expandvars(r"%LOCALAPPDATA%\Skyrim Special Edition")
    return "/mnt/c/Users/cory/AppData/Local/Skyrim Special Edition"


class DeployWorkerThread(QThread):
    """Runs the full deploy pipeline off the UI thread: order -> hard-link ->
    manifest -> mark deployed -> sort plugins. Emits live progress."""

    progress = Signal(int, int, str)        # done, total, current rel path
    status = Signal(str)                    # human status line
    finished_ok = Signal(object)            # FinalizeResult
    failed = Signal(str)
    busy = Signal(str)                      # Vortex running / DB locked

    def __init__(self, collection_path, staging_path, game_data_dir,
                 localappdata_dir, db_path="", workers=16):
        super().__init__()
        self.collection_path = collection_path
        self.staging_path = staging_path
        self.game_data_dir = game_data_dir
        self.localappdata_dir = localappdata_dir
        self.db_path = db_path
        self.workers = workers

    def run(self):
        try:
            import json
            import time
            from utils import vortex_deploy as dep
            from utils import vortex_db
            from utils.vortex_db import VortexBusyError

            db = self.db_path or vortex_db.find_state_db()
            if not db or not os.path.exists(db):
                self.failed.emit(
                    "Could not find Vortex's state.v2 database. Set it in Advanced.")
                return
            with open(self.collection_path, "r", encoding="utf-8") as fh:
                collection = json.load(fh)

            self.status.emit("Ordering mods + hard-linking into the game folder...")
            try:
                res = dep.finalize_collection(
                    db, collection, self.staging_path, self.game_data_dir,
                    self.localappdata_dir, deployment_time_ms=int(time.time() * 1000),
                    workers=self.workers,
                    progress=lambda d, t, n: self.progress.emit(d, t, n))
            except VortexBusyError as e:
                self.busy.emit(str(e))
                return
            self.status.emit(
                f"Deployed {res.deploy.files:,} files; sorted {res.active_plugins:,} plugins.")
            self.finished_ok.emit(res)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


class DeployTab(QWidget):
    """Deploy the staged collection into the game, then launch it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.collection_path = ""
        self.staging_path = ""
        self.game_data_dir = ""
        self.localappdata_dir = _default_localappdata()
        self.skse_path = ""
        self._thread = None
        self._setup_ui()
        self.auto_detect()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        info = QLabel(
            "Final step: deploy the installed mods into the game (hard-links), "
            "sort the load order, and mark Vortex's deployment as current. "
            "Make sure Vortex is fully CLOSED first.")
        info.setWordWrap(True)
        layout.addWidget(info)

        # Editable path rows (auto-detected, but always overridable via Browse).
        box = QGroupBox("Paths")
        grid = QGridLayout(box)
        self._path_lbls = {}

        def add_row(row, key, caption, is_file):
            grid.addWidget(QLabel(caption), row, 0)
            val = QLabel("— not set —")
            val.setStyleSheet("QLabel { border: 1px solid gray; padding: 3px; }")
            val.setWordWrap(True)
            grid.addWidget(val, row, 1)
            btn = QPushButton("Browse…")
            btn.clicked.connect(lambda _=False, k=key, f=is_file: self._browse_path(k, f))
            grid.addWidget(btn, row, 2)
            self._path_lbls[key] = val

        add_row(0, "collection", "Collection file:", True)
        add_row(1, "staging", "Mod staging:", False)
        add_row(2, "game_data", "Game Data folder:", False)
        add_row(3, "localappdata", "Plugins (LOCALAPPDATA):", False)

        self.redetect_btn = QPushButton("Re-detect from Vortex")
        self.redetect_btn.clicked.connect(self.auto_detect)
        grid.addWidget(self.redetect_btn, 4, 1, 1, 2)
        layout.addWidget(box)

        btns = QHBoxLayout()
        self.deploy_btn = QPushButton("Deploy + Sort")
        self.deploy_btn.clicked.connect(self.start_deploy)
        btns.addWidget(self.deploy_btn)

        self.launch_btn = QPushButton("▶ Launch Game")
        self.launch_btn.setEnabled(False)
        self.launch_btn.clicked.connect(self.launch_game)
        btns.addWidget(self.launch_btn)
        btns.addStretch()
        layout.addLayout(btns)

        self.panel = PhasePanel("Deploy Progress")
        layout.addWidget(self.panel)
        layout.addStretch()

    # --- path discovery --------------------------------------------------- #
    def auto_detect(self):
        """Fill paths from Vortex's state.v2 (game install -> Data folder, staging,
        newest collection.json). Requires Vortex closed; fails silently if locked --
        the user can always set paths via Browse."""
        try:
            from utils import vortex_db
            games = []
            db = vortex_db.find_state_db()
            if db:
                try:
                    games = vortex_db.read_vortex_games(db)
                except Exception:
                    games = []
            g = next((x for x in games if x["game"] == "skyrimse"), None) \
                or (games[0] if games else None)
            if g:
                if g.get("staging") and os.path.isdir(g["staging"]):
                    self.staging_path = g["staging"]
                install = g.get("install") or ""
                if install:
                    data = os.path.join(install, "Data")
                    if os.path.isdir(data):
                        self.game_data_dir = data
                    cand = os.path.join(install, "skse64_loader.exe")
                    if os.path.exists(cand):
                        self.skse_path = cand
            if self.staging_path and not self.collection_path:
                cj = _find_collection_json(self.staging_path)
                if cj:
                    self.collection_path = cj
        except Exception:
            pass
        self._refresh_paths_label()

    def _browse_path(self, key, is_file):
        if is_file:
            path, _ = QFileDialog.getOpenFileName(
                self, "Select collection.json", self.staging_path or "",
                "Collection (collection.json);;JSON (*.json);;All files (*.*)")
        else:
            start = (self.game_data_dir or self.staging_path or "")
            path = QFileDialog.getExistingDirectory(self, "Select folder", start)
        if not path:
            return
        setattr(self, {"collection": "collection_path", "staging": "staging_path",
                       "game_data": "game_data_dir", "localappdata": "localappdata_dir"}[key], path)
        self._refresh_paths_label()

    def set_paths(self, collection="", staging="", game_data=""):
        """Let other tabs (Install) push the paths the user picked."""
        if collection:
            self.collection_path = collection
        if staging:
            self.staging_path = staging
        if game_data:
            self.game_data_dir = game_data
        self._refresh_paths_label()

    def _refresh_paths_label(self):
        vals = {
            "collection": self.collection_path,
            "staging": self.staging_path,
            "game_data": self.game_data_dir,
            "localappdata": self.localappdata_dir,
        }
        for k, lbl in self._path_lbls.items():
            lbl.setText(vals.get(k) or "— not set —")
        ready = all([self.collection_path, self.staging_path, self.game_data_dir])
        self.deploy_btn.setEnabled(ready)

    # --- deploy ----------------------------------------------------------- #
    def start_deploy(self):
        if not all([self.collection_path, self.staging_path, self.game_data_dir]):
            QMessageBox.warning(self, "Deploy", "Paths aren't fully detected. "
                                "Set them in the Install tab or Advanced.")
            return
        self.deploy_btn.setEnabled(False)
        self.launch_btn.setEnabled(False)
        self.panel.reset()
        self.panel.start("Starting deploy...")
        self._thread = DeployWorkerThread(
            self.collection_path, self.staging_path, self.game_data_dir,
            self.localappdata_dir)
        self._thread.progress.connect(self._on_progress)
        self._thread.status.connect(lambda m: self.panel.set_progress(
            self.panel.bar.value(), max(self.panel.bar.maximum(), 1), m))
        self._thread.finished_ok.connect(self._on_done)
        self._thread.failed.connect(self._on_failed)
        self._thread.busy.connect(self._on_busy)
        self._thread.start()

    def _on_progress(self, done, total, name):
        self.panel.set_progress(done, total, f"Hard-linking… {name}")
        if done % 1000 == 0:
            self.panel.add_item(name, "ok")

    def _on_done(self, res):
        self.deploy_btn.setEnabled(True)
        self.launch_btn.setEnabled(bool(self.skse_path) or True)
        esl = getattr(res, "esl_flagged", 0)
        self.panel.finish(
            f"Deployed {res.deploy.files:,} files, {res.active_plugins:,} plugins active"
            + (f", {esl:,} auto-flagged light (ESL)" if esl else "")
            + ". Ready to play.", ok=True)
        if esl:
            self.panel.log("INFO", f"ESL: marked {esl:,} eligible plugins as light "
                                   "(keeps the full-plugin count under Skyrim's 254 cap)")
        self.panel.log("INFO", f"manifest: {res.deploy.manifest_path}")
        self.panel.log("INFO", f"plugins.txt: {res.plugins_path}")

    def _on_failed(self, msg):
        self.deploy_btn.setEnabled(True)
        self.panel.finish(msg, ok=False)
        QMessageBox.critical(self, "Deploy failed", msg)

    def _on_busy(self, msg):
        self.deploy_btn.setEnabled(True)
        self.panel.finish("Vortex is open — close it and retry.", ok=False)
        QMessageBox.warning(self, "Close Vortex", msg)

    # --- launch ----------------------------------------------------------- #
    def launch_game(self):
        skse = self.skse_path
        if not skse or not os.path.exists(skse):
            # let the user point at it
            from PySide6.QtWidgets import QFileDialog
            skse, _ = QFileDialog.getOpenFileName(
                self, "Locate skse64_loader.exe", "", "Executables (*.exe);;All files (*.*)")
            if not skse:
                return
            self.skse_path = skse
        try:
            import subprocess
            subprocess.Popen([skse], cwd=os.path.dirname(skse))
            self.panel.log("INFO", f"Launched: {skse}")
        except Exception as e:
            QMessageBox.critical(self, "Launch failed", str(e))
