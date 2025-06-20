import subprocess
import sys
import os
import json
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QPushButton,
    QHBoxLayout, QLineEdit, QFileDialog, QProgressBar, QMenuBar,
    QMenu, QDialog, QFormLayout, QDialogButtonBox, QMessageBox,
    QComboBox,
)
from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QAction
import re
import time

class DownloadThread(QThread):
    progress_update = Signal(int, int)  # current, total
    error_update = Signal(int)          # error count
    finished_signal = Signal(str)       # signal for completion, now passes exec time message

    def __init__(self, json_path, gamefolder, max_threads=10, endorse_only=False):
        super().__init__()
        self.json_path = json_path
        self.gamefolder = gamefolder
        self.max_threads = max_threads
        self.endorse_only = endorse_only
        self.exec_time_message = ""

    def run(self):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        loadcollection_path = os.path.join(script_dir, "loadcollection.py")
        args = [
            sys.executable, loadcollection_path,
            "--json", self.json_path,
            "--gamefolder", self.gamefolder,
            "--maxthreads", str(self.max_threads)
        ]
        if self.endorse_only:
            args.append("--endorseonly")
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=script_dir,
            text=True
        )
        total = 0
        current = 0
        errors = 0
        for line in process.stdout:
            print(line, end="")
            if line.startswith("PROGRESS:"):
                parts = line.strip().split()
                if len(parts) == 2:
                    nums = parts[1].split("/")
                    if len(nums) == 2:
                        current, total = int(nums[0]), int(nums[1])
                        self.progress_update.emit(current, total)
            elif "Completed download for file" in line:
                match = re.search(r'Completed download for file (\d+) of (\d+)', line)
                if match:
                    current = int(match.group(1))
                    total = int(match.group(2))
                    self.progress_update.emit(current, total)
            elif "ERRORS:" in line:
                match = re.search(r'ERRORS:\s*(\d+)', line)
                if match:
                    errors = int(match.group(1))
                    self.error_update.emit(errors)
            elif line.startswith("Total Execution Time for download:"):
                self.exec_time_message = line.strip()
        process.wait()
        self.finished_signal.emit(self.exec_time_message)

class SettingsDialog(QDialog):
    def __init__(self, config_path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.config_path = config_path

        # Load current config
        self.config = {}
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                self.config = json.load(f)

        layout = QFormLayout(self)

        # API Key
        self.api_key_edit = QLineEdit(self.config.get("AccessControl", {}).get("NexusAPIKey", ""))
        layout.addRow("Nexus API Key:", self.api_key_edit)

        # Downloads Folder
        self.downloads_folder_edit = QLineEdit(self.config.get("VortexSettings", {}).get("DownloadsFolderRoot", ""))
        self.downloads_folder_button = QPushButton("Browse...")
        self.downloads_folder_button.clicked.connect(self.pick_downloads_folder)
        downloads_folder_layout = QHBoxLayout()
        downloads_folder_layout.addWidget(self.downloads_folder_edit)
        downloads_folder_layout.addWidget(self.downloads_folder_button)
        layout.addRow("Downloads Folder:", downloads_folder_layout)

        # Dialog buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def pick_downloads_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Downloads Folder", "")
        if folder:
            self.downloads_folder_edit.setText(folder)

    def accept(self):
        # Save config
        self.config.setdefault("AccessControl", {})["NexusAPIKey"] = self.api_key_edit.text()
        self.config.setdefault("VortexSettings", {})["DownloadsFolderRoot"] = self.downloads_folder_edit.text()
        # Keep ModsFolderRoot if present
        if "ModsFolderRoot" not in self.config.get("VortexSettings", {}):
            self.config["VortexSettings"]["ModsFolderRoot"] = "{NOT_USED}"
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2)
        super().accept()

class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NexusDownloader")
        self.setFixedWidth(500)  # Set a fixed width for the window

        layout = QVBoxLayout()

        # Menu bar
        menubar = QMenuBar(self)
        file_menu = QMenu("File", self)
        settings_action = QAction("Settings", self)
        close_action = QAction("Exit", self)
        settings_action.triggered.connect(self.open_settings)
        close_action.triggered.connect(self.close)
        file_menu.addAction(settings_action)
        file_menu.addAction(close_action)
        menubar.addMenu(file_menu)
        layout.setMenuBar(menubar)

        # File picker row
        file_picker_layout = QHBoxLayout()
        self.file_label = QLabel("Collection JSON File:")
        self.file_label.setFixedWidth(160)
        self.file_path_edit = QLineEdit()
        self.file_path_edit.setReadOnly(True)
        self.file_path_edit.setMinimumWidth(250)
        self.file_path_button = QPushButton("Browse...")
        self.file_path_button.setFixedWidth(80)
        file_picker_layout.addWidget(self.file_label)
        file_picker_layout.addWidget(self.file_path_edit)
        file_picker_layout.addWidget(self.file_path_button)
        layout.addLayout(file_picker_layout)

        # Downloads folder picker row
        downloads_folder_layout = QHBoxLayout()
        self.downloads_folder_label = QLabel("Downloads Folder:")
        self.downloads_folder_label.setFixedWidth(160)
        self.downloads_folder_edit = QLineEdit()
        self.downloads_folder_edit.setReadOnly(True)
        self.downloads_folder_edit.setMinimumWidth(250)
        self.downloads_folder_button = QPushButton("Browse...")
        self.downloads_folder_button.setFixedWidth(80)
        downloads_folder_layout.addWidget(self.downloads_folder_label)
        downloads_folder_layout.addWidget(self.downloads_folder_edit)
        downloads_folder_layout.addWidget(self.downloads_folder_button)
        layout.addLayout(downloads_folder_layout)

        # Max Threads selection with icons
        threads_layout = QHBoxLayout()
        self.threads_label = QLabel("Max Download Threads:")
        self.threads_label.setFixedWidth(160)
        self.threads_combo = QComboBox()
        self.threads_combo.setMinimumWidth(250)
        self.threads_combo.addItem("🐢 Tortoise Mode (5)", 5)
        self.threads_combo.addItem("🚶‍♂️ Stroll Mode (10)", 10)
        self.threads_combo.addItem("🚴‍♀️ Cruise Mode (15)", 15)
        self.threads_combo.addItem("🚗 Turbo Lane (20)", 20)
        self.threads_combo.addItem("🛸 Hyperspeed (25)", 25)
        self.threads_combo.addItem("⚡ Ludicrous Mode (30)", 30)
        self.threads_combo.setCurrentIndex(1)  # Default to 10 - Stroll Mode
        threads_layout.addWidget(self.threads_label)
        threads_layout.addWidget(self.threads_combo)
        layout.addLayout(threads_layout)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimumWidth(400)
        layout.addWidget(self.progress_bar)

        # Errors label
        self.errors_label = QLabel("Current Errors: 0")
        layout.addWidget(self.errors_label)

        # Status label
        self.status_label = QLabel("Ready.")
        layout.addWidget(self.status_label)

        # Start button
        self.download_button = QPushButton("Start Download")
        self.download_button.setMinimumWidth(150)
        layout.addWidget(self.download_button)
        self.download_button.clicked.connect(self.start_download)  # <-- Add this line

        layout.addWidget(QLabel("Welcome to NexusDownloader!"))

        self.setLayout(layout)

        # Path to config.json (assumes it's in the same folder as this script)
        self.config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

        # Make file_path_edit respond to click/double-click
        self.file_path_edit.mousePressEvent = lambda event: self.pick_file()
        self.file_path_edit.mouseDoubleClickEvent = lambda event: self.pick_file()

        # Make downloads_folder_edit respond to click/double-click
        self.downloads_folder_edit.mousePressEvent = lambda event: self.pick_downloads_folder()
        self.downloads_folder_edit.mouseDoubleClickEvent = lambda event: self.pick_downloads_folder()

        # Wire up the browse buttons
        self.file_path_button.clicked.connect(self.pick_file)
        self.downloads_folder_button.clicked.connect(self.pick_downloads_folder)

    def open_settings(self):
        dlg = SettingsDialog(self.config_path, self)
        if dlg.exec():
            # Optionally reload config or update UI if needed
            pass

    def pick_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select collection.json",
            "",
            "JSON Files (*.json);;All Files (*)"
        )
        if file_path:
            self.file_path_edit.setText(file_path)

    def pick_downloads_folder(self):
        # Use the value from the config as the initial directory if available
        initial_dir = ""
        # Try to load from config.json
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    initial_dir = config.get("VortexSettings", {}).get("DownloadsFolderRoot", "")
            except Exception:
                pass
        # If the QLineEdit already has a value, use that as a fallback
        if not initial_dir:
            initial_dir = self.downloads_folder_edit.text()
        folder_path = QFileDialog.getExistingDirectory(
            self,
            "Select Nexus Downloads Folder",
            initial_dir
        )
        if folder_path:
            self.downloads_folder_edit.setText(folder_path)

    def start_download(self):
        json_path = self.file_path_edit.text()
        gamefolder = self.downloads_folder_edit.text()
        if not json_path or not gamefolder:
            return  # Add error handling as needed

        # Get max threads value from combo box
        max_threads = self.threads_combo.currentData()

        # Check for running Vortex.exe processes
        vortex_running = False
        try:
            result = subprocess.run(
                ['tasklist', '/FI', 'IMAGENAME eq Vortex.exe'],
                capture_output=True, text=True
            )
            if "Vortex.exe" in result.stdout:
                vortex_running = True
        except Exception:
            pass

        if vortex_running:
            reply = QMessageBox.question(
                self,
                "Vortex Running",
                "Vortex.exe is currently running. It must be closed before downloading mods.\n\n"
                "Would you like NexusDownloader to close Vortex for you?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Yes
            )
            if reply == QMessageBox.Yes:
                try:
                    subprocess.run(['taskkill', '/F', '/IM', 'Vortex.exe'], capture_output=True)
                    time.sleep(2)
                    result = subprocess.run(
                        ['tasklist', '/FI', 'IMAGENAME eq Vortex.exe'],
                        capture_output=True, text=True
                    )
                    if "Vortex.exe" in result.stdout:
                        QMessageBox.warning(self, "Error", "Vortex.exe could not be closed. Please close it manually.")
                        return
                    QMessageBox.information(self, "Vortex Closed", "All Vortex.exe processes have been closed.")
                except Exception as e:
                    QMessageBox.warning(self, "Error", f"Failed to close Vortex.exe: {e}")
                    return
            elif reply == QMessageBox.No:
                QMessageBox.information(self, "Please Close Vortex", "Please close Vortex manually and try again.")
                return
            else:
                return

        # Start download as normal, passing max_threads
        self.download_thread = DownloadThread(json_path, gamefolder, max_threads=max_threads)
        self.download_thread.progress_update.connect(self.update_progress)
        self.download_thread.error_update.connect(self.update_errors)
        self.download_thread.finished_signal.connect(self.prompt_endorse)
        self.download_thread.start()

    def prompt_endorse(self, exec_time_message):
        msg = "Downloads complete. Would you like to endorse all mods now?"
        if exec_time_message:
            msg = f"{exec_time_message}\n\n{msg}"
        reply = QMessageBox.question(
            self,
            "Endorse Mods",
            msg,
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            # Start endorse-only operation
            json_path = self.file_path_edit.text()
            gamefolder = self.downloads_folder_edit.text()
            max_threads = self.threads_combo.currentData()  # Use the same threads value
            self.endorse_thread = DownloadThread(json_path, gamefolder, max_threads=max_threads, endorse_only=True)
            self.endorse_thread.progress_update.connect(self.update_progress)
            self.endorse_thread.error_update.connect(self.update_errors)
            self.endorse_thread.finished_signal.connect(self.thank_for_endorse)
            self.endorse_thread.start()

    def thank_for_endorse(self, _):
        QMessageBox.information(
            self,
            "Thank You!",
            "Thank you for endorsing the mods and supporting the mod authors!"
        )

    def update_progress(self, current, total):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
        self.status_label.setText(f"Downloading file {current} of {total}...")

    def update_errors(self, errors):
        self.errors_label.setText(f"Current Errors: {errors}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())