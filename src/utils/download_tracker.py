import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import Dict, Optional, Tuple


class DownloadStatus(Enum):
    WAITING = "waiting"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class DownloadInfo:
    mod_id: str
    file_id: str
    filename: str
    thread_name: str
    status: DownloadStatus = DownloadStatus.WAITING
    start_time: Optional[float] = None
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed_bps: float = 0
    last_update_time: Optional[float] = None
    error_message: str = ""

    @property
    def progress(self) -> float:
        """Return progress as a value between 0 and 1"""
        if self.total_bytes <= 0:
            return 0
        return min(1.0, self.downloaded_bytes / self.total_bytes)

    @property
    def progress_percent(self) -> float:
        """Return progress as a percentage"""
        return self.progress * 100

    @property
    def eta_seconds(self) -> Optional[float]:
        """Estimate time remaining in seconds"""
        if self.speed_bps <= 0 or self.total_bytes <= 0:
            return None

        remaining_bytes = self.total_bytes - self.downloaded_bytes
        if remaining_bytes <= 0:
            return 0

        return remaining_bytes / self.speed_bps

    @property
    def eta_formatted(self) -> str:
        """Return formatted ETA as MM:SS"""
        eta = self.eta_seconds
        if eta is None:
            return "--:--"

        eta_td = timedelta(seconds=int(eta))
        if eta_td.days > 0:
            hours = eta_td.days * 24 + eta_td.seconds // 3600
            minutes = (eta_td.seconds % 3600) // 60
            return f"{hours}:{minutes:02d}:{eta_td.seconds%60:02d}"
        elif eta_td.seconds >= 3600:
            return f"{eta_td.seconds//3600}:{(eta_td.seconds%3600)//60:02d}:{eta_td.seconds%60:02d}"
        else:
            return f"{eta_td.seconds//60:02d}:{eta_td.seconds%60:02d}"

    @property
    def speed_formatted(self) -> str:
        """Return formatted speed (KB/s, MB/s, etc)"""
        if self.speed_bps < 1024:
            return f"{self.speed_bps:.1f} B/s"
        elif self.speed_bps < 1024 * 1024:
            return f"{self.speed_bps/1024:.1f} KB/s"
        elif self.speed_bps < 1024 * 1024 * 1024:
            return f"{self.speed_bps/(1024*1024):.1f} MB/s"
        else:
            return f"{self.speed_bps/(1024*1024*1024):.1f} GB/s"

    @property
    def elapsed_seconds(self) -> Optional[float]:
        """Get elapsed time in seconds"""
        if not self.start_time:
            return None

        end_time = time.time()
        return end_time - self.start_time

    @property
    def elapsed_formatted(self) -> str:
        """Return formatted elapsed time"""
        elapsed = self.elapsed_seconds
        if elapsed is None:
            return "00:00"

        elapsed_td = timedelta(seconds=int(elapsed))
        if elapsed_td.days > 0:
            hours = elapsed_td.days * 24 + elapsed_td.seconds // 3600
            minutes = (elapsed_td.seconds % 3600) // 60
            return f"{hours}:{minutes:02d}:{elapsed_td.seconds%60:02d}"
        elif elapsed_td.seconds >= 3600:
            return f"{elapsed_td.seconds//3600}:{(elapsed_td.seconds%3600)//60:02d}:{elapsed_td.seconds%60:02d}"
        else:
            return f"{elapsed_td.seconds//60:02d}:{elapsed_td.seconds%60:02d}"


class MultiThreadProgressTracker:
    """Tracks download progress across multiple threads"""

    def __init__(self, update_interval: float = 0.1):
        self.downloads: Dict[Tuple[str, str], DownloadInfo] = {}
        self.lock = threading.RLock()
        self.update_interval = update_interval
        self.running = False
        self.update_thread = None
        self.callbacks = []

    def register_callback(self, callback):
        """Register a callback function to be called on updates"""
        self.callbacks.append(callback)

    def start_tracking(self):
        """Start the background update thread"""
        if self.running:
            return

        self.running = True
        self.update_thread = threading.Thread(target=self._update_loop, daemon=True)
        self.update_thread.start()

    def stop_tracking(self):
        """Stop the background update thread"""
        self.running = False
        if self.update_thread and self.update_thread.is_alive():
            self.update_thread.join(timeout=1.0)

    def _update_loop(self):
        """Background thread that periodically calls the update callbacks"""
        while self.running:
            self._notify_callbacks()
            time.sleep(self.update_interval)

    def _notify_callbacks(self):
        """Notify all registered callbacks with current download status"""
        if not self.callbacks:
            return

        with self.lock:
            # Create a copy to avoid callbacks modifying the dict during iteration
            downloads_copy = {k: v for k, v in self.downloads.items()}

        for callback in self.callbacks:
            try:
                callback(downloads_copy)
            except Exception as e:
                print(f"Error in callback: {e}")

    def start_download(self, mod_id: str, file_id: str, filename: str, thread_name: str, total_bytes: int = 0):
        """Register a new download"""
        with self.lock:
            key = (mod_id, file_id)
            self.downloads[key] = DownloadInfo(
                mod_id=mod_id,
                file_id=file_id,
                filename=filename,
                thread_name=thread_name,
                status=DownloadStatus.DOWNLOADING,
                start_time=time.time(),
                total_bytes=total_bytes,
                last_update_time=time.time()
            )

    def update_progress(self, mod_id: str, file_id: str, downloaded_bytes: int, total_bytes: int, speed_bps: float = None):
        """Update progress for a specific download"""
        with self.lock:
            key = (mod_id, file_id)
            if key not in self.downloads:
                return

            download = self.downloads[key]
            download.downloaded_bytes = downloaded_bytes
            if total_bytes > 0:
                download.total_bytes = total_bytes

            current_time = time.time()

            # Calculate speed if not provided
            if speed_bps is None and download.last_update_time:
                time_diff = current_time - download.last_update_time
                if time_diff > 0:
                    # Exponential moving average for smoother speed display
                    if download.speed_bps > 0:
                        alpha = 0.3  # Smoothing factor
                        current_speed = (downloaded_bytes - download.downloaded_bytes) / time_diff
                        download.speed_bps = alpha * current_speed + (1 - alpha) * download.speed_bps
                    else:
                        download.speed_bps = downloaded_bytes / (current_time - download.start_time)
            elif speed_bps is not None:
                download.speed_bps = speed_bps

            download.last_update_time = current_time

    def complete_download(self, mod_id: str, file_id: str, success: bool = True, error_message: str = ""):
        """Mark a download as completed or failed"""
        with self.lock:
            key = (mod_id, file_id)
            if key not in self.downloads:
                return

            download = self.downloads[key]
            if success:
                download.status = DownloadStatus.COMPLETED
                # Ensure progress shows 100%
                if download.total_bytes > 0:
                    download.downloaded_bytes = download.total_bytes
            else:
                download.status = DownloadStatus.FAILED
                download.error_message = error_message

    def skip_download(self, mod_id: str, file_id: str, reason: str = ""):
        """Mark a download as skipped"""
        with self.lock:
            key = (mod_id, file_id)
            if key not in self.downloads:
                return

            download = self.downloads[key]
            download.status = DownloadStatus.SKIPPED
            download.error_message = reason

    def get_download_info(self, mod_id: str, file_id: str) -> Optional[DownloadInfo]:
        """Get information about a specific download"""
        with self.lock:
            key = (mod_id, file_id)
            return self.downloads.get(key)

    def get_all_downloads(self) -> Dict[Tuple[str, str], DownloadInfo]:
        """Get all downloads (thread-safe copy)"""
        with self.lock:
            return {k: v for k, v in self.downloads.items()}

    def get_active_downloads(self) -> Dict[Tuple[str, str], DownloadInfo]:
        """Get only the active downloads"""
        with self.lock:
            return {k: v for k, v in self.downloads.items() 
                   if v.status == DownloadStatus.DOWNLOADING}

    def get_overall_progress(self) -> Dict:
        """Calculate overall download progress statistics"""
        with self.lock:
            total_downloads = len(self.downloads)
            if total_downloads == 0:
                return {
                    "total_files": 0,
                    "completed_files": 0,
                    "active_files": 0,
                    "failed_files": 0,
                    "skipped_files": 0,
                    "waiting_files": 0,
                    "progress": 0,
                    "bytes_downloaded": 0,
                    "bytes_total": 0,
                    "overall_speed": 0,
                    "eta_seconds": None,
                    "eta_formatted": "--:--",
                    "elapsed_seconds": 0,
                    "elapsed_formatted": "00:00"
                }

            completed = sum(1 for d in self.downloads.values() if d.status == DownloadStatus.COMPLETED)
            active = sum(1 for d in self.downloads.values() if d.status == DownloadStatus.DOWNLOADING)
            failed = sum(1 for d in self.downloads.values() if d.status == DownloadStatus.FAILED)
            skipped = sum(1 for d in self.downloads.values() if d.status == DownloadStatus.SKIPPED)
            waiting = sum(1 for d in self.downloads.values() if d.status == DownloadStatus.WAITING)

            bytes_downloaded = sum(d.downloaded_bytes for d in self.downloads.values())
            bytes_total = sum(d.total_bytes for d in self.downloads.values() if d.total_bytes > 0)

            # Calculate overall progress
            if bytes_total > 0:
                progress = bytes_downloaded / bytes_total
            else:
                progress = 0 if active + waiting > 0 else 1

            # Calculate overall speed (only from active downloads)
            active_downloads = [d for d in self.downloads.values() if d.status == DownloadStatus.DOWNLOADING]
            overall_speed = sum(d.speed_bps for d in active_downloads)

            # Calculate ETA based on overall speed
            eta_seconds = None
            if overall_speed > 0 and bytes_total > bytes_downloaded:
                eta_seconds = (bytes_total - bytes_downloaded) / overall_speed

            # Calculate elapsed time (from the earliest start)
            start_times = [d.start_time for d in self.downloads.values() if d.start_time is not None]
            elapsed_seconds = 0
            if start_times:
                earliest_start = min(start_times)
                elapsed_seconds = time.time() - earliest_start

            # Format ETA
            if eta_seconds is None:
                eta_formatted = "--:--"
            else:
                eta_td = timedelta(seconds=int(eta_seconds))
                if eta_td.days > 0:
                    hours = eta_td.days * 24 + eta_td.seconds // 3600
                    minutes = (eta_td.seconds % 3600) // 60
                    eta_formatted = f"{hours}:{minutes:02d}:{eta_td.seconds%60:02d}"
                elif eta_td.seconds >= 3600:
                    eta_formatted = f"{eta_td.seconds//3600}:{(eta_td.seconds%3600)//60:02d}:{eta_td.seconds%60:02d}"
                else:
                    eta_formatted = f"{eta_td.seconds//60:02d}:{eta_td.seconds%60:02d}"

            # Format elapsed time
            elapsed_td = timedelta(seconds=int(elapsed_seconds))
            if elapsed_td.days > 0:
                hours = elapsed_td.days * 24 + elapsed_td.seconds // 3600
                minutes = (elapsed_td.seconds % 3600) // 60
                elapsed_formatted = f"{hours}:{minutes:02d}:{elapsed_td.seconds%60:02d}"
            elif elapsed_td.seconds >= 3600:
                elapsed_formatted = f"{elapsed_td.seconds//3600}:{(elapsed_td.seconds%3600)//60:02d}:{elapsed_td.seconds%60:02d}"
            else:
                elapsed_formatted = f"{elapsed_td.seconds//60:02d}:{elapsed_td.seconds%60:02d}"

            return {
                "total_files": total_downloads,
                "completed_files": completed,
                "active_files": active,
                "failed_files": failed,
                "skipped_files": skipped,
                "waiting_files": waiting,
                "progress": progress,
                "bytes_downloaded": bytes_downloaded,
                "bytes_total": bytes_total,
                "overall_speed": overall_speed,
                "eta_seconds": eta_seconds,
                "eta_formatted": eta_formatted,
                "elapsed_seconds": elapsed_seconds,
                "elapsed_formatted": elapsed_formatted
            }
