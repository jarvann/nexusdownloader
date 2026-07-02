"""
Installation tab for the NexusDownloader GUI.

Provides interface for installing downloaded mods using FOMOD technology
with collection-based automation.
"""

import os
import json
import time
import threading
from pathlib import Path
from typing import Optional, Dict, Any, List

import html as _html

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFileDialog,
    QTextEdit, QProgressBar, QGroupBox, QGridLayout, QListWidget,
    QListWidgetItem, QMessageBox, QSpinBox, QCheckBox, QInputDialog, QDialog,
    QScrollArea, QDialogButtonBox, QStyle
)
from PySide6.QtCore import QThread, Signal, Qt

# Import installation utilities
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from utils.fomod_installer import create_fomod_installer, create_parallel_fomod_installer, InstallationResult, InstallResult
from utils.unified_logging import create_operation_logger


class VortexSyncWorkerThread(QThread):
    """Runs the Vortex DB sync off the UI thread (dry-run or apply)."""

    finished_result = Signal(object)   # SyncResult
    failed = Signal(str)
    busy = Signal(str)                 # Vortex running / DB locked -> close Vortex

    def __init__(self, collection_path, downloads_path, staging_path, apply, force):
        super().__init__()
        self.collection_path = collection_path
        self.downloads_path = downloads_path
        self.staging_path = staging_path
        self.apply = apply
        self.force = force

    def run(self):
        try:
            from utils import vortex_sync
            from utils.vortex_db import VortexBusyError
            try:
                res = vortex_sync.sync_collection(
                    self.collection_path, self.downloads_path, self.staging_path,
                    apply=self.apply, force=self.force)
                self.finished_result.emit(res)
            except VortexBusyError as e:
                self.busy.emit(str(e))
        except Exception as e:
            self.failed.emit(str(e))


class InstallWorkerThread(QThread):
    """Worker thread for mod installation operations."""

    progress_updated = Signal(int, int, str)  # current, total, mod_name
    installation_complete = Signal(str, bool, str, int)  # mod_name, success, message, file_count
    log_message = Signal(str, str)  # level, message
    installation_finished = Signal(list)  # List of InstallationResult
    active_installs_updated = Signal(dict)  # {active:[{thread,mod,phase}], done, failed, max_threads, files_per_sec}

    def __init__(self, collection_path: str, downloads_path: str, staging_path: str,
                 use_parallel: bool = True, max_workers: int = 4, temp_root: str = "",
                 game_root: str = ""):
        super().__init__()
        self.collection_path = collection_path
        self.downloads_path = downloads_path
        self.staging_path = staging_path
        self.is_cancelled = False
        self.use_parallel = use_parallel
        self.max_workers = max_workers
        self.temp_root = temp_root
        self.game_root = game_root or ""
        self._installer = None   # live ref so concurrency can be changed mid-run
        # Live active-thread tracking for the monitor view.
        self._active: Dict[int, dict] = {}     # thread_id -> {mod, phase}
        self._active_lock = threading.Lock()
        self._done_count = 0
        self._failed_count = 0
        self._files_total = 0
        self._run_t0 = None                    # first-completion timestamp for files/sec
        self._last_emit = 0.0                  # throttle gate
    
    def cancel(self):
        """Cancel the installation process."""
        self.is_cancelled = True
        # Propagate to the running installer so queued mods stop being started.
        inst = self._installer
        if inst is not None and hasattr(inst, "cancel"):
            inst.cancel()

    def set_concurrency(self, n: int):
        """Change install concurrency live (no restart) if an installer is active."""
        inst = self._installer
        if inst is not None and hasattr(inst, "set_concurrency"):
            inst.set_concurrency(n)

    def run(self):
        """Run the installation process."""
        logger = None
        ledger_handler = None
        try:
            self.log_message.emit("DEBUG", "InstallWorkerThread.run() started")
            self.log_message.emit("INFO", f"Starting installation from {self.collection_path}")
            
            # Load collection data
            self.log_message.emit("DEBUG", f"Loading collection from {self.collection_path}")
            with open(self.collection_path, 'r', encoding='utf-8') as f:
                collection_data = json.load(f)
            self.log_message.emit("DEBUG", "Collection loaded successfully")
            
            mods = collection_data.get("mods", [])
            total_mods = len(mods)
            self.log_message.emit("DEBUG", f"Found {total_mods} mods in collection")
            
            if self.use_parallel:
                self.log_message.emit("DEBUG", f"Using parallel installation with {self.max_workers} workers")
                self.log_message.emit("INFO", f"Found {total_mods} mods to install using {self.max_workers} parallel threads")
            else:
                self.log_message.emit("DEBUG", "Using sequential installation")
                self.log_message.emit("INFO", f"Found {total_mods} mods to install (sequential)")
            
            # Create installer
            self.log_message.emit("DEBUG", "Creating installer logger")
            # Setup installer logger using unified logging system
            logger = create_operation_logger("install", "skyrimse")
            # Route this operation's logs into the ledger (one queryable place).
            # The parallel installer logs through the same-named "install" logger,
            # so this captures the per-mod lines too. Detached in `finally`.
            try:
                from utils import local_state
                ledger_handler = local_state.attach_operation_logging(
                    logger, self.staging_path, "install")
            except Exception as e:
                self.log_message.emit("DEBUG", f"ledger logging unavailable: {e}")
            self.log_message.emit("DEBUG", f"Logger created with handlers: {[type(h).__name__ for h in logger.handlers]}")
            logger.info("Installation process started from GUI")
            
            if self.use_parallel and total_mods > 1:
                self.log_message.emit("DEBUG", "Starting parallel installation")
                # Use parallel installer for multiple mods
                config = {
                    "installation_timeout_seconds": 600
                }
                self.log_message.emit("DEBUG", "Creating parallel installer")
                # Pass None as logger so installer creates its own file logger
                with create_parallel_fomod_installer(self.staging_path, None, self.max_workers, config, self.temp_root or None, self.game_root or None) as installer:
                    self._installer = installer   # expose for live concurrency changes
                    # Set up callbacks for real-time progress updates
                    self.log_message.emit("DEBUG", "Setting up callbacks")
                    installer.set_progress_callback(self._on_progress_update)
                    installer.set_installation_callback(self._on_installation_complete)
                    installer.set_status_callback(self._on_status)
                    
                    # Run parallel installation (includes pre-scan)
                    self.log_message.emit("DEBUG", "Starting install_collection_parallel with pre-scan")
                    self.log_message.emit("INFO", f"Scanning staging folder for existing installations...")
                    results = installer.install_collection_parallel(collection_data, self.downloads_path)
                    self.log_message.emit("DEBUG", f"Parallel installation completed with {len(results)} results")
                    
                    # Report scan results
                    skipped_count = sum(1 for r in results if r.status == InstallResult.SKIPPED)
                    if skipped_count > 0:
                        self.log_message.emit("INFO", f"Found {skipped_count} mods already installed - skipped reinstallation")
                    
                    if not self.is_cancelled:
                        self.progress_updated.emit(total_mods, total_mods, "Complete")
                        self.installation_finished.emit(results)
                        
                        successful = sum(1 for r in results if r.status == InstallResult.SUCCESS)
                        failed = sum(1 for r in results if r.status == InstallResult.FAILED)
                        skipped = sum(1 for r in results if r.status == InstallResult.SKIPPED)
                        self.log_message.emit("INFO", f"Parallel installation finished: {successful} successful, {failed} failed, {skipped} skipped")
            else:
                self.log_message.emit("DEBUG", "Starting sequential installation")
                # Use sequential installer for single mod or when parallel is disabled
                self.log_message.emit("DEBUG", "Creating sequential installer")
                self.log_message.emit("INFO", f"Scanning staging folder for existing installations...")
                # Pass None as logger so installer creates its own file logger
                with create_fomod_installer(self.staging_path, None, self.temp_root or None, self.game_root or None) as installer:
                    # Run sequential installation (includes pre-scan)
                    results = installer.install_collection(collection_data, self.downloads_path)
                    
                    # Report scan results
                    skipped_count = sum(1 for r in results if r.status == InstallResult.SKIPPED)
                    if skipped_count > 0:
                        self.log_message.emit("INFO", f"Found {skipped_count} mods already installed - skipped reinstallation")
                    
                    # Skip the old manual loop since install_collection now handles everything
                    if not self.is_cancelled:
                        self.progress_updated.emit(total_mods, total_mods, "Complete")
                        self.installation_finished.emit(results)
                        
                        successful = sum(1 for r in results if r.status == InstallResult.SUCCESS)
                        failed = sum(1 for r in results if r.status == InstallResult.FAILED)
                        skipped = sum(1 for r in results if r.status == InstallResult.SKIPPED)
                        self.log_message.emit("INFO", f"Installation finished: {successful} successful, {failed} failed, {skipped} skipped")
                
                return  # Exit early since install_collection handled everything
        
        except Exception as e:
            self.log_message.emit("ERROR", f"Exception in InstallWorkerThread.run(): {e}")
            import traceback
            traceback.print_exc()
            self.log_message.emit("ERROR", f"Installation failed: {str(e)}")
        finally:
            if ledger_handler is not None and logger is not None:
                logger.removeHandler(ledger_handler)

    def _on_progress_update(self, current: int, total: int, mod_name: str):
        """Callback for parallel installer progress updates."""
        if not self.is_cancelled:
            self.progress_updated.emit(current, total, mod_name)
    
    def _on_status(self, thread_id: int, mod_name: str, phase):
        """Installer phase callback (worker-thread): update the active-thread map.

        Runs on ThreadPoolExecutor worker threads; only touches a locked dict and a
        throttled Qt signal (safe to emit cross-thread), so it never blocks install
        work."""
        with self._active_lock:
            self._active[thread_id] = {"thread": thread_id, "mod": mod_name,
                                       "phase": phase or "working"}
        self._emit_active_snapshot()

    def _emit_active_snapshot(self, force: bool = False):
        """Emit a throttled snapshot of active threads + tallies + files/sec."""
        now = time.monotonic()
        if not force and (now - self._last_emit) < 0.2:
            return
        self._last_emit = now
        with self._active_lock:
            active = list(self._active.values())
        elapsed = (now - self._run_t0) if self._run_t0 else 0
        fps = (self._files_total / elapsed) if elapsed > 0.5 else 0
        self.active_installs_updated.emit({
            "active": active,
            "done": self._done_count,
            "failed": self._failed_count,
            "max_threads": self.max_workers,
            "files_per_sec": fps,
        })

    def _on_installation_complete(self, mod_name: str, success: bool, message: str,
                                  file_count: int = 0):
        """Callback for individual mod installation completion."""
        # Free this thread's active row + update tallies/throughput.
        if self._run_t0 is None:
            self._run_t0 = time.monotonic()
        with self._active_lock:
            for tid, row in list(self._active.items()):
                if row.get("mod") == mod_name:
                    del self._active[tid]
                    break
        if success:
            self._done_count += 1
            self._files_total += max(0, file_count)
        else:
            self._failed_count += 1
        self._emit_active_snapshot(force=True)
        if not self.is_cancelled:
            self.installation_complete.emit(mod_name, success, message, file_count)


def collection_mod_key(m: dict) -> str:
    """Stable per-mod key for remembering selections across installs.

    Prefers the collection entry's ``tag`` (a stable per-file identifier Nexus
    assigns), falling back to ``modId-fileId`` then the name.
    """
    s = m.get("source") or {}
    if s.get("tag"):
        return f"tag:{s['tag']}"
    if s.get("modId") and s.get("fileId"):
        return f"mf:{s['modId']}-{s['fileId']}"
    return f"name:{m.get('name', '')}"


def collection_key_for(cdata: dict) -> str:
    """Stable per-collection key (name + game) for scoping saved selections."""
    info = cdata.get("info", {}) or {}
    return f"{info.get('domainName', '')}:{info.get('name', '') or 'collection'}"


def collection_mod_message(m: dict) -> str:
    """The collection author's note/warning for a mod, if any.

    Lives in the top-level ``instructions`` field (and, for off-site mods, in
    ``source.instructions``)."""
    return (m.get("instructions") or (m.get("source") or {}).get("instructions") or "").strip()


class OptionalSelectionDialog(QDialog):
    """Let the user opt IN to optional collection mods, and shows off-site/manual
    mods they must download themselves. Pre-checks any previously-saved choices
    for this collection (``preselected`` = set of mod keys)."""

    def __init__(self, optional_mods, offsite_mods, parent=None, preselected=None):
        super().__init__(parent)
        self.setWindowTitle("Choose optional mods")
        self.resize(560, 520)
        self._checks = []
        preselected = preselected or set()
        layout = QVBoxLayout(self)

        remembered = sum(1 for m in optional_mods if collection_mod_key(m) in preselected)
        hint = (f"  ({remembered} remembered from a previous install)" if remembered else "")
        layout.addWidget(QLabel(
            f"<b>{len(optional_mods)} optional mod(s)</b> are not installed by "
            f"default. Check any you want to include:{hint}"))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        info_icon = self.style().standardIcon(QStyle.SP_MessageBoxInformation)
        for m in optional_mods:
            cb = QCheckBox(f"{m.get('name', '?')}  (v{m.get('version', '?')})")
            cb._mod = m
            cb._key = collection_mod_key(m)
            cb.setChecked(cb._key in preselected)   # restore prior choice
            self._checks.append(cb)

            msg = collection_mod_message(m)
            if msg:
                # Row = [checkbox] … [ⓘ]; the author's note shows on hover.
                row = QWidget()
                row_l = QHBoxLayout(row)
                row_l.setContentsMargins(0, 0, 0, 0)
                tip = ('<div style="max-width:380px; white-space:pre-wrap">'
                       + _html.escape(msg) + '</div>')
                cb.setToolTip(tip)                  # hovering the name shows it too
                row_l.addWidget(cb)
                row_l.addStretch()
                badge = QLabel()
                badge.setPixmap(info_icon.pixmap(16, 16))
                badge.setToolTip(tip)
                badge.setCursor(Qt.WhatsThisCursor)
                row_l.addWidget(badge)
                inner_layout.addWidget(row)
            else:
                inner_layout.addWidget(cb)
        inner_layout.addStretch()
        scroll.setWidget(inner)
        layout.addWidget(scroll, 1)

        if offsite_mods:
            layout.addWidget(QLabel(
                f"<b>{len(offsite_mods)} off-site / manual mod(s)</b> can't be "
                "auto-downloaded. Get these from their pages and drop them in your "
                "downloads folder, then re-run install:"))
            off = QTextEdit()
            off.setReadOnly(True)
            off.setMaximumHeight(120)
            off.setPlainText("\n".join(f"• {m.get('name', '?')}" for m in offsite_mods))
            layout.addWidget(off)

        row = QHBoxLayout()
        self._select_all = QPushButton("Select all")
        self._select_all.clicked.connect(lambda: [c.setChecked(True) for c in self._checks])
        self._select_none = QPushButton("Select none")
        self._select_none.clicked.connect(lambda: [c.setChecked(False) for c in self._checks])
        row.addWidget(self._select_all)
        row.addWidget(self._select_none)
        row.addStretch()
        layout.addLayout(row)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

    def selected_mods(self):
        return [c._mod for c in self._checks if c.isChecked()]

    def all_states(self):
        """{mod_key: checked} for EVERY optional shown -- so de-selections are
        remembered too, not just additions."""
        return {c._key: c.isChecked() for c in self._checks}


class InstallTab(QWidget):
    """Installation tab widget."""

    paths_changed = Signal(str, str, str)   # collection, staging, game_data(blank)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.install_thread = None
        self.collection_path = ""
        self.downloads_path = ""
        self.game_path = ""
        
        # Load configuration
        self.config = self._load_config()

        self._paths_loaded = False   # guard so we don't save empty paths during build
        self.setup_ui()
        self.setup_connections()
        self._restore_paths()        # bring back last session's collection/downloads/staging

    def _config_file(self):
        from pathlib import Path as _P
        return _P(__file__).parent.parent / "config.json"

    def _save_paths(self):
        """Persist the chosen collection/downloads/staging to ui_preferences so they
        survive a restart (the path pickers were previously session-only)."""
        if not getattr(self, "_paths_loaded", False):
            return
        try:
            p = self._config_file()
            cfg = {}
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            ui = cfg.setdefault("ui_preferences", {})
            ui["last_collection_file"] = self.collection_path or ""
            ui["last_downloads_folder"] = self.downloads_path or ""
            ui["last_staging_folder"] = self.game_path or ""
            tmp = str(p) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            os.replace(tmp, str(p))
        except Exception as e:
            self.log_message("DEBUG", f"Could not save paths: {e}")

    def _restore_paths(self):
        try:
            ui = (self._load_config().get("ui_preferences") or {})
            c, d, s = (ui.get("last_collection_file") or "",
                       ui.get("last_downloads_folder") or "",
                       ui.get("last_staging_folder") or "")
            if c and os.path.exists(c):
                self.collection_path = c
                self.collection_path_edit.setText(c)
            if d and os.path.isdir(d):
                self.downloads_path = d
                self.downloads_path_edit.setText(d)
            if s and os.path.isdir(s):
                self.game_path = s
                self.game_path_edit.setText(s)
        except Exception as e:
            self.log_message("DEBUG", f"Could not restore paths: {e}")
        self._paths_loaded = True
        self.update_start_button_state()

    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from config.json."""
        default_config = {
            "installation": {
                "max_concurrent_installs": 4,
                "enable_parallel_installation": True,
                "thread_safety_enabled": True,
                "installation_timeout_seconds": 600
            }
        }
        
        try:
            config_path = Path(__file__).parent.parent / "config.json"
            if config_path.exists():
                with open(config_path, 'r', encoding='utf-8') as f:
                    loaded_config = json.load(f)
                    # Merge with defaults to ensure all required keys exist
                    if "installation" not in loaded_config:
                        loaded_config["installation"] = default_config["installation"]
                    return loaded_config
        except Exception as e:
            print(f"WARNING: Failed to load config: {e}")  # Use print since log_message might not be available yet
        
        # Return default configuration
        return default_config
    
    def setup_ui(self):
        """Setup the user interface."""
        layout = QVBoxLayout(self)

        # Paths come from the global Game + Collection header (shared store); these
        # fields show what's wired up and let you override any of them.
        config_group = QGroupBox("Paths (from the Game/Collection header — override if needed)")
        config_layout = QGridLayout(config_group)
        
        # Collection file selection
        config_layout.addWidget(QLabel("Collection File:"), 0, 0)
        self.collection_path_edit = QLabel("No file selected")
        self.collection_path_edit.setStyleSheet("QLabel { border: 1px solid gray; padding: 5px; }")
        config_layout.addWidget(self.collection_path_edit, 0, 1)
        self.browse_collection_btn = QPushButton("Browse...")
        config_layout.addWidget(self.browse_collection_btn, 0, 2)
        
        # Downloads folder
        config_layout.addWidget(QLabel("Downloads Folder:"), 1, 0)
        self.downloads_path_edit = QLabel("No folder selected")
        self.downloads_path_edit.setStyleSheet("QLabel { border: 1px solid gray; padding: 5px; }")
        config_layout.addWidget(self.downloads_path_edit, 1, 1)
        self.browse_downloads_btn = QPushButton("Browse...")
        config_layout.addWidget(self.browse_downloads_btn, 1, 2)
        
        # Mod staging folder
        config_layout.addWidget(QLabel("Mod Staging Folder:"), 2, 0)
        self.game_path_edit = QLabel("No folder selected")
        self.game_path_edit.setStyleSheet("QLabel { border: 1px solid gray; padding: 5px; }")
        config_layout.addWidget(self.game_path_edit, 2, 1)
        self.browse_game_btn = QPushButton("Browse...")
        config_layout.addWidget(self.browse_game_btn, 2, 2)
        
        # Auto-detect button for Vortex integration
        self.auto_detect_btn = QPushButton("Auto-Detect from Vortex")
        self.auto_detect_btn.setToolTip("Automatically detect download and mod paths from Vortex installation")
        config_layout.addWidget(self.auto_detect_btn, 3, 1, 1, 2)  # Span 2 columns
        
        layout.addWidget(config_group)
        
        # Options section
        options_group = QGroupBox("Installation Options")
        options_layout = QGridLayout(options_group)
        
        # First row - Basic options
        self.overwrite_existing = QCheckBox("Overwrite existing files")
        self.overwrite_existing.setChecked(True)
        options_layout.addWidget(self.overwrite_existing, 0, 0)
        
        self.create_backup = QCheckBox("Create backup of existing files")
        options_layout.addWidget(self.create_backup, 0, 1)
        
        self.skip_optional = QCheckBox("Skip optional mods")
        options_layout.addWidget(self.skip_optional, 0, 2)
        
        # Second row - Parallel installation options
        install_config = self.config.get("installation", {})
        
        self.parallel_install = QCheckBox("Enable parallel installation")
        self.parallel_install.setChecked(install_config.get("enable_parallel_installation", True))
        self.parallel_install.setToolTip("Install multiple mods simultaneously for faster processing")
        options_layout.addWidget(self.parallel_install, 1, 0)
        
        options_layout.addWidget(QLabel("Max concurrent installs:"), 1, 1)
        self.max_workers_spinbox = QSpinBox()
        self.max_workers_spinbox.setMinimum(1)
        self.max_workers_spinbox.setMaximum(48)
        _saved_workers = (self.config.get("ui_preferences") or {}).get("max_concurrent_installs")
        self.max_workers_spinbox.setValue(
            int(_saved_workers) if _saved_workers
            else install_config.get("max_concurrent_installs", 4))
        self.max_workers_spinbox.setToolTip("Number of mods to install simultaneously (1-48). "
                                            "External 7-Zip keeps memory bounded, so high values "
                                            "are safe on many-core machines.")
        options_layout.addWidget(self.max_workers_spinbox, 1, 2)
        
        layout.addWidget(options_group)

        # One-click pipeline: Install -> Baseline -> Link -> Deploy/Finalize.
        oneclick = QGroupBox("One-Click Setup")
        oc = QVBoxLayout(oneclick)
        self.oc_install = QCheckBox("Install"); self.oc_install.setChecked(True)
        self.oc_baseline = QCheckBox("Checksum baseline (slow)")
        self.oc_link = QCheckBox("Link to Vortex"); self.oc_link.setChecked(True)
        self.oc_deploy = QCheckBox("Deploy + finalize"); self.oc_deploy.setChecked(True)
        self.oc_force = QCheckBox("Force (override Vortex version/schema warnings)")
        ocrow = QHBoxLayout()
        for cb in (self.oc_install, self.oc_baseline, self.oc_link, self.oc_deploy, self.oc_force):
            ocrow.addWidget(cb)
        ocrow.addStretch(1)
        oc.addLayout(ocrow)
        self.oneclick_btn = QPushButton("▶  Run Everything  (Install → Link → Deploy)")
        self.oneclick_btn.setStyleSheet("QPushButton { font-weight: bold; padding: 8px; }")
        self.oneclick_btn.clicked.connect(self.run_pipeline)
        oc.addWidget(self.oneclick_btn)
        self.oc_status = QLabel("")
        oc.addWidget(self.oc_status)
        layout.addWidget(oneclick)

        # Local-state / integrity panel: build the checksum baseline, verify
        # staging against it, and repair broken mods from their source download.
        from gui.ledger_panel import LedgerPanel
        self.ledger_panel = LedgerPanel(
            lambda: (self.game_path, self.downloads_path, self.collection_path))
        layout.addWidget(self.ledger_panel)
        
        # Control buttons
        control_layout = QHBoxLayout()
        self.start_install_btn = QPushButton("Start Installation")
        self.start_install_btn.setEnabled(False)
        control_layout.addWidget(self.start_install_btn)
        
        self.cancel_install_btn = QPushButton("Cancel Installation")
        self.cancel_install_btn.setEnabled(False)
        control_layout.addWidget(self.cancel_install_btn)

        self.link_vortex_btn = QPushButton("Link to Vortex")
        self.link_vortex_btn.setToolTip(
            "Register the installed mods + collection in Vortex's database so it "
            "shows them as installed/enabled. Close Vortex completely first.")
        self.link_vortex_btn.setEnabled(False)
        control_layout.addWidget(self.link_vortex_btn)

        control_layout.addStretch()
        self.remove_install_btn = QPushButton("Remove Installation")
        self.remove_install_btn.setStyleSheet("QPushButton { color: #b00; }")
        self.remove_install_btn.setToolTip(
            "DESTRUCTIVE: delete every staged mod folder for this collection, "
            "optionally un-deploy from the game folder (update vortex.deployment.json), "
            "and reset the ledger to 'downloaded, waiting to install'. Keeps your "
            "downloads and the collection. Vortex must be CLOSED.")
        control_layout.addWidget(self.remove_install_btn)
        layout.addLayout(control_layout)
        
        # Progress section
        progress_group = QGroupBox("Installation Progress")
        progress_layout = QVBoxLayout(progress_group)
        
        # Overall progress
        self.overall_progress = QProgressBar()
        self.overall_progress.setVisible(False)
        progress_layout.addWidget(self.overall_progress)
        
        self.progress_label = QLabel("Ready to install")
        progress_layout.addWidget(self.progress_label)
        
        # Live install monitor: one row per active worker thread showing what it's
        # doing right now (extracting -> staging -> verifying), plus an aggregate
        # header (active/max threads, done/failed, files/sec). Full width.
        from gui.install_monitor import InstallMonitorWidget
        mod_status_group = QGroupBox("Mod Installation Status")
        mod_status_layout = QVBoxLayout(mod_status_group)
        self.install_monitor = InstallMonitorWidget(
            getattr(self, "max_workers_spinbox", None).value()
            if getattr(self, "max_workers_spinbox", None) else 4)
        mod_status_layout.addWidget(self.install_monitor)
        progress_layout.addWidget(mod_status_group, 1)
        # Stretch factor 1: the progress group absorbs all extra vertical space
        # when the window grows, while Paths/Options stay at their natural height.
        layout.addWidget(progress_group, 1)

        # The completed-list + log pane were removed from the UI, but plenty of
        # code paths still write to them (on_mod_installed history, log_message,
        # clear_log). Keep the objects alive off-screen so nothing AttributeErrors.
        self.mod_status_list = QListWidget()
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.document().setMaximumBlockCount(1000)
        self.clear_log_btn = QPushButton("Clear Log")
    
    def setup_connections(self):
        """Setup signal connections."""
        self.browse_collection_btn.clicked.connect(self.browse_collection_file)
        self.browse_downloads_btn.clicked.connect(self.browse_downloads_folder)
        self.browse_game_btn.clicked.connect(self.browse_game_folder)
        self.auto_detect_btn.clicked.connect(self.auto_detect_vortex_paths)
        self.start_install_btn.clicked.connect(self.start_installation)
        self.cancel_install_btn.clicked.connect(self.cancel_installation)
        self.clear_log_btn.clicked.connect(self.clear_log)
        self.link_vortex_btn.clicked.connect(self.link_to_vortex)
        self.remove_install_btn.clicked.connect(self.remove_installation)
        # Live concurrency: moving the spinbox retargets a running install (no restart).
        self.max_workers_spinbox.valueChanged.connect(self._on_workers_changed)
        # Subscribe to the shared store: the global Game/Collection header fills
        # these path fields automatically.
        from gui.session_paths import session_paths
        self._session = session_paths()
        self._session.changed.connect(self._sync_from_session)
        self._sync_from_session()

    def _sync_from_session(self):
        """Pull collection/downloads/staging from the shared store into the fields.

        update_start_button_state re-publishes, but session.update() is a no-op
        when values are unchanged, so there's no feedback loop.
        """
        s = self._session
        if s.collection and self.collection_path != s.collection:
            self.collection_path = s.collection
            self.collection_path_edit.setText(s.collection)
        if s.downloads and self.downloads_path != s.downloads:
            self.downloads_path = s.downloads
            self.downloads_path_edit.setText(s.downloads)
        if s.staging and self.game_path != s.staging:
            self.game_path = s.staging
            self.game_path_edit.setText(s.staging)
        self.update_start_button_state()

    def _on_workers_changed(self, value: int):
        """Apply a new concurrency to the running install immediately, if any, and
        persist it so the choice survives a restart (used for install AND reset)."""
        if self.install_thread and self.install_thread.isRunning():
            self.install_thread.set_concurrency(value)
            self.log_message("INFO", f"Install concurrency changed to {value} (live)")
        if getattr(self, "install_monitor", None):
            self.install_monitor.set_max_threads(value)
        self._save_ui_pref("max_concurrent_installs", value)

    def _save_ui_pref(self, key: str, value):
        """Persist a single ui_preferences key to config.json (atomic write)."""
        try:
            p = self._config_file()
            cfg = {}
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            cfg.setdefault("ui_preferences", {})[key] = value
            tmp = str(p) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            os.replace(tmp, str(p))
        except Exception as e:
            self.log_message("DEBUG", f"Could not save {key}: {e}")

    def browse_collection_file(self):
        """Browse for collection JSON file."""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Collection File",
            "",
            "JSON files (*.json);;All files (*.*)"
        )
        
        if file_path:
            self.collection_path = file_path
            self.collection_path_edit.setText(file_path)
            self.update_start_button_state()
            self.log_message("INFO", f"Selected collection: {file_path}")
    
    def browse_downloads_folder(self):
        """Browse for downloads folder."""
        folder_path = QFileDialog.getExistingDirectory(
            self,
            "Select Downloads Folder",
            ""
        )
        
        if folder_path:
            self.downloads_path = folder_path
            self.downloads_path_edit.setText(folder_path)
            self.update_start_button_state()
            self.log_message("INFO", f"Selected downloads folder: {folder_path}")
    
    def browse_game_folder(self):
        """Browse for mod staging folder."""
        folder_path = QFileDialog.getExistingDirectory(
            self,
            "Select Mod Staging Folder",
            ""
        )
        
        if folder_path:
            self.game_path = folder_path
            self.game_path_edit.setText(folder_path)
            self.update_start_button_state()
            self.log_message("INFO", f"Selected game folder: {folder_path}")
    
    def update_start_button_state(self):
        """Update the state of the start installation button."""
        can_start = bool(
            self.collection_path and os.path.exists(self.collection_path) and
            self.downloads_path and os.path.exists(self.downloads_path) and
            self.game_path and os.path.exists(self.game_path) and
            not (self.install_thread and self.install_thread.isRunning())
        )
        self.start_install_btn.setEnabled(can_start)
        # Linking needs the same three paths; available whether or not an install ran.
        self.link_vortex_btn.setEnabled(can_start)
        # Share the picked paths with the Deploy & Play tab.
        self.paths_changed.emit(self.collection_path or "", self.game_path or "", "")
        # Publish to the shared store (single source of truth for all tabs).
        if getattr(self, "_paths_loaded", False):
            from gui.session_paths import session_paths
            session_paths().update(collection=self.collection_path,
                                   downloads=self.downloads_path,
                                   staging=self.game_path)
        # Persist the picks so they come back next session.
        self._save_paths()

    def _confirm_if_elevated(self, action: str) -> bool:
        """Warn when running elevated; return False if the user cancels.

        Files created by an Administrator run are owned by the Administrators group,
        so a later non-elevated run can't delete them (WinError 5) -- which is what
        breaks Remove Installation. Keep every run at the same (ideally non-admin)
        elevation."""
        try:
            from utils.platform_admin import is_elevated
            if not is_elevated():
                return True
        except Exception:
            return True
        ans = QMessageBox.warning(
            self, "Running as Administrator",
            f"This app is running elevated (Administrator). If you {action} now, the "
            f"files it stages are tied to the Administrator account — a normal "
            f"(non-elevated) run may then be unable to delete them, which breaks "
            f"Remove Installation.\n\nRecommended: cancel, relaunch WITHOUT admin, and "
            f"keep every run at the same level.\n\nProceed anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        return ans == QMessageBox.StandardButton.Yes

    def remove_installation(self):
        """Nuke this collection's staging install: delete every staged mod folder,
        optionally un-deploy from the game folder, and reset the ledger to
        'downloaded, waiting to install'. Downloads + the collection are preserved.
        Reuses the shared maintenance ops (same as the Deploy tab's Reset)."""
        staging = self.game_path
        if not staging or not os.path.isdir(staging):
            QMessageBox.warning(self, "Remove Installation",
                                "Set the Mod Staging Folder first.")
            return
        # Never reset while an install is running: the install would re-create every
        # folder the reset deletes, so the reset silently "succeeds" but nothing sticks.
        if self.install_thread and self.install_thread.isRunning():
            QMessageBox.warning(
                self, "Remove Installation",
                "An installation is still running. Wait for it to finish first — "
                "otherwise the install re-creates whatever the reset deletes.")
            return
        if getattr(self, "_remove_thread", None) and self._remove_thread.isRunning():
            return
        game_data = getattr(getattr(self, "_session", None), "game_data", "") or ""
        can_purge = bool(game_data and os.path.isdir(game_data))

        # Single confirm whose buttons ARE the actions: pick what to remove, or cancel.
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Remove Installation")
        box.setText("Remove this collection's installation?")
        info = ("DESTRUCTIVE: deletes every staged mod folder and resets the ledger to "
                "'downloaded, waiting to install'.\n"
                "PRESERVED: your downloads, endorsements, and the collection.\n"
                "Vortex must be CLOSED.\n\n")
        if can_purge:
            info += ("• Staging and Deployed — un-deploy the game folder (remove hardlinks "
                     "+ update vortex.deployment.json), then wipe staging.\n"
                     "• Staging Only — wipe staging, leave the game folder as-is.")
        else:
            info += "• Staging Only — wipe staging (no deployed game folder detected)."
        box.setInformativeText(info)

        both_btn = (box.addButton("Staging and Deployed", QMessageBox.ButtonRole.DestructiveRole)
                    if can_purge else None)
        box.addButton("Staging Only", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_btn)
        box.setEscapeButton(cancel_btn)
        box.exec()

        clicked = box.clickedButton()
        if clicked is None or clicked is cancel_btn:
            return
        also_purge = clicked is both_btn

        from gui.deploy_tab import MaintenanceWorkerThread
        self.remove_install_btn.setEnabled(False)
        self.start_install_btn.setEnabled(False)
        self.overall_progress.setVisible(True)
        self.overall_progress.setRange(0, 0)   # busy indicator until first tick
        self.progress_label.setText("Removing installation…")
        self.log_message("INFO", "Remove Installation started"
                         + (" (with game-folder purge)" if also_purge else ""))
        self._remove_thread = MaintenanceWorkerThread(
            "reset", staging, game_data, purge_deploy=also_purge,
            workers=self.max_workers_spinbox.value())
        self._remove_thread.progress.connect(self._on_remove_progress)
        self._remove_thread.status.connect(lambda m: self.log_message("INFO", m))
        self._remove_thread.done.connect(self._on_remove_done)
        self._remove_thread.failed.connect(self._on_remove_failed)
        self._remove_thread.start()

    def _on_remove_progress(self, done, total, name):
        if total > 0:
            self.overall_progress.setRange(0, total)
            self.overall_progress.setValue(done)
        self.progress_label.setText(f"Removing… {done}/{total}  {name}")

    def _on_remove_done(self, msg):
        self.remove_install_btn.setEnabled(True)
        self.overall_progress.setRange(0, 1)
        self.overall_progress.setValue(1)
        self.progress_label.setText(msg)
        self.log_message("INFO", msg)
        self.update_start_button_state()
        QMessageBox.information(self, "Remove Installation", msg)

    def _on_remove_failed(self, msg):
        self.remove_install_btn.setEnabled(True)
        self.progress_label.setText("Remove failed")
        self.log_message("ERROR", msg)
        self.update_start_button_state()
        QMessageBox.critical(self, "Remove Installation failed", msg)

    def link_to_vortex(self):
        """Register installed mods + the collection into Vortex's DB (two-phase:
        dry-run for a preview + drift check, then apply on confirmation)."""
        if not (self.collection_path and self.downloads_path and self.game_path):
            QMessageBox.warning(self, "Link to Vortex",
                                "Select the collection, downloads, and staging folders first.")
            return
        self.link_vortex_btn.setEnabled(False)
        self.log_message("INFO", "Planning Vortex sync (dry-run)...")
        self._sync_thread = VortexSyncWorkerThread(
            self.collection_path, self.downloads_path, self.game_path, apply=False, force=False)
        self._sync_thread.finished_result.connect(self._on_sync_dryrun)
        self._sync_thread.failed.connect(self._on_sync_error)
        self._sync_thread.busy.connect(self._on_sync_busy)
        self._sync_thread.start()

    def _on_sync_dryrun(self, res):
        p = res.plan
        force = bool(p.violations) or (not res.risk.safe)
        cyc = f" ({p.dropped_cycle_rules} cyclic dropped)" if p.dropped_cycle_rules else ""
        lines = [
            f"This will register {p.mod_count} mods and link the collection in Vortex.",
            "  (projected from the local ledger — no disk guessing)",
            "",
            f"  Downloads to register     : {p.new_downloads}",
            f"  Orphan mods (yours/manual): {p.orphan_count}",
            f"  Conflict rules written    : {p.modrule_count}{cyc}",
            f"  Total DB keys to write    : {p.total_keys}",
            "",
        ]
        if not res.risk.safe:
            lines += ["RISK: " + res.risk.message,
                      "This Vortex version has not been validated -- writing could corrupt "
                      "your Vortex setup. A backup is made first. Proceed anyway?", ""]
        else:
            lines += [f"Vortex check: {res.risk.message}",
                      "Make sure Vortex is fully CLOSED, then proceed.", ""]
        if p.violations:
            lines.append(f"WARNING: {len(p.violations)} schema issue(s) detected.")
        reply = QMessageBox.question(self, "Link to Vortex", "\n".join(lines),
                                     QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            self.link_vortex_btn.setEnabled(True)
            self.log_message("INFO", "Vortex sync cancelled by user.")
            return
        self.log_message("INFO", "Applying Vortex sync...")
        self._sync_thread = VortexSyncWorkerThread(
            self.collection_path, self.downloads_path, self.game_path, apply=True, force=force)
        self._sync_thread.finished_result.connect(self._on_sync_applied)
        self._sync_thread.failed.connect(self._on_sync_error)
        self._sync_thread.busy.connect(self._on_sync_busy)
        self._sync_thread.start()

    def _on_sync_applied(self, res):
        self.link_vortex_btn.setEnabled(True)
        if res.applied:
            self.log_message("INFO", f"Vortex sync applied: {res.plan.mod_count} mods, "
                                     f"{res.keys_written} keys, replaced "
                                     f"{res.replaced_collections} old revision(s), "
                                     f"removed {res.removed_stubs} duplicate stub(s). "
                                     f"Backup: {res.backup_path}")
            replaced = (f"Replaced {res.replaced_collections} old collection revision(s).\n"
                        if res.replaced_collections else "")
            destubbed = (f"Cleared {res.removed_stubs} duplicate 'Never Installed' stub(s).\n"
                         if res.removed_stubs else "")
            QMessageBox.information(
                self, "Linked to Vortex",
                f"Linked {res.plan.mod_count} mods and the collection into Vortex.\n"
                f"{replaced}{destubbed}\n"
                f"Backup of your Vortex DB: {res.backup_path}\n\n"
                f"Now open Vortex and click Deploy Mods.")
        else:
            self.log_message("WARNING", f"Vortex sync not applied: {res.message}")
            QMessageBox.warning(self, "Not applied", res.message)

    def _on_sync_busy(self, msg):
        self.link_vortex_btn.setEnabled(True)
        self.log_message("WARNING", f"Vortex sync blocked: {msg}")
        QMessageBox.warning(self, "Close Vortex", msg)

    def _on_sync_error(self, msg):
        self.link_vortex_btn.setEnabled(True)
        self.log_message("ERROR", f"Vortex sync failed: {msg}")
        QMessageBox.critical(self, "Vortex sync failed", msg)

    def _install_temp_root(self) -> str:
        """The optional install-temp override (blank = system %TEMP%), read fresh
        from src/config.json so a value saved this session is picked up without a
        restart. Shared by the normal install and the one-click pipeline."""
        try:
            return (self._load_config().get("downloads") or {}).get("install_temp_dir") or ""
        except Exception as e:
            self.log_message("DEBUG", f"Could not read install_temp_dir from config: {e}")
            return ""

    def _resolve_game_root(self, game_id: str = "skyrimse") -> str:
        """Best-effort game install root (folder holding SkyrimSE.exe) from Vortex's
        discovery, for routing root files (SKSE/ENB). Blank if it can't be read
        (Vortex open, no node, etc.) -- the installer then skips root files safely."""
        try:
            from utils import vortex_db
            db = vortex_db.find_state_db()
            if not db:
                return ""
            for g in vortex_db.read_vortex_games(db):
                if g.get("game") == game_id and g.get("install"):
                    return g["install"]
        except Exception as e:
            self.log_message("DEBUG", f"Could not resolve game root: {e}")
        return ""

    # ----- One-click pipeline ------------------------------------------------ #
    def run_pipeline(self):
        """Run the whole flow (Install -> Baseline -> Link -> Deploy) in order."""
        if getattr(self, "_pipeline", None) and self._pipeline.isRunning():
            return
        from gui.session_paths import session_paths
        from gui.pipeline import PipelineWorker
        s = session_paths()
        paths = {
            "collection": self.collection_path or s.collection,
            "downloads": self.downloads_path or s.downloads,
            "staging": self.game_path or s.staging,
            "game_data": s.game_data,
            "localappdata": s.localappdata,
        }
        need = [k for k in ("collection", "downloads", "staging") if not paths[k]]
        if self.oc_deploy.isChecked() and not paths["game_data"]:
            need.append("game_data")
        if need:
            QMessageBox.information(
                self, "Paths needed",
                "Set these first (Game/Collection header + Deploy tab): "
                + ", ".join(need))
            return
        steps = []
        if self.oc_install.isChecked(): steps.append("Install")
        if self.oc_baseline.isChecked(): steps.append("Checksum baseline")
        if self.oc_link.isChecked(): steps.append("Link")
        if self.oc_deploy.isChecked(): steps.append("Deploy")
        if QMessageBox.question(
                self, "Run Everything",
                "Run, in order:\n  • " + "\n  • ".join(steps) +
                "\n\nLink/Deploy need Vortex CLOSED — the run will wait if it's open.\n"
                "Continue?", QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return

        self.oneclick_btn.setEnabled(False)
        self.start_install_btn.setEnabled(False)
        self.log_message("INFO", "One-click pipeline started.")
        self._pipeline = PipelineWorker(
            paths, workers=self.max_workers_spinbox.value(),
            temp_root=self._install_temp_root(),
            force=self.oc_force.isChecked(),
            do_install=self.oc_install.isChecked(),
            do_baseline=self.oc_baseline.isChecked(),
            do_link=self.oc_link.isChecked(),
            do_deploy=self.oc_deploy.isChecked())
        self._pipeline.phase.connect(self._pipe_phase)
        self._pipeline.progress.connect(self.update_progress)
        self._pipeline.log.connect(self.log_message)
        self._pipeline.waiting_vortex.connect(self._pipe_waiting)
        self._pipeline.failed.connect(self._pipe_failed)
        self._pipeline.finished_ok.connect(self._pipe_done)
        self._pipeline.start()

    def _pipe_phase(self, label, idx, total):
        self.oc_status.setText(f"Phase {idx}/{total}: {label}…")

    def _pipe_waiting(self, waiting):
        if waiting:
            self.oc_status.setText("⏳ Waiting for you to CLOSE Vortex…")

    def _pipe_failed(self, phase, msg):
        self.oneclick_btn.setEnabled(True)
        self.start_install_btn.setEnabled(True)
        self.oc_status.setText(f"✗ Failed at: {phase}")
        QMessageBox.warning(self, f"Pipeline stopped — {phase}", msg)

    def _pipe_done(self, summary):
        self.oneclick_btn.setEnabled(True)
        self.start_install_btn.setEnabled(True)
        self.oc_status.setText("✓ Done.")
        if hasattr(self, "ledger_panel"):
            self.ledger_panel.refresh_status()
        QMessageBox.information(self, "All done", summary or "Pipeline complete.")

    def start_installation(self):
        """Start the installation process."""
        # Log debug information
        if self.install_thread and self.install_thread.isRunning():
            self.log_message("WARNING", "Installation already running, returning")
            return
        if getattr(self, "_remove_thread", None) and self._remove_thread.isRunning():
            self.log_message("WARNING", "Remove Installation is running; wait for it to finish")
            return
        if not self._confirm_if_elevated("install"):
            return

        # Clear previous results + reset the running mod/file counters.
        self.mod_status_list.clear()
        self.install_monitor.reset()
        self._mods_done = self._mods_total = self._files_done = 0
        self.overall_progress.setRange(0, 0)   # busy indicator during pre-scan; update_progress sets 0..total
        self.overall_progress.setVisible(True)

        # Optionals are opt-in: install required only, unless the user picks some
        # (or "Skip optional mods" is checked, which skips all without asking).
        install_collection_path = self.collection_path
        try:
            with open(self.collection_path, "r", encoding="utf-8") as fh:
                cdata = json.load(fh)
            mods = cdata.get("mods", [])

            def _is_offsite(m):
                s = m.get("source", {})
                return (s.get("type") or "").lower() != "nexus" or not s.get("modId")

            optional = [m for m in mods if m.get("optional") and not _is_offsite(m)]
            offsite = [m for m in mods if _is_offsite(m)]
            selected_optional = []
            if not self.skip_optional.isChecked() and (optional or offsite):
                # Restore the user's prior optional choices for THIS collection.
                coll_key = collection_key_for(cdata)
                preselected = self._load_optional_selection(coll_key)
                dlg = OptionalSelectionDialog(optional, offsite, self, preselected=preselected)
                if dlg.exec() != QDialog.Accepted:
                    self.log_message("INFO", "Installation cancelled at optionals dialog.")
                    self.overall_progress.setVisible(False)
                    return
                selected_optional = dlg.selected_mods()
                # Remember the choices (checked AND unchecked) for next time.
                self._save_optional_selection(coll_key, dlg.all_states())
            sel_ids = {id(m) for m in selected_optional}
            filtered = [m for m in mods if (not m.get("optional")) or (id(m) in sel_ids)]
            if len(filtered) != len(mods):
                cdata = dict(cdata)
                cdata["mods"] = filtered
                import tempfile
                install_collection_path = os.path.join(
                    tempfile.gettempdir(), "nxd_install_collection.json")
                with open(install_collection_path, "w", encoding="utf-8") as fh:
                    json.dump(cdata, fh)
                self.log_message("INFO", f"Installing {len(filtered)}/{len(mods)} mods "
                                         f"({len(mods) - len(filtered)} optional skipped).")
        except Exception as e:
            self.log_message("WARNING", f"Optionals filtering skipped: {e}")

        # Create and start installation thread
        use_parallel = self.parallel_install.isChecked()
        max_workers = self.max_workers_spinbox.value()
        
        self.log_message("DEBUG", f"Creating InstallWorkerThread with parallel={use_parallel}, workers={max_workers}")
        self.log_message("DEBUG", f"Paths - collection: {self.collection_path}")
        self.log_message("DEBUG", f"Paths - downloads: {self.downloads_path}")
        self.log_message("DEBUG", f"Paths - staging: {self.game_path}")
        
        temp_root = self._install_temp_root()
        if temp_root:
            self.log_message("INFO", f"Using override install temp dir: {temp_root}")

        game_root = self._resolve_game_root()
        if game_root:
            self.log_message("INFO", f"Game root for root-file placement: {game_root}")
        else:
            self.log_message("WARNING", "Game root unknown -- root files (SKSE/ENB) "
                             "will be skipped rather than misplaced into Data.")

        self.install_thread = InstallWorkerThread(
            install_collection_path,
            self.downloads_path,
            self.game_path,
            use_parallel,
            max_workers,
            temp_root,
            game_root
        )
        
        # Connect signals
        self.log_message("DEBUG", "Connecting signals")
        self.install_thread.progress_updated.connect(self.update_progress)
        self.install_thread.installation_complete.connect(self.on_mod_installed)
        self.install_thread.log_message.connect(self.log_message)
        self.install_thread.installation_finished.connect(self.on_installation_finished)
        self.install_thread.active_installs_updated.connect(self.install_monitor.update_view)
        
        self.log_message("DEBUG", "Starting thread")
        self.install_thread.start()
        
        # Update UI state
        self.start_install_btn.setEnabled(False)
        self.cancel_install_btn.setEnabled(True)
        
        self.log_message("DEBUG", "Installation process started")
        self.log_message("INFO", "Starting installation process...")
    
    def _load_optional_selection(self, collection_key: str) -> set:
        """Set of mod keys the user previously opted into for this collection."""
        try:
            if not self.game_path:
                return set()
            from utils import local_state
            led = local_state.get_ledger(local_state.db_path_for(self.game_path))
            return {k for k, sel in led.get_collection_options(collection_key).items() if sel}
        except Exception as e:
            self.log_message("DEBUG", f"optional-selection load skipped: {e}")
            return set()

    def _save_optional_selection(self, collection_key: str, states: dict) -> None:
        """Persist the user's optional choices (checked + unchecked) for re-use."""
        try:
            if not self.game_path:
                return
            from utils import local_state
            led = local_state.get_ledger(local_state.db_path_for(self.game_path))
            led.set_collection_options(collection_key, states)
            led.flush()
            chosen = sum(1 for v in states.values() if v)
            self.log_message("INFO", f"Saved {chosen}/{len(states)} optional selections "
                                     f"for this collection.")
        except Exception as e:
            self.log_message("DEBUG", f"optional-selection save skipped: {e}")

    def cancel_installation(self):
        """Cancel the current installation."""
        if self.install_thread and self.install_thread.isRunning():
            self.install_thread.cancel()
            self.log_message("WARNING", "Installation cancelled by user")
            
            # Update UI state
            self.cancel_install_btn.setEnabled(False)
            self.progress_label.setText("Cancelling...")
    
    def update_progress(self, current: int, total: int, mod_name: str):
        """Update installation progress (bar tracks mods; label adds file totals)."""
        self._mods_done, self._mods_total = current, total
        if total > 0:
            # Drive the bar with raw counts, not a 0-100 percentage: setRange here
            # makes it self-correcting even if a prior op (e.g. Remove Installation's
            # busy indicator) left the range at 0-0/0-1, which would otherwise clamp
            # setValue to a false 100%.
            self.overall_progress.setRange(0, total)
            self.overall_progress.setValue(current)
        self._refresh_overall_label(mod_name if current < total else "")

    def _refresh_overall_label(self, mod_name: str = ""):
        """Overall status: mods completed + total files installed so far."""
        done, total = getattr(self, "_mods_done", 0), getattr(self, "_mods_total", 0)
        files = getattr(self, "_files_done", 0)
        if total and done < total:
            self.progress_label.setText(
                f"Installing {done}/{total} mods — {files:,} files installed"
                + (f"  ({mod_name})" if mod_name else ""))
        elif total:
            self.progress_label.setText(
                f"Installed {done}/{total} mods — {files:,} files total")

    def on_mod_installed(self, mod_name: str, success: bool, message: str, file_count: int = 0):
        """Handle individual mod completion: one MOD-level row + running file total."""
        self._files_done = getattr(self, "_files_done", 0) + max(0, file_count)
        item = QListWidgetItem(f"{mod_name}: {message}")
        item.setBackground(Qt.green if success else Qt.red)
        self.mod_status_list.addItem(item)
        self.mod_status_list.scrollToBottom()
        self._refresh_overall_label()
    
    def on_installation_finished(self, results: List[InstallationResult]):
        """Handle completion of entire installation process."""
        # Update UI state
        self.start_install_btn.setEnabled(True)
        self.cancel_install_btn.setEnabled(False)
        self.overall_progress.setVisible(False)
        
        # Show summary
        successful = sum(1 for r in results if r.status == InstallResult.SUCCESS)
        failed = sum(1 for r in results if r.status == InstallResult.FAILED)
        skipped = sum(1 for r in results if r.status == InstallResult.SKIPPED)
        total_files = sum(len(getattr(r, "installed_files", []) or []) for r in results)

        self.progress_label.setText(
            f"Installation finished: {successful} successful, {failed} failed, "
            f"{skipped} skipped — {total_files:,} files installed")
        
        # Show completion dialog
        if failed == 0:
            if skipped > 0:
                QMessageBox.information(
                    self,
                    "Installation Complete",
                    f"Installation completed successfully!\n\n"
                    f"• {successful} mods installed\n"
                    f"• {skipped} mods already installed (skipped)"
                )
            else:
                QMessageBox.information(
                    self,
                    "Installation Complete",
                    f"Successfully installed all {successful} mods!"
                )
        else:
            summary_parts = [f"{successful} successful", f"{failed} failed"]
            if skipped > 0:
                summary_parts.append(f"{skipped} skipped")
            
            QMessageBox.warning(
                self,
                "Installation Complete with Errors",
                f"Installation finished with {', '.join(summary_parts)} installations.\n"
                "Check the log for details."
            )
    
    def log_message(self, level: str, message: str):
        """Add a message to the log."""
        # Color code by log level
        color_map = {
            "DEBUG": "gray",
            "INFO": "black",
            "WARNING": "orange",
            "ERROR": "red"
        }
        
        color = color_map.get(level, "black")
        formatted_message = f'<span style="color: {color};">[{level}] {message}</span>'
        
        self.log_text.append(formatted_message)
        self.log_text.verticalScrollBar().setValue(self.log_text.verticalScrollBar().maximum())
    
    def clear_log(self):
        """Clear the installation log."""
        self.log_text.clear()
    
    def set_downloads_path(self, path: str):
        """Set the downloads path from external source."""
        if path and os.path.exists(path):
            self.downloads_path = path
            self.downloads_path_edit.setText(path)
            self.update_start_button_state()
    
    def set_game_path(self, path: str):
        """Set the game path from external source."""
        if path and os.path.exists(path):
            self.game_path = path
            self.game_path_edit.setText(path)
            self.update_start_button_state()
    
    def auto_detect_vortex_paths(self):
        """Read Vortex's mod-managed games from its DB (state.v2), let the user pick
        one, and fill the Downloads + Mod Staging pickers from that game's configured
        paths (and the newest collection.json found in staging, if none is chosen)."""
        try:
            from gui.vortex_detect import pick_game
            domain = self._extract_game_domain_from_collection()
            g = pick_game(self, prefer_domain=domain,
                          prompt="Pick the game to set up (fills Downloads + Mod Staging):")
            if not g:
                return   # helper already explained why (not found / locked / cancel)

            updates = []
            if g.get('downloads'):
                self.downloads_path = g['downloads']
                self.downloads_path_edit.setText(g['downloads'])
                updates.append(f"Downloads: {g['downloads']}")
            if g.get('staging'):
                self.game_path = g['staging']
                self.game_path_edit.setText(g['staging'])
                updates.append(f"Mod Staging: {g['staging']}")
            # If no collection is picked yet, default to the newest one in staging.
            if (not self.collection_path) and g.get('staging') and os.path.isdir(g['staging']):
                import glob
                cjs = glob.glob(os.path.join(g['staging'], '*', 'collection.json'))
                if cjs:
                    newest = max(cjs, key=os.path.getmtime)
                    self.collection_path = newest
                    self.collection_path_edit.setText(newest)
                    updates.append(f"Collection: {os.path.basename(os.path.dirname(newest))}")

            self.update_start_button_state()
            if updates:
                self.log_message("INFO", f"Auto-detected Vortex paths for {g['game']}")
                QMessageBox.information(self, "Auto-Detect",
                    f"Set up for {g['game']}:\n\n" + "\n".join(updates))
            else:
                QMessageBox.warning(self, "Paths Not Found",
                    f"{g['game']} is discovered but has no configured staging/downloads paths.")
        except Exception as e:
            self.log_message("ERROR", f"Auto-detection failed: {e}")
            QMessageBox.critical(self, "Auto-Detection Error", str(e))

    def _extract_game_domain_from_collection(self) -> Optional[str]:
        """Extract game domain from the selected collection file."""
        if not self.collection_path or not os.path.exists(self.collection_path):
            return None
        
        try:
            with open(self.collection_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            info = data.get('info', {})
            domain = info.get('domainName')
            
            if domain:
                self.log_message("INFO", f"Extracted game domain from collection: {domain}")
                return domain
        
        except Exception as e:
            self.log_message("WARNING", f"Could not extract game domain from collection: {e}")
        
        return None
    
    def _show_game_selection_dialog(self, vortex_reader) -> Optional[str]:
        """Show dialog for user to select a game from Vortex managed games."""
        try:
            managed_games = vortex_reader.list_managed_games()
            if not managed_games:
                QMessageBox.information(
                    self,
                    "No Games Found",
                    "No games found in Vortex configuration.\n\n"
                    "Please ensure Vortex is installed and has discovered at least one game."
                )
                return None
            
            game_names = [f"{game.game_name} ({game.game_id})" for game in managed_games]
            
            selected, ok = QInputDialog.getItem(
                self,
                "Select Game",
                "Select the game to configure paths for:",
                game_names,
                0,
                False
            )
            
            if ok and selected:
                # Extract game_id from the selected item
                game_id = selected.split('(')[-1].rstrip(')')
                return game_id
        
        except Exception as e:
            QMessageBox.critical(
                self,
                "Error",
                f"Error showing game selection dialog:\n\n{str(e)}"
            )
            self.log_message("ERROR", f"Error showing game selection dialog: {e}")
        
        return None