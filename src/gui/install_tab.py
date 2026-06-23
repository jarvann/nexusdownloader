"""
Installation tab for the NexusDownloader GUI.

Provides interface for installing downloaded mods using FOMOD technology
with collection-based automation.
"""

import os
import json
import threading
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFileDialog,
    QTextEdit, QProgressBar, QGroupBox, QGridLayout, QListWidget, QSplitter,
    QListWidgetItem, QMessageBox, QComboBox, QSpinBox, QCheckBox, QInputDialog,
    QDialog, QScrollArea, QDialogButtonBox
)
from PySide6.QtCore import QThread, Signal, Qt, QTimer
from PySide6.QtGui import QFont

# Import installation utilities
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from utils.fomod_installer import create_fomod_installer, create_parallel_fomod_installer, InstallationResult, InstallResult
from utils.unified_logging import get_logger, create_operation_logger
from utils.archive_handler import get_archive_handler
from utils.vortex_config import get_vortex_config_reader


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
    installation_complete = Signal(str, bool, str)  # mod_name, success, message
    log_message = Signal(str, str)  # level, message
    installation_finished = Signal(list)  # List of InstallationResult
    
    def __init__(self, collection_path: str, downloads_path: str, staging_path: str,
                 use_parallel: bool = True, max_workers: int = 4, temp_root: str = ""):
        super().__init__()
        self.collection_path = collection_path
        self.downloads_path = downloads_path
        self.staging_path = staging_path
        self.is_cancelled = False
        self.use_parallel = use_parallel
        self.max_workers = max_workers
        self.temp_root = temp_root
        self._installer = None   # live ref so concurrency can be changed mid-run
    
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
                with create_parallel_fomod_installer(self.staging_path, None, self.max_workers, config, self.temp_root or None) as installer:
                    self._installer = installer   # expose for live concurrency changes
                    # Set up callbacks for real-time progress updates
                    self.log_message.emit("DEBUG", "Setting up callbacks")
                    installer.set_progress_callback(self._on_progress_update)
                    installer.set_installation_callback(self._on_installation_complete)
                    
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
                with create_fomod_installer(self.staging_path, None, self.temp_root or None) as installer:
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
    
    def _on_progress_update(self, current: int, total: int, mod_name: str):
        """Callback for parallel installer progress updates."""
        if not self.is_cancelled:
            self.progress_updated.emit(current, total, mod_name)
    
    def _on_installation_complete(self, mod_name: str, success: bool, message: str):
        """Callback for individual mod installation completion."""
        if not self.is_cancelled:
            self.installation_complete.emit(mod_name, success, message)


class OptionalSelectionDialog(QDialog):
    """Let the user opt IN to optional collection mods, and shows off-site/manual
    mods they must download themselves. Optionals are unchecked by default."""

    def __init__(self, optional_mods, offsite_mods, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose optional mods")
        self.resize(560, 520)
        self._checks = []
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(
            f"<b>{len(optional_mods)} optional mod(s)</b> are not installed by "
            "default. Check any you want to include:"))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        for m in optional_mods:
            cb = QCheckBox(f"{m.get('name', '?')}  (v{m.get('version', '?')})")
            cb.setChecked(False)
            cb._mod = m
            self._checks.append(cb)
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
        
        # Configuration section
        config_group = QGroupBox("Installation Configuration")
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
        self.max_workers_spinbox.setMaximum(16)
        self.max_workers_spinbox.setValue(install_config.get("max_concurrent_installs", 4))
        self.max_workers_spinbox.setToolTip("Number of mods to install simultaneously (1-16)")
        options_layout.addWidget(self.max_workers_spinbox, 1, 2)
        
        layout.addWidget(options_group)
        
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
        
        # Splitter for mod list and log
        splitter = QSplitter(Qt.Horizontal)
        
        # Mod installation status list
        mod_status_group = QGroupBox("Mod Installation Status")
        mod_status_layout = QVBoxLayout(mod_status_group)
        
        self.mod_status_list = QListWidget()
        mod_status_layout.addWidget(self.mod_status_list)
        
        splitter.addWidget(mod_status_group)
        
        # Installation log
        log_group = QGroupBox("Installation Log")
        log_layout = QVBoxLayout(log_group)
        
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        # Note: PySide6 QTextEdit doesn't have setMaximumBlockCount, using document().setMaximumBlockCount instead
        self.log_text.document().setMaximumBlockCount(1000)  # Limit log size
        log_layout.addWidget(self.log_text)
        
        # Log controls
        log_controls = QHBoxLayout()
        self.clear_log_btn = QPushButton("Clear Log")
        log_controls.addWidget(self.clear_log_btn)
        log_controls.addStretch()
        log_layout.addLayout(log_controls)
        
        splitter.addWidget(log_group)
        splitter.setSizes([300, 500])  # Set initial sizes
        
        progress_layout.addWidget(splitter)
        layout.addWidget(progress_group)
    
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
        # Live concurrency: moving the spinbox retargets a running install (no restart).
        self.max_workers_spinbox.valueChanged.connect(self._on_workers_changed)

    def _on_workers_changed(self, value: int):
        """Apply a new concurrency to the running install immediately, if any."""
        if self.install_thread and self.install_thread.isRunning():
            self.install_thread.set_concurrency(value)
            self.log_message("INFO", f"Install concurrency changed to {value} (live)")

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
        # Persist the picks so they come back next session.
        self._save_paths()

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
        lines = [
            f"This will register {p.mod_count} mods and link the collection in Vortex.",
            "",
            f"  New downloads to register : {p.new_downloads}",
            f"  Collection requires-rules : {p.rule_count}",
            f"  Skipped (no files on disk): {p.skipped_no_disk}",
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
                                     f"{res.replaced_collections} old revision(s). "
                                     f"Backup: {res.backup_path}")
            replaced = (f"Replaced {res.replaced_collections} old collection revision(s).\n"
                        if res.replaced_collections else "")
            QMessageBox.information(
                self, "Linked to Vortex",
                f"Linked {res.plan.mod_count} mods and the collection into Vortex.\n"
                f"{replaced}\n"
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

    def start_installation(self):
        """Start the installation process."""
        # Log debug information
        if self.install_thread and self.install_thread.isRunning():
            self.log_message("WARNING", "Installation already running, returning")
            return
        
        # Clear previous results
        self.mod_status_list.clear()
        self.overall_progress.setValue(0)
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
                dlg = OptionalSelectionDialog(optional, offsite, self)
                if dlg.exec() != QDialog.Accepted:
                    self.log_message("INFO", "Installation cancelled at optionals dialog.")
                    self.overall_progress.setVisible(False)
                    return
                selected_optional = dlg.selected_mods()
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
        
        # Pull the optional install-temp override (blank = system %TEMP%). Read it
        # fresh from src/config.json -- the same file the Settings dialog writes to --
        # so a value just saved this session is picked up without a restart.
        temp_root = ""
        try:
            temp_root = (self._load_config().get("downloads") or {}).get("install_temp_dir") or ""
        except Exception as e:
            self.log_message("DEBUG", f"Could not read install_temp_dir from config: {e}")
        if temp_root:
            self.log_message("INFO", f"Using override install temp dir: {temp_root}")

        self.install_thread = InstallWorkerThread(
            install_collection_path,
            self.downloads_path,
            self.game_path,
            use_parallel,
            max_workers,
            temp_root
        )
        
        # Connect signals
        self.log_message("DEBUG", "Connecting signals")
        self.install_thread.progress_updated.connect(self.update_progress)
        self.install_thread.installation_complete.connect(self.on_mod_installed)
        self.install_thread.log_message.connect(self.log_message)
        self.install_thread.installation_finished.connect(self.on_installation_finished)
        
        self.log_message("DEBUG", "Starting thread")
        self.install_thread.start()
        
        # Update UI state
        self.start_install_btn.setEnabled(False)
        self.cancel_install_btn.setEnabled(True)
        
        self.log_message("DEBUG", "Installation process started")
        self.log_message("INFO", "Starting installation process...")
    
    def cancel_installation(self):
        """Cancel the current installation."""
        if self.install_thread and self.install_thread.isRunning():
            self.install_thread.cancel()
            self.log_message("WARNING", "Installation cancelled by user")
            
            # Update UI state
            self.cancel_install_btn.setEnabled(False)
            self.progress_label.setText("Cancelling...")
    
    def update_progress(self, current: int, total: int, mod_name: str):
        """Update installation progress."""
        if total > 0:
            progress_percent = int((current / total) * 100)
            self.overall_progress.setValue(progress_percent)
            
        if current < total:
            self.progress_label.setText(f"Installing {current+1}/{total}: {mod_name}")
        else:
            self.progress_label.setText(f"Installation complete: {current}/{total}")
    
    def on_mod_installed(self, mod_name: str, success: bool, message: str):
        """Handle individual mod installation completion."""
        item = QListWidgetItem(f"{mod_name}: {message}")
        if success:
            item.setBackground(Qt.green)
        else:
            item.setBackground(Qt.red)
        
        self.mod_status_list.addItem(item)
        self.mod_status_list.scrollToBottom()
    
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
        
        self.progress_label.setText(f"Installation finished: {successful} successful, {failed} failed, {skipped} skipped")
        
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
            from utils import vortex_db
            db = vortex_db.find_state_db()
            if not db:
                QMessageBox.warning(self, "Vortex Not Found",
                    "Could not find Vortex's database (state.v2). Make sure Vortex is "
                    "installed and has been run at least once.")
                return
            try:
                games = vortex_db.read_vortex_games(db)
            except Exception as e:
                QMessageBox.warning(self, "Close Vortex",
                    "Couldn't read Vortex's configuration -- its database is locked.\n\n"
                    "Close Vortex completely, then try Auto-Detect again.\n\n"
                    f"Details: {e}")
                return
            if not games:
                QMessageBox.information(self, "No Games Found",
                    "Vortex has no mod-managed games configured.")
                return

            # Pre-select the game matching the loaded collection. Collections use
            # the Nexus DOMAIN (e.g. 'skyrimspecialedition') while Vortex's game id
            # is shorter (e.g. 'skyrimse'), so map the common ones.
            _DOMAIN_TO_GAME = {
                "skyrimspecialedition": "skyrimse", "skyrim": "skyrim",
                "skyrimvr": "skyrimvr", "oblivion": "oblivion",
                "fallout4": "fallout4", "fallout4vr": "fallout4vr",
                "falloutnv": "falloutnv", "starfield": "starfield",
            }
            domain = self._extract_game_domain_from_collection()
            game_id = _DOMAIN_TO_GAME.get((domain or "").lower(), domain)
            labels = [f"{g['game']}" + (f"  [{g['store']}]" if g.get('store') else "")
                      for g in games]
            default_idx = next((i for i, g in enumerate(games) if g['game'] == game_id), 0)
            choice, ok = QInputDialog.getItem(
                self, "Select Game",
                "Pick the game to set up (fills Downloads + Mod Staging):",
                labels, default_idx, False)
            if not ok:
                return
            g = games[labels.index(choice)]

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