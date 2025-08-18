"""
Main GUI window for NexusDownloader application.

This module provides the primary user interface for managing mod collection downloads
with real-time progress tracking, configuration management, and multi-threaded download monitoring.
"""

import subprocess
import sys
import os
import json
import time
import threading
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QPushButton,
    QHBoxLayout, QLineEdit, QFileDialog, QProgressBar, QMenuBar,
    QMenu, QDialog, QFormLayout, QDialogButtonBox, QMessageBox,
    QComboBox, QSpinBox, QGroupBox, QGridLayout, QSplitter, 
    QStatusBar, QMainWindow, QTabWidget, QCheckBox
)
from PySide6.QtCore import QThread, Signal, QTimer, Qt, QMutex
from PySide6.QtGui import QAction, QFont, QIcon

# Add parent directory to path for relative imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Import application modules
from progress_tracking import ProgressTracker, DownloadProgress
from gui.settings_dialog import SettingsDialog
from gui.progress_monitor import ProgressMonitorWidget

# Import Phase 1 modules
try:
    from config.config_manager import ConfigManager, AppConfig
    from utils.logging_config import setup_logging, PerformanceMetrics
    from utils.security import InputValidator
    from utils.error_handler import ErrorHandler, ErrorType
    from utils.async_logger import AsyncLogHandler
    PHASE1_AVAILABLE = True
except ImportError as e:
    # Provide a more informative error message
    print(f"Error: A core module failed to import: {e}")
    print("This is often caused by running the script from the wrong directory or a missing __init__.py file.")
    print(f"Current working directory: {os.getcwd()}")
    print(f"Python path: {sys.path}")
    PHASE1_AVAILABLE = False
    # Fallback for critical classes if imports fail
    class ConfigManager: pass
    class AppConfig: pass
    class PerformanceMetrics: pass
    class InputValidator: pass
    class ErrorHandler: pass
    class ErrorType: pass
    def setup_logging(config=None): return None


class DownloadWorkerThread(QThread):
    """
    Worker thread for handling download operations with progress tracking.
    
    Executes the download process in a separate thread to prevent GUI blocking
    and provides real-time progress updates through Qt signals.
    """
    
    # Qt signals for communicating with GUI thread
    overall_progress_updated = Signal(dict)       # Overall progress statistics
    active_downloads_updated = Signal(list)       # Current active downloads
    file_completed = Signal(str, bool, str)       # File completion notification
    status_changed = Signal(str)                  # Status message updates
    log_message_received = Signal(str, str)       # Log level and message
    operation_finished = Signal(str, dict)        # Completion with final stats
    
    def __init__(self, json_path: str, game_folder: str, max_threads: int = 10, 
                 endorse_only: bool = False, config_manager: Optional['ConfigManager'] = None):
        """
        Initialize the download worker thread.
        
        Args:
            json_path: Path to the collection JSON file
            game_folder: Target folder for downloads
            max_threads: Maximum number of concurrent download threads
            endorse_only: If True, only endorse mods without downloading
            config_manager: Configuration manager instance for settings
        """
        super().__init__()
        self.json_path = json_path
        self.game_folder = game_folder
        self.max_threads = max_threads
        self.endorse_only = endorse_only
        self.config_manager = config_manager
        self.is_cancelled = False
        self.is_paused = False
        self.resume_requested = False
        
        # Track resume state for progress offset calculation
        self.progress_offset = 0  # Files already completed before resume
        self.original_total_files = 0  # Total files from original collection
        
        # Initialize progress tracking
        self.progress_tracker = ProgressTracker(
            progress_callback=self._handle_progress_update
        )
        
        # Download state tracking for pause/resume
        self.completed_downloads = set()
        self.state_file_path = None
        
        # Thread synchronization
        self.mutex = QMutex()
    
    def run(self):
        """
        Execute the download/endorsement process.
        
        This method runs in a separate thread and coordinates the entire
        download operation including progress tracking and error handling.
        """
        try:
            # Initialize state file path for pause/resume functionality
            import hashlib
            import os
            collection_hash = hashlib.md5(f"{self.json_path}:{self.game_folder}".encode()).hexdigest()[:8]
            # Create state directory in the app directory, not the collection directory
            app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # Go up to main app directory
            state_dir = os.path.join(app_dir, ".nexusdownloader_state")
            os.makedirs(state_dir, exist_ok=True)
            self.state_file_path = os.path.join(state_dir, f"download_state_{collection_hash}.json")
            
            # Load previous download state if available
            self._load_download_state()
            
            # Initialize progress tracking
            self.progress_tracker.start_tracking()
            
            # Pre-load collection for progress tracking
            self._initialize_collection()
            
            # Execute the download process with resume capability
            while True:
                self._execute_operation()
                
                # Check if we should resume after pausing
                if self.is_paused and not self.is_cancelled:
                    # Wait for resume request
                    while self.is_paused and not self.is_cancelled and not self.resume_requested:
                        time.sleep(0.1)
                    
                    if self.resume_requested and not self.is_cancelled:
                        # Store original path before changing it
                        if not hasattr(self, 'original_json_path'):
                            self.original_json_path = self.json_path
                        
                        # Create resume JSON and get the path
                        resume_path = self._create_resume_json()
                        if resume_path:
                            # Update to use resume JSON
                            self.json_path = resume_path
                            self.resume_requested = False
                            self.status_changed.emit("Resuming from where we left off...")
                            continue
                        else:
                            self.log_message_received.emit("ERROR", "Failed to create resume file")
                            break
                    
                # Exit the loop if not resuming
                break
            
        except Exception as e:
            self.log_message_received.emit("ERROR", f"Operation failed: {str(e)}")
            self.status_changed.emit(f"Error: {str(e)}")
        finally:
            # Save final state and cleanup progress tracking
            if hasattr(self, 'state_file_path'):
                self._save_download_state()
            self.progress_tracker.stop_tracking()
    
    def _initialize_collection(self):
        """
        Initialize collection data for progress tracking.
        
        Pre-loads the collection JSON file to register all files that will
        be processed, enabling accurate progress reporting.
        """
        try:
            with open(self.json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            total_files = len(data.get('mods', []))
            self.log_message_received.emit("INFO", f"Initializing {total_files} files for processing")
            
            for i, entry in enumerate(data.get('mods', [])):
                if self.is_cancelled:
                    break
                    
                try:
                    mod_id = entry['source']['modId']
                    file_id = entry['source']['fileId']
                    filename = entry.get('name', f"mod_{mod_id}_file_{file_id}")
                    
                    # Create unique identifier for this download
                    download_id = f"{mod_id}:{file_id}"
                    
                    # Check if this download was already completed
                    if download_id in self.completed_downloads:
                        # Register as already completed
                        self.progress_tracker.register_file(mod_id, file_id, filename, 100)
                        self.progress_tracker.mark_file_completed(mod_id, file_id)
                    else:
                        # Register file for tracking
                        self.progress_tracker.register_file(mod_id, file_id, filename, 0)
                    
                except KeyError as e:
                    self.log_message_received.emit("WARNING", f"Skipping entry {i} due to missing key: {e}")
                    continue
            
            self.status_changed.emit(f"Registered {total_files} files for processing")
            
        except Exception as e:
            self.log_message_received.emit("ERROR", f"Failed to initialize collection: {str(e)}")
    
    def _execute_operation(self):
        """
        Execute the main download or endorsement operation.
        
        Launches the appropriate subprocess and monitors its output for
        progress updates and completion status.
        """
        script_dir = os.path.dirname(os.path.abspath(__file__))
        loadcollection_path = os.path.join(script_dir, "../loadcollection.py")
        
        args = [
            sys.executable, loadcollection_path,
            "--json", self.json_path,
            "--gamefolder", self.game_folder,
            "--maxthreads", str(self.max_threads)
        ]
        if self.endorse_only:
            args.append("--endorseonly")
        
        operation_type = "endorsement" if self.endorse_only else "download"
        self.log_message_received.emit("INFO", f"Starting {operation_type} with {self.max_threads} threads")
        self.status_changed.emit(f"Starting {operation_type} process...")
        
        try:
            process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=os.path.dirname(script_dir),
                text=True,
                bufsize=1,
                universal_newlines=True
            )
            
            # Monitor process output
            for line in process.stdout:
                if self.is_cancelled:
                    process.terminate()
                    break
                
                # Handle pause logic - terminate process when paused
                if self.is_paused:
                    self.status_changed.emit("Pausing operation - terminating current downloads...")
                    process.terminate()
                    break
                
                line = line.strip()
                if line:
                    # Output subprocess line to logging instead of direct print
                    self.log_message_received.emit("DEBUG", f"Subprocess output: {line}")
                    self._parse_output_line(line)
            
            process.wait()
            
            if self.is_cancelled:
                # Operation was cancelled, emit finished signal
                self.status_changed.emit("Operation cancelled.")
                cancel_stats = self.progress_tracker.get_overall_progress()
                self.operation_finished.emit("Operation cancelled by user.", cancel_stats)
            elif self.is_paused:
                # Operation was paused, emit paused status
                self.status_changed.emit("Operation paused. Click Resume to continue.")
                # Don't emit operation_finished when paused
            else:
                final_stats = self.progress_tracker.get_overall_progress()
                completion_msg = f"{operation_type.title()} completed successfully!"
                
                # Clean up resume file if it exists
                self._cleanup_resume_file()
                
                self.operation_finished.emit(completion_msg, final_stats)
            
        except Exception as e:
            self.log_message_received.emit("ERROR", f"Process execution failed: {str(e)}")
            raise
    
    def _parse_output_line(self, line: str):
        """
        Parse subprocess output line for progress information.
        
        Args:
            line: Output line from the download process
        """
        try:
            # Only debug key parsing events
            if "Starting download of" in line or "Successfully downloaded" in line or "Completed download for file" in line:
                self.log_message_received.emit("DEBUG", f"Key event - {line[:120]}")
            if "Completed download for file" in line:
                import re
                # Handle both formats: with and without "0000\t" prefix
                match = re.search(r'(?:0000\s+)?Completed download for file (\d+) of (\d+)', line)
                if match:
                    current = int(match.group(1))
                    total = int(match.group(2))
                    
                    # Adjust progress numbers if we're in resume mode
                    if hasattr(self, 'progress_offset') and self.progress_offset > 0:
                        adjusted_current = current + self.progress_offset
                        adjusted_total = getattr(self, 'original_total_files', total)
                        actual_file_index = adjusted_current  # The actual file index in original collection
                        self.status_changed.emit(f"Completed file {adjusted_current} of {adjusted_total}")
                    else:
                        adjusted_current = current
                        adjusted_total = total
                        actual_file_index = current
                        self.status_changed.emit(f"Completed file {current} of {total}")
                    
                    # Track completion by actual file index (not the adjusted progress number)
                    if not hasattr(self, 'completed_file_indices'):
                        self.completed_file_indices = set()
                    self.completed_file_indices.add(actual_file_index)
                    
                    # Save state when files complete
                    self._save_download_state()
                    
                    # Update progress tracker and emit progress signal
                    if hasattr(self, 'progress_tracker'):
                        # Calculate average speed from recent downloads
                        avg_speed = 0
                        if hasattr(self, '_recent_speeds') and self._recent_speeds:
                            avg_speed = sum(self._recent_speeds) / len(self._recent_speeds)
                            self.log_message_received.emit("DEBUG", f"Calculated average speed: {avg_speed:.2f} MB/s from {len(self._recent_speeds)} samples")
                        
                        # Calculate ETA based on remaining files and current speed
                        eta_formatted = 'Calculating...'
                        if avg_speed > 0 and total > current:
                            remaining_files = total - current
                            # Estimate 2 seconds per file as baseline, adjusted by speed
                            estimated_seconds = remaining_files * (2 / max(avg_speed, 0.1))
                            eta_formatted = f"{int(estimated_seconds // 60)}:{int(estimated_seconds % 60):02d}"
                        
                        # Get current active downloads for the Download Monitor
                        # Since subprocess doesn't provide detailed individual download info,
                        # create simulated active downloads based on progress
                        active_downloads = []
                        downloading_files_count = 0
                        
                        # Simulate active downloads based on concurrent thread count
                        max_concurrent = getattr(self, 'max_threads', 10)
                        remaining_files = adjusted_total - adjusted_current
                        
                        # Show progress bars for estimated concurrent downloads
                        concurrent_count = min(max_concurrent, remaining_files)
                        if concurrent_count > 0:
                            from progress_tracking import DownloadProgress
                            import random
                            
                            # Try to get actual mod names from the JSON if available
                            # adjusted_current is the number of completed files (1-based count)
                            # We want names for the next files to be downloaded (0-based indexing)
                            mod_names = self._get_upcoming_mod_names(adjusted_current, concurrent_count)
                            self.log_message_received.emit("DEBUG", f"Requested {concurrent_count} mod names starting from index {adjusted_current}, got {len(mod_names)} names")
                            
                            for i in range(concurrent_count):
                                # Create simulated download progress with varying progress
                                progress_percent = random.randint(20, 80)  # Random progress between 20-80%
                                file_index = adjusted_current + i + 1
                                
                                # Use actual mod name if available, otherwise fallback
                                if i < len(mod_names):
                                    filename = mod_names[i]
                                else:
                                    filename = f"Downloading file {file_index}..."
                                
                                download_progress = DownloadProgress(
                                    mod_id=file_index,
                                    file_id=0,
                                    filename=filename,
                                    total_size=100,  # Mock size
                                    downloaded_size=progress_percent  # Dynamic progress
                                )
                                active_downloads.append(download_progress)
                            
                            downloading_files_count = len(active_downloads)
                        
                        self.log_message_received.emit("DEBUG", f"Progress update - {downloading_files_count} simulated active downloads")
                        
                        # Create progress update with calculated values (use adjusted numbers for display)
                        overall_progress = {
                            'completed_files': adjusted_current,
                            'total_files': adjusted_total,
                            'overall_progress_percent': (adjusted_current / adjusted_total) * 100 if adjusted_total > 0 else 0,
                            'downloading_files': downloading_files_count,
                            'failed_files': 0,
                            'overall_speed': round(avg_speed, 2),
                            'eta_formatted': eta_formatted
                        }
                        
                        # Emit the progress update
                        update_data = {
                            'overall_progress': overall_progress,
                            'active_downloads': active_downloads
                        }
                        self._handle_progress_update(update_data)
            
            elif "Starting download for mod" in line:
                # Extract mod_id and file_id from log line
                import re
                match = re.search(r'Starting download for mod (\d+), file (\d+)', line)
                if match:
                    mod_id, file_id = match.group(1), match.group(2)
                    download_id = f"{mod_id}:{file_id}"
                    # Track that this download started
                    if download_id not in self.completed_downloads:
                        self.log_message_received.emit("DEBUG", f"Started tracking download: {download_id}")
            
            elif "Starting download of" in line:
                # Extract active download info for Download Monitor
                import re
                self.log_message_received.emit("DEBUG", f"Found 'Starting download of' line: {line}")
                # Format: "Starting download of filename (#123)"
                match = re.search(r'Starting download of (.+?)\s+\(#(\d+)\)', line)
                if match:
                    filename, download_num = match.group(1), match.group(2)
                    self.log_message_received.emit("DEBUG", f"Parsed filename='{filename}', download_num='{download_num}'")
                    
                    # Initialize active downloads tracking if not exists
                    if not hasattr(self, '_active_downloads'):
                        self._active_downloads = {}
                    
                    # Create a simple active download object
                    from progress_tracking import DownloadProgress
                    download_progress = DownloadProgress(
                        mod_id=int(download_num),  # Using download_num as mod_id for simplicity
                        file_id=0,  # Default file_id
                        filename=filename,
                        total_size=0,
                        downloaded_size=0
                    )
                    
                    self._active_downloads[download_num] = download_progress
                    self.log_message_received.emit("DEBUG", f"Added to _active_downloads. Total active: {len(self._active_downloads)}")
                    self.log_message_received.emit("DEBUG", f"Started tracking download: {filename}")
                else:
                    self.log_message_received.emit("DEBUG", f"Regex didn't match for line: {line}")
            
            elif "Successfully downloaded" in line:
                # Extract completed download with speed info
                import re
                # Format: "Successfully downloaded filename (bytes, X.XX MB/s, Time: duration) (#num)"
                match = re.search(r'Successfully downloaded .+ \((\d+) bytes, ([\d.]+) MB/s, Time: (.+?)\) \(#(\d+)\)', line)
                if match:
                    bytes_downloaded, speed_mbps, duration, download_num = match.groups()
                    
                    # Track the speed for overall calculation
                    if not hasattr(self, '_recent_speeds'):
                        self._recent_speeds = []
                    self._recent_speeds.append(float(speed_mbps))
                    
                    # Keep only recent speeds (last 10 downloads for moving average)
                    if len(self._recent_speeds) > 10:
                        self._recent_speeds = self._recent_speeds[-10:]
                    
                    self.log_message_received.emit("DEBUG", f"Download completed: {speed_mbps} MB/s")
                    
                    # Remove from active downloads if being tracked
                    if hasattr(self, '_active_downloads'):
                        # Try to find by download number in the _active_downloads dict
                        if download_num in self._active_downloads:
                            del self._active_downloads[download_num]
                
                # Also handle the case for tracking mod/file completion
                mod_file_match = re.search(r'(?:mod|for)\s+(\d+).*?(?:file|#)[\s#]*(\d+)', line)
                if mod_file_match:
                    mod_id, file_id = mod_file_match.group(1), mod_file_match.group(2)
                    download_id = f"{mod_id}:{file_id}"
                    self.completed_downloads.add(download_id)
            
            elif "already exists" in line:
                # Handle files that already exist (also track for completion)
                import re
                match = re.search(r'(?:mod|for)\s+(\d+).*?(?:file|#)[\s#]*(\d+)', line)
                if match:
                    mod_id, file_id = match.group(1), match.group(2)
                    download_id = f"{mod_id}:{file_id}"
                    self.completed_downloads.add(download_id)
            
            elif line.startswith("PROGRESS:"):
                parts = line.split()
                if len(parts) >= 2:
                    nums = parts[1].split("/")
                    if len(nums) == 2:
                        current, total = int(nums[0]), int(nums[1])
                        
                        # Adjust progress numbers if we're in resume mode
                        if hasattr(self, 'progress_offset') and self.progress_offset > 0:
                            adjusted_current = current + self.progress_offset
                            adjusted_total = getattr(self, 'original_total_files', total)
                            self.status_changed.emit(f"Progress: {adjusted_current}/{adjusted_total} files")
                        else:
                            adjusted_current = current
                            adjusted_total = total
                            self.status_changed.emit(f"Progress: {current}/{total} files")
                        
            
            elif "ERRORS:" in line:
                import re
                match = re.search(r'ERRORS:\s*(\d+)', line)
                if match:
                    errors = int(match.group(1))
                    self.log_message_received.emit("WARNING", f"Total errors: {errors}")
            
            elif "Total Execution Time" in line:
                self.log_message_received.emit("INFO", line)
            
            elif any(keyword in line.lower() for keyword in ["error", "failed", "exception"]):
                self.log_message_received.emit("ERROR", line)
            
            elif any(keyword in line.lower() for keyword in ["warning", "skip"]):
                self.log_message_received.emit("WARNING", line)
                
            else:
                # General status update
                if len(line) > 10:
                    self.status_changed.emit(line[:100])
                    
        except Exception as e:
            self.log_message_received.emit("ERROR", f"Error parsing output: {str(e)}")
    
    def _handle_progress_update(self, update_data: Dict[str, Any]):
        """
        Handle progress updates from the progress tracker.
        
        Args:
            update_data: Dictionary containing progress information
        """
        try:
            overall_progress = update_data['overall_progress']
            active_downloads = update_data['active_downloads']
            
            # Emit signals for GUI updates
            self.overall_progress_updated.emit(overall_progress)
            self.active_downloads_updated.emit(active_downloads)
            
        except Exception as e:
            self.log_message_received.emit("ERROR", f"Error handling progress update: {str(e)}")
    
    def cancel_operation(self):
        """Cancel the current operation gracefully."""
        self.is_cancelled = True
        self.is_paused = False  # Clear paused state
        self.resume_requested = False  # Clear resume request
        self.status_changed.emit("Cancelling operation...")
        
        # Force thread to emit finished signal when cancelled
        if hasattr(self, '_should_emit_finished'):
            self._should_emit_finished = True
    
    def pause_operation(self):
        """Pause the current operation - finish current downloads but don't start new ones."""
        self.is_paused = True
        self.resume_requested = False
        self.status_changed.emit("Pausing operation - finishing current downloads...")
    
    def resume_operation(self):
        """Resume the paused operation."""
        self.is_paused = False
        self.resume_requested = True
        self.status_changed.emit("Resuming operation...")
        
        # Set flag to restart operation - the run() method will handle restarting
        # when it detects resume_requested = True
    
    def _save_download_state(self):
        """Save current download state to file for resume capability."""
        if self.state_file_path:
            try:
                import json
                # Always save the original path, not the resume path
                save_path = getattr(self, 'original_json_path', self.json_path)
                
                # Save both old and new tracking methods for compatibility
                completed_downloads_list = list(getattr(self, 'completed_downloads', set()))
                completed_indices_list = list(getattr(self, 'completed_file_indices', set()))
                
                state_data = {
                    "completed_downloads": completed_downloads_list,
                    "completed_file_indices": completed_indices_list,
                    "json_path": save_path,
                    "game_folder": self.game_folder,
                    "timestamp": time.time()
                }
                with open(self.state_file_path, 'w') as f:
                    json.dump(state_data, f, indent=2)
                
                self.log_message_received.emit("DEBUG", f"Saved state to: {self.state_file_path}")
                self.log_message_received.emit("DEBUG", f"State contains {len(completed_indices_list)} file indices")
            except Exception as e:
                self.log_message_received.emit("WARNING", f"Failed to save download state: {str(e)}")
    
    def _load_download_state(self):
        """Load previous download state from file."""
        if self.state_file_path and os.path.exists(self.state_file_path):
            try:
                import json
                with open(self.state_file_path, 'r') as f:
                    state_data = json.load(f)
                
                # Use original path for comparison if available, otherwise current path
                compare_path = getattr(self, 'original_json_path', self.json_path)
                
                # Only load if it's for the same collection and game folder
                if (state_data.get("json_path") == compare_path and 
                    state_data.get("game_folder") == self.game_folder):
                    
                    # Load both tracking methods
                    self.completed_downloads = set(state_data.get("completed_downloads", []))
                    self.completed_file_indices = set(state_data.get("completed_file_indices", []))
                    
                    total_completed = len(self.completed_downloads) + len(self.completed_file_indices)
                    self.log_message_received.emit("INFO", f"Loaded {total_completed} completed downloads from previous session")
                    return True
            except Exception as e:
                self.log_message_received.emit("WARNING", f"Failed to load download state: {str(e)}")
        return False
    
    def _create_resume_json(self):
        """Create a filtered JSON file with only incomplete downloads for resume."""
        try:
            # Load original JSON (not the resume JSON)
            original_path = getattr(self, 'original_json_path', self.json_path)
            with open(original_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # Filter out completed downloads
            original_mods = data.get('mods', [])
            filtered_mods = []
            
            for index, entry in enumerate(original_mods, 1):  # Start from 1 to match file numbering
                try:
                    # Check if this file index was completed
                    if hasattr(self, 'completed_file_indices') and index in self.completed_file_indices:
                        continue  # Skip completed files
                    
                    # Also check old method for backwards compatibility
                    mod_id = entry['source']['modId']
                    file_id = entry['source']['fileId']
                    download_id = f"{mod_id}:{file_id}"
                    
                    # Only include if not completed by either method
                    if download_id not in getattr(self, 'completed_downloads', set()):
                        filtered_mods.append(entry)
                except KeyError:
                    # Include entries with missing keys (they'll be skipped anyway)
                    filtered_mods.append(entry)
            
            # Create resume JSON
            resume_data = data.copy()
            resume_data['mods'] = filtered_mods
            
            # Generate resume path in app directory with game domain
            app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            original_path = getattr(self, 'original_json_path', self.json_path)
            original_filename = os.path.basename(original_path)
            
            # Extract game domain from game folder path for unique naming
            game_domain = os.path.basename(self.game_folder) if self.game_folder else "unknown"
            resume_filename = f"{game_domain}_{original_filename.replace('.json', '_resume.json')}"
            resume_path = os.path.join(app_dir, resume_filename)
            with open(resume_path, 'w', encoding='utf-8') as f:
                json.dump(resume_data, f, indent=2)
            
            skipped = len(original_mods) - len(filtered_mods)
            self.log_message_received.emit("INFO", f"Resume JSON created: {len(filtered_mods)} downloads remaining, {skipped} already completed")
            
            # Set resume tracking information for progress calculation
            self.progress_offset = skipped  # Number of files already completed
            self.original_total_files = len(original_mods)  # Original total from collection
            
            # Debug tracking information
            completed_downloads_count = len(getattr(self, 'completed_downloads', set()))
            completed_indices_count = len(getattr(self, 'completed_file_indices', set()))
            self.log_message_received.emit("DEBUG", f"Completed downloads (old method): {completed_downloads_count} items")
            self.log_message_received.emit("DEBUG", f"Completed file indices (new method): {completed_indices_count} items")
            self.log_message_received.emit("DEBUG", f"Progress offset set to: {self.progress_offset}, original total: {self.original_total_files}")
            if hasattr(self, 'completed_file_indices') and self.completed_file_indices:
                indices_sample = sorted(list(self.completed_file_indices))[:10]
                self.log_message_received.emit("DEBUG", f"Sample completed indices: {indices_sample}...")
            
            return resume_path
            
        except Exception as e:
            self.log_message_received.emit("ERROR", f"Failed to create resume JSON: {str(e)}")
            return None
    
    def _cleanup_resume_file(self):
        """Delete the resume file for this session if it exists."""
        try:
            if hasattr(self, 'game_folder') and hasattr(self, 'original_json_path'):
                app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                original_filename = os.path.basename(getattr(self, 'original_json_path', self.json_path))
                game_domain = os.path.basename(self.game_folder) if self.game_folder else "unknown"
                resume_filename = f"{game_domain}_{original_filename.replace('.json', '_resume.json')}"
                resume_path = os.path.join(app_dir, resume_filename)
                
                if os.path.exists(resume_path):
                    os.remove(resume_path)
                    self.log_message_received.emit("DEBUG", f"Cleaned up resume file: {resume_filename}")
        except Exception as e:
            # Don't fail the operation for cleanup issues
            self.log_message_received.emit("DEBUG", f"Resume file cleanup failed: {str(e)}")
    
    def _get_upcoming_mod_names(self, current_index: int, count: int) -> List[str]:
        """
        Get the names of upcoming mod files from the JSON collection.
        
        Args:
            current_index: Current download progress index
            count: Number of mod names to retrieve
            
        Returns:
            List of mod names for the upcoming downloads
        """
        mod_names = []
        try:
            import json
            with open(self.json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            mods = data.get('mods', [])
            self.log_message_received.emit("DEBUG", f"Loading mod names: total mods in JSON={len(mods)}, requested from index {current_index}, count={count}")
            
            for i in range(count):
                mod_index = current_index + i
                if mod_index < len(mods):
                    entry = mods[mod_index]
                    mod_id = entry.get('source', {}).get('modId', 'unknown')
                    file_id = entry.get('source', {}).get('fileId', 'unknown')
                    name = entry.get('name', f"mod_{mod_id}_file_{file_id}")
                    self.log_message_received.emit("DEBUG", f"Found mod at index {mod_index}: '{name}'")
                    mod_names.append(name)
                else:
                    # No more mods available
                    self.log_message_received.emit("DEBUG", f"No more mods available at index {mod_index}")
                    break
                    
        except Exception as e:
            self.log_message_received.emit("DEBUG", f"Failed to load mod names: {str(e)}")
            # Fallback to generic names
            for i in range(count):
                mod_names.append(f"Downloading file {current_index + i + 1}...")
                
        return mod_names


class MainWindow(QMainWindow):
    """
    Main application window for NexusDownloader.
    
    Provides the primary user interface with file selection, configuration,
    progress monitoring, and download management capabilities.
    """
    
    def __init__(self):
        """Initialize the main window with all UI components and configuration."""
        super().__init__()
        self.setWindowTitle("NexusDownloader")
        self.setMinimumSize(900, 700)
        
        # Set application icon
        icon_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "assets", "icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        
        # Initialize configuration management
        self._initialize_configuration()
        
        # Initialize UI components
        self.download_thread = None
        self.overall_statistics = {}
        
        # Setup UI and timers
        self._setup_user_interface()
        self._setup_update_timers()
        self._validate_initial_configuration()

    def _initialize_configuration(self):
        """Initialize configuration manager and logging."""
        self.config_manager = None
        self.logger = None
        if PHASE1_AVAILABLE:
            try:
                # Use the repository's src/config.json (one level up from gui) so
                # the GUI reads/writes the same configuration file shipped with the package.
                config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../config.json")
                
                if not os.path.exists(config_path):
                    # Provide a clear error message if the config file is missing
                    QMessageBox.critical(
                        self, "Configuration Error",
                        f"The configuration file could not be found at the expected path:\n{config_path}\n\nPlease ensure 'config.json' exists in the 'src' directory."
                    )
                    # Exit gracefully if config is essential for startup
                    sys.exit(1)

                self.config_manager = ConfigManager(config_path)
                # Pass the entire logging config object to setup_logging
                self.logger = setup_logging(self.config_manager.get_config().logging)
                
                # Initialize async logging handler to prevent UI blocking
                self.async_log_handler = AsyncLogHandler(self.logger)
            except Exception as e:
                # Show a detailed error message to the user
                QMessageBox.critical(
                    self, "Fatal Configuration Error",
                    f"A critical error occurred while loading the configuration:\n\n{str(e)}\n\nThe application will now exit."
                )
                # Exit gracefully
                sys.exit(1)
        else:
            QMessageBox.critical(
                self, "Dependency Error",
                "Core components (Phase 1 modules) are missing. The application cannot continue."
            )
            sys.exit(1)

    def _setup_user_interface(self):
        """Setup the main user interface components."""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # Create menu bar and status bar
        self._create_menu_bar()
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")
        
        # Main layout with sections
        main_layout = QVBoxLayout(central_widget)
        
        # File selection section
        file_section = self._create_file_selection_section()
        main_layout.addWidget(file_section)
        
        # Progress monitoring section
        splitter = QSplitter(Qt.Horizontal)
        
        # Overall progress
        progress_section = self._create_overall_progress_section()
        splitter.addWidget(progress_section)
        
        # Active downloads monitor
        # Initialize with current max threads from config
        current_max_threads = self.config_manager.get_config().downloads.max_concurrent_downloads if self.config_manager else 10
        self.progress_monitor = ProgressMonitorWidget(current_max_threads)
        splitter.addWidget(self.progress_monitor)
        
        # Set splitter proportions
        splitter.setSizes([400, 500])
        main_layout.addWidget(splitter)
        
        # Control buttons section
        control_section = self._create_control_section()
        main_layout.addWidget(control_section)

    def _create_menu_bar(self):
        """Create the application menu bar with all actions."""
        menubar = self.menuBar()
        
        # File menu
        file_menu = menubar.addMenu("File")
        
        settings_action = QAction("Settings...", self)
        settings_action.triggered.connect(self._open_settings_dialog)
        file_menu.addAction(settings_action)
        
        file_menu.addSeparator()
        
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        
        # Tools menu
        tools_menu = menubar.addMenu("Tools")
        
        export_config_action = QAction("Export Configuration...", self)
        export_config_action.triggered.connect(self._export_configuration)
        tools_menu.addAction(export_config_action)
        
        import_config_action = QAction("Import Configuration...", self)
        import_config_action.triggered.connect(self._import_configuration)
        tools_menu.addAction(import_config_action)
        
        # Help menu
        help_menu = menubar.addMenu("Help")
        
        about_action = QAction("About...", self)
        about_action.triggered.connect(self._show_about_dialog)
        help_menu.addAction(about_action)

    def _create_file_selection_section(self) -> QGroupBox:
        """
        Create the file selection section of the UI.
        
        Returns:
            QGroupBox containing file selection controls
        """
        group = QGroupBox("File Selection")
        layout = QFormLayout(group)
        
        # Collection JSON file selection
        json_layout = QHBoxLayout()
        self.json_file_edit = QLineEdit()
        self.json_file_edit.setReadOnly(True)
        json_layout.addWidget(self.json_file_edit)
        
        json_browse_btn = QPushButton("Browse...")
        json_browse_btn.clicked.connect(self._browse_collection_file)
        json_layout.addWidget(json_browse_btn)
        
        layout.addRow("Collection JSON:", json_layout)
        
        # Game folder selection
        folder_layout = QHBoxLayout()
        self.game_folder_edit = QLineEdit()
        self.game_folder_edit.setReadOnly(True)
        folder_layout.addWidget(self.game_folder_edit)
        
        folder_browse_btn = QPushButton("Browse...")
        folder_browse_btn.clicked.connect(self._browse_game_folder)
        folder_layout.addWidget(folder_browse_btn)
        
        layout.addRow("Game Folder:", folder_layout)
        
        return group

    def _create_configuration_section(self) -> QGroupBox:
        """
        Create the download configuration section.
        
        Returns:
            QGroupBox containing configuration controls
        """
        group = QGroupBox("Download Configuration")
        layout = QHBoxLayout(group)
        
        # Thread count configuration
        layout.addWidget(QLabel("Max Threads:"))
        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(1, 50)
        self.threads_spin.setValue(10)
        self.threads_spin.valueChanged.connect(self._on_thread_count_changed)
        layout.addWidget(self.threads_spin)
        
        # Thread status indicator
        self.thread_status_label = QLabel("")
        self.thread_status_label.setStyleSheet("color: #666; font-size: 10px;")
        layout.addWidget(self.thread_status_label)
        
        layout.addStretch()
        
        # Thread count presets
        preset_buttons = [
            ("Conservative (5)", 5),
            ("Balanced (10)", 10),
            ("Aggressive (20)", 20)
        ]
        
        for label, value in preset_buttons:
            btn = QPushButton(label)
            btn.clicked.connect(lambda checked, v=value: self.threads_spin.setValue(v))
            layout.addWidget(btn)
        
        return group

    def _create_overall_progress_section(self) -> QGroupBox:
        """
        Create the overall progress tracking section.
        
        Returns:
            QGroupBox containing overall progress display
        """
        group = QGroupBox("Overall Progress")
        layout = QVBoxLayout(group)
        
        # Main progress bar
        self.main_progress_bar = QProgressBar()
        self.main_progress_bar.setMinimumHeight(30)
        layout.addWidget(self.main_progress_bar)
        
        # Statistics display
        stats_widget = QWidget()
        stats_layout = QGridLayout(stats_widget)
        
        # Progress statistics labels
        stats_layout.addWidget(QLabel("Files Completed:"), 0, 0)
        self.files_completed_label = QLabel("0 / 0")
        stats_layout.addWidget(self.files_completed_label, 0, 1)
        
        stats_layout.addWidget(QLabel("Overall Speed:"), 1, 0)
        self.overall_speed_label = QLabel("0.0 MB/s")
        stats_layout.addWidget(self.overall_speed_label, 1, 1)
        
        stats_layout.addWidget(QLabel("Estimated Time:"), 2, 0)
        self.eta_label = QLabel("Unknown")
        stats_layout.addWidget(self.eta_label, 2, 1)
        
        stats_layout.addWidget(QLabel("Elapsed Time:"), 3, 0)
        self.elapsed_label = QLabel("00:00:00")
        stats_layout.addWidget(self.elapsed_label, 3, 1)
        
        stats_layout.addWidget(QLabel("Active Downloads:"), 4, 0)
        self.active_count_label = QLabel("0")
        stats_layout.addWidget(self.active_count_label, 4, 1)
        
        stats_layout.addWidget(QLabel("Errors:"), 5, 0)
        self.errors_label = QLabel("0")
        stats_layout.addWidget(self.errors_label, 5, 1)
        
        layout.addWidget(stats_widget)
        
        return group

    def _create_control_section(self) -> QGroupBox:
        """
        Create the control buttons section.
        
        Returns:
            QGroupBox containing control buttons
        """
        group = QGroupBox("Controls")
        layout = QHBoxLayout(group)
        
        self.start_button = QPushButton("Start Download")
        self.start_button.setMinimumHeight(40)
        self.start_button.clicked.connect(self._start_download_operation)
        layout.addWidget(self.start_button)
        
        self.pause_button = QPushButton("Pause")
        self.pause_button.setMinimumHeight(40)
        self.pause_button.setEnabled(False)
        self.pause_button.clicked.connect(self._toggle_pause_operation)
        layout.addWidget(self.pause_button)
        
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setMinimumHeight(40)
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel_download_operation)
        layout.addWidget(self.cancel_button)
        
        return group

    def _setup_update_timers(self):
        """Setup timers for periodic UI updates."""
        # Timer for updating elapsed time display
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self._update_elapsed_time_display)
        self.update_timer.start(1000)  # Update every second

    def _validate_initial_configuration(self):
        """Validate initial configuration and prompt user if needed."""
        if not self.config_manager:
            return
            
        api_key = self.config_manager.get_api_key()
        if not api_key or len(api_key.strip()) < 20:
            QMessageBox.warning(
                self,
                "Configuration Required",
                "Your Nexus API key is not configured. Please configure it in Settings."
            )
            self._open_settings_dialog()

    def _browse_collection_file(self):
        """Browse for and select a collection JSON file."""
        # Get initial directory from config or use empty string
        initial_dir = ""
        if self.config_manager:
            try:
                config = self.config_manager.get_config()
                if (hasattr(config, 'ui_preferences') and 
                    config.ui_preferences and 
                    config.ui_preferences.remember_collection_location):
                    saved_dir = config.ui_preferences.last_collection_directory
                    # Only use saved directory if it exists
                    if saved_dir and os.path.exists(saved_dir):
                        initial_dir = saved_dir
            except Exception:
                pass  # Fall back to empty string if config is not available
        
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Collection JSON File",
            initial_dir,
            "JSON Files (*.json);;All Files (*)"
        )
        
        if file_path:
            self.json_file_edit.setText(file_path)
            
            # Save the directory for next time
            self._save_last_collection_directory(file_path)

    def _save_last_collection_directory(self, file_path: str):
        """Save the directory of the selected collection file for next time."""
        if self.config_manager:
            try:
                import os
                directory = os.path.dirname(file_path)
                
                # Update the config
                self.config_manager.update_config(
                    ui_preferences={
                        'last_collection_directory': directory,
                        'remember_collection_location': True
                    }
                )
                
            except Exception as e:
                # Don't show error to user for this non-critical feature
                if hasattr(self, 'async_log_handler') and self.async_log_handler:
                    self.async_log_handler.handle_log_message("DEBUG", f"Failed to save collection directory: {e}")
                elif self.logger and hasattr(self.logger, 'debug'):
                    self.logger.debug(f"Failed to save collection directory: {e}")
                elif self.logger and hasattr(self.logger, 'logger'):
                    self.logger.logger.debug(f"Failed to save collection directory: {e}")
                else:
                    import sys
                    print(f"Failed to save collection directory: {e}", file=sys.stderr)

    def _browse_game_folder(self):
        """Browse for and select the game downloads folder."""
        if self.config_manager:
            config = self.config_manager.get_config()
            initial_dir = config.vortex.downloads_folder_root
        else:
            initial_dir = ""
            
        folder_path = QFileDialog.getExistingDirectory(
            self,
            "Select Game Downloads Folder",
            initial_dir
        )
        if folder_path:
            self.game_folder_edit.setText(folder_path)

    def _on_thread_count_changed(self, value: int):
        """
        Handle thread count changes from the user.
        
        Args:
            value: New thread count value
        """
        # Update progress monitor with new thread count
        if hasattr(self, 'progress_monitor'):
            self.progress_monitor.set_max_threads(value)
        
        # Check if downloads are currently active
        downloads_active = (hasattr(self, 'download_thread') and 
                           self.download_thread and 
                           self.download_thread.isRunning())
        
        if downloads_active:
            # Show warning that change won't take effect immediately
            self._show_thread_change_warning(value)
        else:
            # Update configuration immediately when not downloading
            self._update_thread_config(value)
    
    def _show_thread_change_warning(self, new_thread_count: int):
        """
        Show warning dialog when thread count is changed during active downloads.
        
        Args:
            new_thread_count: The new thread count value
        """
        # Get the current active thread count from the running download
        current_threads = (self.download_thread.max_threads 
                         if hasattr(self.download_thread, 'max_threads') 
                         else "Unknown")
        
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Information)
        msg.setWindowTitle("Thread Count Change")
        msg.setText("Downloads are currently active")
        msg.setInformativeText(
            f"The thread count has been updated to {new_thread_count}, but downloads are "
            f"currently running with {current_threads} threads.\n\n"
            f"The new thread count will take effect when you:\n"
            f"• Start a new download session\n"
            f"• Resume downloads after pause/cancel\n\n"
            f"Current active threads will continue until their downloads complete."
        )
        msg.setStandardButtons(QMessageBox.Ok)
        msg.exec_()
        
        # Show temporary status message
        self.status_bar.showMessage(
            f"Thread count will change to {new_thread_count} on next download session", 
            5000
        )
        
        # Update thread status indicator
        self._update_thread_status_indicator()
    
    def _update_thread_config(self, thread_count: int):
        """
        Update the thread count configuration.
        
        Args:
            thread_count: New thread count value
        """
        if self.config_manager:
            try:
                # Update the configuration
                config = self.config_manager.get_config()
                config.downloads.max_concurrent_downloads = thread_count
                self.config_manager.save_config()
                
                # Show confirmation
                self.status_bar.showMessage(
                    f"Thread count updated to {thread_count}", 
                    2000
                )
                
                # Log the change
                if hasattr(self, 'async_log_handler') and self.async_log_handler:
                    self.async_log_handler.handle_log_message(
                        "INFO", 
                        f"Thread count updated to {thread_count}"
                    )
                
                # Update thread status indicator
                self._update_thread_status_indicator()
            except Exception as e:
                # Show error message
                QMessageBox.warning(
                    self, 
                    "Configuration Error",
                    f"Failed to update thread count configuration: {str(e)}"
                )
    
    def _update_thread_status_indicator(self):
        """Update the thread status indicator label to show current vs configured threads."""
        if not hasattr(self, 'thread_status_label'):
            return
            
        # Check if downloads are currently active
        downloads_active = (hasattr(self, 'download_thread') and 
                           self.download_thread and 
                           self.download_thread.isRunning())
        
        if downloads_active:
            # Get current active thread count from running download
            active_threads = (self.download_thread.max_threads 
                            if hasattr(self.download_thread, 'max_threads') 
                            else "?")
            
            # Get configured thread count
            configured_threads = self.threads_spin.value()
            
            if active_threads != configured_threads:
                # Show mismatch - active vs configured
                self.thread_status_label.setText(
                    f"Active: {active_threads} | Next: {configured_threads}"
                )
                self.thread_status_label.setStyleSheet("color: #ff8800; font-size: 10px; font-weight: bold;")
            else:
                # Show current active threads
                self.thread_status_label.setText(f"Active: {active_threads}")
                self.thread_status_label.setStyleSheet("color: #00aa00; font-size: 10px;")
        else:
            # No downloads active - just show configured
            configured_threads = self.threads_spin.value()
            self.thread_status_label.setText(f"Ready: {configured_threads}")
            self.thread_status_label.setStyleSheet("color: #666; font-size: 10px;")

    def _start_download_operation(self):
        """Start the download operation with validation and setup."""
        json_path = self.json_file_edit.text()
        game_folder = self.game_folder_edit.text()
        
        # Validate inputs
        if not json_path or not game_folder:
            QMessageBox.warning(self, "Missing Information", 
                              "Please select both collection JSON file and game folder.")
            return
            
        if not os.path.exists(json_path):
            QMessageBox.warning(self, "File Not Found", 
                              "The selected JSON file does not exist.")
            return
            
        if not os.path.exists(game_folder):
            QMessageBox.warning(self, "Folder Not Found", 
                              "The selected game folder does not exist.")
            return
        
        # Check for Vortex process if configured
        if self._should_check_vortex_process():
            if not self._handle_vortex_process():
                return
        
        # Initialize and start download operation
        self._execute_download_operation(json_path, game_folder)

    def _should_check_vortex_process(self) -> bool:
        """
        Determine if Vortex process checking is enabled.
        
        Returns:
            True if Vortex process checking should be performed
        """
        if self.config_manager:
            config = self.config_manager.get_config()
            return config.vortex.check_vortex_running
        return True  # Default to checking

    def _handle_vortex_process(self) -> bool:
        """
        Handle Vortex process management before download.
        
        Returns:
            True if it's safe to proceed with download
        """
        if not self._is_vortex_running():
            return True
        
        if self.config_manager:
            config = self.config_manager.get_config()
            if config.vortex.auto_close_vortex:
                return self._close_vortex_automatically()
        
        # Ask user what to do
        reply = QMessageBox.question(
            self,
            "Vortex Running",
            "Vortex is currently running and must be closed before downloading.\n\n"
            "Would you like to close Vortex automatically?",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel
        )
        
        if reply == QMessageBox.Yes:
            return self._close_vortex_automatically()
        elif reply == QMessageBox.No:
            QMessageBox.information(self, "Manual Action Required", 
                                  "Please close Vortex manually and try again.")
            return False
        else:
            return False

    def _execute_download_operation(self, json_path: str, game_folder: str):
        """
        Execute the download operation in a separate thread.
        
        Args:
            json_path: Path to collection JSON file
            game_folder: Target folder for downloads
        """
        # Reset statistics
        self.overall_statistics = {}
        
        # Get max threads from configuration
        config = self.config_manager.get_config()
        max_threads = config.downloads.max_concurrent_downloads
        
        # Create and configure download thread
        self.download_thread = DownloadWorkerThread(
            json_path, game_folder, max_threads, 
            endorse_only=False, config_manager=self.config_manager
        )
        
        # Connect thread signals to UI update methods
        self.download_thread.overall_progress_updated.connect(self._update_overall_progress)
        self.download_thread.active_downloads_updated.connect(self._update_active_downloads)
        self.download_thread.file_completed.connect(self._handle_file_completion)
        self.download_thread.status_changed.connect(self._update_status_message)
        self.download_thread.operation_finished.connect(self._handle_operation_completion)
        self.download_thread.log_message_received.connect(self._handle_log_message)
        
        # Update UI state for active download
        self.start_button.setEnabled(False)
        self.pause_button.setEnabled(True)
        self.pause_button.setText("Pause")
        self.cancel_button.setEnabled(True)
        self.status_bar.showMessage("Download operation starting...")
        
        # Start the download thread
        self.download_thread.start()
        
        # Update thread status indicator to show active threads
        self._update_thread_status_indicator()

    def _cancel_download_operation(self):
        """Cancel the current download operation with user confirmation."""
        if self.download_thread and self.download_thread.isRunning():
            reply = QMessageBox.question(
                self,
                "Cancel Download",
                "Are you sure you want to cancel the download operation?",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                self.download_thread.cancel_operation()
                self.status_bar.showMessage("Cancelling download operation...")

    def _toggle_pause_operation(self):
        """Toggle pause/resume for the current download operation."""
        if self.download_thread and self.download_thread.isRunning():
            if hasattr(self.download_thread, 'is_paused') and self.download_thread.is_paused:
                # Currently paused, so resume
                self.download_thread.resume_operation()
                self.pause_button.setText("Pause")
                self.status_bar.showMessage("Resuming download operation...")
            else:
                # Currently running, so pause
                self.download_thread.pause_operation()
                self.pause_button.setText("Resume")
                self.status_bar.showMessage("Pausing download operation...")

    def _update_overall_progress(self, statistics: Dict[str, Any]):
        """
        Update the overall progress display with new statistics.
        
        Args:
            statistics: Dictionary containing progress statistics
        """
        # Use direct logging instead of signal emission in MainWindow
        if hasattr(self, 'async_log_handler') and self.async_log_handler:
            self.async_log_handler.handle_log_message("DEBUG", f"Overall progress update: speed={statistics.get('overall_speed', 'N/A')}, eta={statistics.get('eta_formatted', 'N/A')}")
        elif self.logger and hasattr(self.logger, 'debug'):
            self.logger.debug(f"Overall progress update: speed={statistics.get('overall_speed', 'N/A')}, eta={statistics.get('eta_formatted', 'N/A')}")
        elif self.logger and hasattr(self.logger, 'logger'):
            self.logger.logger.debug(f"Overall progress update: speed={statistics.get('overall_speed', 'N/A')}, eta={statistics.get('eta_formatted', 'N/A')}")
        self.overall_statistics = statistics
        
        # Update main progress bar
        if statistics['total_files'] > 0:
            progress_percent = statistics['overall_progress_percent']
            self.main_progress_bar.setMaximum(100)
            self.main_progress_bar.setValue(int(progress_percent))
            self.main_progress_bar.setFormat(f"{progress_percent:.1f}%")
        
        # Update statistics labels
        self.files_completed_label.setText(
            f"{statistics['completed_files']} / {statistics['total_files']}"
        )
        
        if statistics['overall_speed'] > 0:
            # Speed is already in MB/s from parsing
            self.overall_speed_label.setText(f"{statistics['overall_speed']:.1f} MB/s")
        else:
            self.overall_speed_label.setText("Calculating...")
        
        self.eta_label.setText(statistics['eta_formatted'])
        self.active_count_label.setText(str(statistics['downloading_files']))
        self.errors_label.setText(str(statistics['failed_files']))
        
        # Update window title with progress
        if statistics['total_files'] > 0:
            progress_percent = statistics['overall_progress_percent']
            self.setWindowTitle(f"NexusDownloader ({progress_percent:.1f}%)")

    def _update_active_downloads(self, active_downloads: List):
        """
        Update the active downloads display.
        
        Args:
            active_downloads: List of currently active download objects
        """
        # Use direct logging instead of signal emission in MainWindow
        if hasattr(self, 'async_log_handler') and self.async_log_handler:
            self.async_log_handler.handle_log_message("DEBUG", f"Updating active downloads: {len(active_downloads)} items")
            if active_downloads:
                self.async_log_handler.handle_log_message("DEBUG", f"First download item: {active_downloads[0]}")
        elif self.logger:
            if hasattr(self.logger, 'debug'):
                self.logger.debug(f"Updating active downloads: {len(active_downloads)} items")
                if active_downloads:
                    self.logger.debug(f"First download item: {active_downloads[0]}")
            elif hasattr(self.logger, 'logger'):
                self.logger.logger.debug(f"Updating active downloads: {len(active_downloads)} items")
                if active_downloads:
                    self.logger.logger.debug(f"First download item: {active_downloads[0]}")
        self.progress_monitor.update_downloads(active_downloads)

    def _handle_file_completion(self, filename: str, success: bool, message: str):
        """
        Handle individual file completion notification.
        
        Args:
            filename: Name of the completed file
            success: Whether the file completed successfully
            message: Additional completion message
        """
        if success:
            self.status_bar.showMessage(f"Completed: {filename[:50]}...", 2000)
        else:
            self.status_bar.showMessage(f"Failed: {filename[:50]}... - {message}", 5000)

    def _update_status_message(self, status: str):
        """
        Update the status bar message.
        
        Args:
            status: New status message
        """
        self.status_bar.showMessage(status)

    def _update_elapsed_time_display(self):
        """Update the elapsed time display with current time."""
        if self.overall_statistics and 'elapsed_formatted' in self.overall_statistics:
            self.elapsed_label.setText(self.overall_statistics['elapsed_formatted'])

    def _handle_operation_completion(self, message: str, statistics: Dict[str, Any]):
        """
        Handle download operation completion.
        
        Args:
            message: Completion message
            statistics: Final operation statistics
        """
        # Reset UI state
        self.start_button.setEnabled(True)
        self.pause_button.setEnabled(False)
        self.pause_button.setText("Pause")
        self.cancel_button.setEnabled(False)
        self.setWindowTitle("NexusDownloader")
        
        # Show completion dialog
        stats_text = f"""
Operation Statistics:
• Total Files: {statistics.get('total_files', 0)}
• Completed: {statistics.get('completed_files', 0)}
• Failed: {statistics.get('failed_files', 0)}
• Skipped: {statistics.get('skipped_files', 0)}
• Elapsed Time: {statistics.get('elapsed_formatted', 'Unknown')}
        """.strip()
        
        full_message = f"{message}\n\n{stats_text}\n\nWould you like to endorse the downloaded mods?"
        
        reply = QMessageBox.question(
            self,
            "Operation Complete",
            full_message,
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            self._start_endorsement_operation()
        
        self.status_bar.showMessage("Operation completed")
        
        # Update thread status indicator now that downloads are complete
        self._update_thread_status_indicator()

    def _start_endorsement_operation(self):
        """Start the mod endorsement operation."""
        json_path = self.json_file_edit.text()
        game_folder = self.game_folder_edit.text()
        # Get max threads from configuration
        config = self.config_manager.get_config()
        max_threads = config.downloads.max_concurrent_downloads
        
        self.download_thread = DownloadWorkerThread(
            json_path, game_folder, max_threads,
            endorse_only=True, config_manager=self.config_manager
        )
        
        # Connect essential signals
        self.download_thread.overall_progress_updated.connect(self._update_overall_progress)
        self.download_thread.status_changed.connect(self._update_status_message)
        self.download_thread.operation_finished.connect(self._handle_endorsement_completion)
        self.download_thread.log_message_received.connect(self._handle_log_message)
        
        # Update UI state
        self.start_button.setEnabled(False)
        self.pause_button.setEnabled(True)
        self.pause_button.setText("Pause")
        self.cancel_button.setEnabled(True)
        self.status_bar.showMessage("Starting endorsement process...")
        
        # Start endorsement
        self.download_thread.start()

    def _handle_endorsement_completion(self, message: str, statistics: Dict[str, Any]):
        """
        Handle endorsement operation completion.
        
        Args:
            message: Completion message
            statistics: Final endorsement statistics
        """
        self.start_button.setEnabled(True)
        self.pause_button.setEnabled(False)
        self.pause_button.setText("Pause")
        self.cancel_button.setEnabled(False)
        
        QMessageBox.information(
            self,
            "Endorsement Complete",
            "Thank you for endorsing the mods and supporting the mod authors!"
        )
        
        self.status_bar.showMessage("Endorsement completed")

    def _handle_log_message(self, level: str, message: str):
        """
        Handle log messages from the download thread asynchronously.
        
        Args:
            level: Log level (INFO, WARNING, ERROR, etc.)
            message: Log message content
        """
        if hasattr(self, 'async_log_handler') and self.async_log_handler:
            # Use async logging to prevent UI blocking
            self.async_log_handler.handle_log_message(level, message)
        elif self.logger:
            # Fallback to synchronous logging if async not available
            if hasattr(self.logger, 'logger'):
                log_level = getattr(logging, level, logging.INFO)
                self.logger.logger.log(log_level, message)
            else:
                # Fallback for standard logger
                log_level = getattr(logging, level, logging.INFO)
                self.logger.log(log_level, message)

    def _is_vortex_running(self) -> bool:
        """
        Check if Vortex is currently running.
        
        Returns:
            True if Vortex process is detected
        """
        try:
            result = subprocess.run(
                ['tasklist', '/FI', 'IMAGENAME eq Vortex.exe'],
                capture_output=True, text=True, timeout=5
            )
            return "Vortex.exe" in result.stdout
        except Exception:
            return False

    def _close_vortex_automatically(self) -> bool:
        """
        Automatically close Vortex processes.
        
        Returns:
            True if Vortex was successfully closed
        """
        try:
            subprocess.run(['taskkill', '/F', '/IM', 'Vortex.exe'], 
                         capture_output=True, timeout=10)
            time.sleep(2)
            
            if not self._is_vortex_running():
                QMessageBox.information(self, "Vortex Closed", 
                                      "Vortex has been closed successfully.")
                return True
            else:
                QMessageBox.warning(self, "Close Failed", 
                                  "Could not close Vortex automatically. Please close it manually.")
                return False
        except Exception as e:
            QMessageBox.warning(self, "Close Error", 
                              f"Failed to close Vortex: {str(e)}")
            return False

    def _open_settings_dialog(self):
        """Open the application settings dialog."""
        dialog = SettingsDialog(self.config_manager, self)
        if dialog.exec():
            self.status_bar.showMessage("Configuration updated", 2000)
            # Update progress monitor with new max threads setting
            if hasattr(self, 'progress_monitor') and self.config_manager:
                new_max_threads = self.config_manager.get_config().downloads.max_concurrent_downloads
                self.progress_monitor.set_max_threads(new_max_threads)

    def _export_configuration(self):
        """Export current configuration to a file."""
        if not self.config_manager:
            QMessageBox.warning(self, "Export Failed", 
                              "Configuration manager not available.")
            return
            
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Configuration",
            "nexusdownloader_config.json",
            "JSON Files (*.json);;All Files (*)"
        )
        
        if file_path:
            try:
                success = self.config_manager.export_config(file_path, include_sensitive=False)
                if success:
                    QMessageBox.information(self, "Export Successful", 
                                          f"Configuration exported to:\n{file_path}")
                else:
                    QMessageBox.warning(self, "Export Failed", 
                                      "Failed to export configuration.")
            except Exception as e:
                QMessageBox.critical(self, "Export Error", 
                                   f"Export failed: {str(e)}")

    def _import_configuration(self):
        """Import configuration from a file."""
        if not self.config_manager:
            QMessageBox.warning(self, "Import Failed", 
                              "Configuration manager not available.")
            return
            
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Configuration",
            "",
            "JSON Files (*.json);;All Files (*)"
        )
        
        if file_path:
            reply = QMessageBox.question(
                self,
                "Import Configuration",
                "This will replace your current configuration. Continue?",
                QMessageBox.Yes | QMessageBox.No
            )
            
            if reply == QMessageBox.Yes:
                try:
                    success = self.config_manager.import_config(file_path, preserve_api_key=True)
                    if success:
                        QMessageBox.information(self, "Import Successful", 
                                              "Configuration imported successfully.")
                    else:
                        QMessageBox.warning(self, "Import Failed", 
                                          "Failed to import configuration.")
                except Exception as e:
                    QMessageBox.critical(self, "Import Error", 
                                       f"Import failed: {str(e)}")

    def _show_about_dialog(self):
        """Show the application about dialog."""
        about_text = f"""
        <h3>NexusDownloader</h3>
        <p>Advanced mod collection downloader for Nexus Mods</p>
        
        <p><b>Features:</b></p>
        <ul>
        <li>Multi-threaded download management</li>
        <li>Real-time progress tracking</li>
        <li>Comprehensive configuration system</li>
        <li>Secure API key storage</li>
        <li>Vortex integration</li>
        <li>Performance monitoring</li>
        </ul>
        
        <p><b>Phase 1 Modules:</b> {'Available' if PHASE1_AVAILABLE else 'Not Available'}</p>
        """
        
        QMessageBox.about(self, "About NexusDownloader", about_text)

    def closeEvent(self, event):
        """
        Handle application close event with download operation check.
        
        Args:
            event: Qt close event
        """
        if self.download_thread and self.download_thread.isRunning():
            reply = QMessageBox.question(
                self,
                "Close Application",
                "A download operation is in progress. Cancel and exit?",
                QMessageBox.Yes | QMessageBox.No
            )
            
            if reply == QMessageBox.Yes:
                self.download_thread.cancel_operation()
                self.download_thread.wait(5000)  # Wait up to 5 seconds
                self._cleanup_async_logger()
                event.accept()
            else:
                event.ignore()
        else:
            self._cleanup_async_logger()
            event.accept()
    
    def _cleanup_async_logger(self):
        """Clean up async logger resources."""
        if hasattr(self, 'async_log_handler') and self.async_log_handler:
            try:
                self.async_log_handler.flush()  # Flush pending messages
                self.async_log_handler.stop()   # Stop the background thread
            except Exception as e:
                # Can't use self.log_message_received here as it may be disconnected
                import sys
                print(f"Error cleaning up async logger: {e}", file=sys.stderr)
    
    def get_logging_stats(self) -> dict:
        """
        Get current async logging statistics for monitoring.
        
        Returns:
            Dictionary with logging statistics
        """
        if hasattr(self, 'async_log_handler') and self.async_log_handler:
            return self.async_log_handler.get_stats()
        return {'async_logging': False}


def main():
    """Main entry point for the GUI application."""
    app = QApplication(sys.argv)
    
    # Set application metadata
    app.setApplicationName("NexusDownloader")
    app.setApplicationVersion("2.0")
    app.setOrganizationName("NexusDownloader")
    
    # Create and show main window
    window = MainWindow()
    window.show()
    
    # Start application event loop
    sys.exit(app.exec())


if __name__ == "__main__":
    """Entry point when run directly."""
    main()