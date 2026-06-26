"""
Progress tracking system for multi-threaded download operations.

This module provides comprehensive progress tracking capabilities for concurrent
file downloads, including individual file progress, overall statistics, and
real-time updates through callback mechanisms.
"""

import threading
import time
import json
import queue
import logging
from typing import Dict, List, Optional, Callable, Any
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from collections import deque


@dataclass
class DownloadProgress:
    """
    Progress information for a single file download.
    
    Tracks all relevant metrics for an individual download operation including
    progress percentage, transfer speed, estimated completion time, and status.
    """
    mod_id: int
    file_id: int
    filename: str
    total_size: int = 0
    downloaded_size: int = 0
    download_speed: float = 0.0  # bytes per second
    eta_seconds: float = 0.0
    status: str = "waiting"  # waiting, downloading, completed, failed, skipped
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    error_message: str = ""
    thread_id: str = ""
    
    @property
    def progress_percent(self) -> float:
        """
        Calculate progress percentage.
        
        Returns:
            Progress as a percentage (0.0 to 100.0)
        """
        if self.total_size == 0:
            return 0.0
        return (self.downloaded_size / self.total_size) * 100
    
    @property
    def elapsed_time(self) -> float:
        """
        Calculate elapsed download time.
        
        Returns:
            Elapsed time in seconds
        """
        if not self.start_time:
            return 0.0
        end = self.end_time or time.time()
        return end - self.start_time


class ProgressStatistics:
    """
    Container for overall progress statistics.
    
    Aggregates statistics from multiple concurrent downloads to provide
    comprehensive overview of the download operation progress.
    """
    
    def __init__(self):
        """Initialize statistics with default values."""
        self.reset()
    
    def reset(self):
        """Reset all statistics to initial values."""
        self.total_files = 0
        self.completed_files = 0
        self.failed_files = 0
        self.skipped_files = 0
        self.downloading_files = 0
        self.waiting_files = 0
        self.total_downloaded_bytes = 0
        self.total_size_bytes = 0
        self.overall_speed = 0.0
        self.start_time = time.time()
    
    def calculate_progress_percentage(self) -> float:
        """
        Calculate overall progress percentage by files.
        
        Returns:
            Overall progress as percentage (0.0 to 100.0)
        """
        if self.total_files == 0:
            return 0.0
        completed_and_skipped = self.completed_files + self.skipped_files
        return (completed_and_skipped / self.total_files) * 100
    
    def calculate_bytes_percentage(self) -> float:
        """
        Calculate progress percentage by bytes downloaded.
        
        Returns:
            Bytes progress as percentage (0.0 to 100.0)
        """
        if self.total_size_bytes == 0:
            return 0.0
        return (self.total_downloaded_bytes / self.total_size_bytes) * 100
    
    def calculate_eta(self) -> str:
        """
        Calculate estimated time to completion.
        
        Returns:
            Formatted ETA string
        """
        remaining_files = (self.total_files - self.completed_files - 
                          self.failed_files - self.skipped_files)
        
        if remaining_files <= 0 or self.overall_speed <= 0:
            return "Unknown"
        
        remaining_bytes = self.total_size_bytes - self.total_downloaded_bytes
        if remaining_bytes > 0:
            eta_seconds = remaining_bytes / self.overall_speed
            return str(timedelta(seconds=int(eta_seconds)))
        
        return "Unknown"
    
    def get_elapsed_time(self) -> str:
        """
        Get formatted elapsed time.
        
        Returns:
            Formatted elapsed time string
        """
        elapsed = time.time() - self.start_time
        return str(timedelta(seconds=int(elapsed)))


class ProgressTracker:
    """
    Multi-threaded progress tracker for download operations.
    
    Provides thread-safe tracking of multiple concurrent downloads with
    real-time statistics calculation and callback notifications.
    """
    
    def __init__(self, progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        """
        Initialize the progress tracker.
        
        Args:
            progress_callback: Optional callback function for progress updates
        """
        self._files: Dict[str, DownloadProgress] = {}
        self._lock = threading.RLock()
        self._progress_callback = progress_callback
        self._statistics = ProgressStatistics()
        self.logger = logging.getLogger(__name__)
        
        # Update queue and background thread
        self._update_queue = queue.Queue()
        self._update_thread = None
        self._stop_updates = False
        
        self.logger.debug("ProgressTracker initialized")
        
    def start_tracking(self):
        """Start the background progress update thread."""
        self._stop_updates = False
        self._update_thread = threading.Thread(target=self._update_loop, daemon=True)
        self._update_thread.start()
    
    def stop_tracking(self):
        """Stop the background progress update thread."""
        self._stop_updates = True
        if self._update_thread:
            self._update_thread.join(timeout=1.0)
    
    def register_file(self, mod_id: int, file_id: int, filename: str, total_size: int = 0):
        """
        Register a new file for progress tracking.
        
        Args:
            mod_id: Mod identifier
            file_id: File identifier
            filename: Name of the file
            total_size: Total file size in bytes
        """
        file_key = f"{mod_id}_{file_id}"
        
        with self._lock:
            self._files[file_key] = DownloadProgress(
                mod_id=mod_id,
                file_id=file_id,
                filename=filename,
                total_size=total_size,
                status="waiting"
            )
            self._statistics.total_files += 1
            self._statistics.total_size_bytes += total_size
            self._statistics.waiting_files += 1
        
        self._queue_update("file_registered", file_key)
    
    def start_download(self, mod_id: int, file_id: int, thread_id: str = ""):
        """
        Mark a file as starting download.
        
        Args:
            mod_id: Mod identifier
            file_id: File identifier
            thread_id: Identifier of the downloading thread
        """
        file_key = f"{mod_id}_{file_id}"
        
        with self._lock:
            if file_key in self._files:
                file_info = self._files[file_key]
                file_info.status = "downloading"
                file_info.start_time = time.time()
                file_info.thread_id = thread_id
                
                self._statistics.waiting_files -= 1
                self._statistics.downloading_files += 1
        
        self._queue_update("download_started", file_key)
    
    def update_progress(self, mod_id: int, file_id: int, downloaded_bytes: int, 
                       total_bytes: Optional[int] = None, speed_bps: float = 0.0):
        """
        Update download progress for a specific file.
        
        Args:
            mod_id: Mod identifier
            file_id: File identifier
            downloaded_bytes: Number of bytes downloaded
            total_bytes: Total file size (if known)
            speed_bps: Download speed in bytes per second
        """
        file_key = f"{mod_id}_{file_id}"
        
        with self._lock:
            if file_key in self._files:
                file_info = self._files[file_key]
                
                # Update file progress
                old_downloaded = file_info.downloaded_size
                file_info.downloaded_size = downloaded_bytes
                file_info.download_speed = speed_bps
                
                if total_bytes is not None and total_bytes > file_info.total_size:
                    # Update total size if better information is available
                    size_diff = total_bytes - file_info.total_size
                    file_info.total_size = total_bytes
                    self._statistics.total_size_bytes += size_diff
                
                # Calculate ETA for this file
                if speed_bps > 0 and file_info.total_size > downloaded_bytes:
                    remaining_bytes = file_info.total_size - downloaded_bytes
                    file_info.eta_seconds = remaining_bytes / speed_bps
                
                # Update overall statistics
                bytes_diff = downloaded_bytes - old_downloaded
                self._statistics.total_downloaded_bytes += bytes_diff
        
        self._queue_update("progress_updated", file_key)
    
    def complete_download(self, mod_id: int, file_id: int, success: bool = True, 
                         error_message: str = ""):
        """
        Mark a file download as completed.
        
        Args:
            mod_id: Mod identifier
            file_id: File identifier
            success: Whether the download completed successfully
            error_message: Error message if download failed
        """
        file_key = f"{mod_id}_{file_id}"
        
        with self._lock:
            if file_key in self._files:
                file_info = self._files[file_key]
                file_info.end_time = time.time()
                file_info.error_message = error_message
                
                if success:
                    file_info.status = "completed"
                    file_info.downloaded_size = file_info.total_size
                    self._statistics.completed_files += 1
                    self.logger.info(f"Download completed successfully: {file_info.filename} (mod {mod_id})")
                else:
                    file_info.status = "failed"
                    self._statistics.failed_files += 1
                    self.logger.warning(f"Download failed: {file_info.filename} (mod {mod_id}) - {error_message}")
                
                self._statistics.downloading_files -= 1
        
        self._queue_update("download_completed", file_key)
    
    def skip_download(self, mod_id: int, file_id: int, reason: str = ""):
        """
        Mark a file download as skipped.
        
        Args:
            mod_id: Mod identifier
            file_id: File identifier
            reason: Reason for skipping the download
        """
        file_key = f"{mod_id}_{file_id}"
        
        with self._lock:
            if file_key in self._files:
                file_info = self._files[file_key]
                old_status = file_info.status
                file_info.status = "skipped"
                file_info.error_message = reason
                file_info.end_time = time.time()
                
                self._statistics.skipped_files += 1
                if old_status == "waiting":
                    self._statistics.waiting_files -= 1
                else:
                    self._statistics.downloading_files -= 1
        
        self._queue_update("download_skipped", file_key)
    
    def get_overall_progress(self) -> Dict[str, Any]:
        """
        Get comprehensive overall progress statistics.
        
        Returns:
            Dictionary containing all progress statistics
        """
        with self._lock:
            # Update overall speed calculation
            active_speeds = [
                file_info.download_speed for file_info in self._files.values()
                if file_info.status == "downloading" and file_info.download_speed > 0
            ]
            self._statistics.overall_speed = sum(active_speeds)
            
            return {
                'total_files': self._statistics.total_files,
                'completed_files': self._statistics.completed_files,
                'failed_files': self._statistics.failed_files,
                'skipped_files': self._statistics.skipped_files,
                'downloading_files': self._statistics.downloading_files,
                'waiting_files': self._statistics.waiting_files,
                'total_downloaded_bytes': self._statistics.total_downloaded_bytes,
                'total_size_bytes': self._statistics.total_size_bytes,
                'overall_speed': self._statistics.overall_speed,
                'overall_progress_percent': self._statistics.calculate_progress_percentage(),
                'bytes_progress_percent': self._statistics.calculate_bytes_percentage(),
                'eta_formatted': self._statistics.calculate_eta(),
                'elapsed_formatted': self._statistics.get_elapsed_time(),
                'elapsed_seconds': time.time() - self._statistics.start_time
            }
    
    def get_active_downloads(self) -> List[DownloadProgress]:
        """
        Get list of currently active downloads.
        
        Returns:
            List of DownloadProgress objects for active downloads
        """
        with self._lock:
            return [
                file_info for file_info in self._files.values() 
                if file_info.status == "downloading"
            ]
    
    def get_file_progress(self, mod_id: int, file_id: int) -> Optional[DownloadProgress]:
        """
        Get progress information for a specific file.
        
        Args:
            mod_id: Mod identifier
            file_id: File identifier
            
        Returns:
            DownloadProgress object or None if not found
        """
        file_key = f"{mod_id}_{file_id}"
        with self._lock:
            return self._files.get(file_key)
    
    def get_all_files(self) -> List[DownloadProgress]:
        """
        Get all tracked files.
        
        Returns:
            List of all DownloadProgress objects
        """
        with self._lock:
            return list(self._files.values())
    
    def _queue_update(self, event_type: str, file_key: str):
        """
        Queue a progress update event for background processing.
        
        Args:
            event_type: Type of update event
            file_key: File identifier key
        """
        try:
            self._update_queue.put_nowait({
                'event_type': event_type,
                'file_key': file_key,
                'timestamp': time.time()
            })
        except queue.Full:
            pass  # Skip update if queue is full
    
    def _update_loop(self):
        """
        Background thread loop for processing progress updates.
        
        Runs continuously until stop_tracking() is called, processing
        queued updates and invoking progress callbacks.
        """
        while not self._stop_updates:
            try:
                # Process all queued updates
                while not self._update_queue.empty():
                    update = self._update_queue.get_nowait()
                    
                    if self._progress_callback:
                        overall_progress = self.get_overall_progress()
                        active_downloads = self.get_active_downloads()
                        
                        self._progress_callback({
                            'event': update,
                            'overall_progress': overall_progress,
                            'active_downloads': active_downloads
                        })
                
                time.sleep(0.1)  # Update frequency: 10 times per second
                
            except queue.Empty:
                time.sleep(0.1)
            except Exception as e:
                print(f"Error in progress update loop: {e}")
                time.sleep(0.5)
    
    def export_progress_log(self, filename: str):
        """
        Export current progress state to a JSON file.
        
        Args:
            filename: Output filename for the progress log
        """
        with self._lock:
            export_data = {
                'timestamp': datetime.now().isoformat(),
                'overall_statistics': asdict(self._statistics),
                'files': {
                    key: asdict(file_info) 
                    for key, file_info in self._files.items()
                }
            }
        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False)


def create_progress_callback_for_gui():
    """
    Create a progress callback function suitable for GUI integration.
    
    Returns:
        Callback function that formats progress data for GUI display
    """
    def progress_callback(update_data: Dict[str, Any]):
        """
        Progress callback function for GUI integration.
        
        Args:
            update_data: Progress update data containing overall stats and active downloads
        """
        overall = update_data['overall_progress']
        active = update_data['active_downloads']
        
        # Format progress information for display
        progress_text = f"{overall['completed_files']} / {overall['total_files']} files"
        speed_text = f"{overall['overall_speed'] / (1024*1024):.2f} MB/s" if overall['overall_speed'] > 0 else "Calculating..."
        eta_text = overall['eta_formatted']
        
        # Output current status (for console/log monitoring) - reduced frequency
        if active and hasattr(progress_callback, '_last_console_output'):
            import time
            current_time = time.time()
            if current_time - progress_callback._last_console_output >= 5.0:  # Only every 5 seconds
                print(f"PROGRESS: {progress_text} | Speed: {speed_text} | ETA: {eta_text}")
                progress_callback._last_console_output = current_time
        elif active and not hasattr(progress_callback, '_last_console_output'):
            # First time - set up the throttling
            import time
            progress_callback._last_console_output = time.time()
            print(f"PROGRESS: {progress_text} | Speed: {speed_text} | ETA: {eta_text}")
    
    return progress_callback