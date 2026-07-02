"""
Logging Configuration Module for NexusDownloader.

Provides comprehensive logging capabilities including file rotation, filtering,
performance monitoring, and intelligent error aggregation.
"""

from __future__ import annotations
import os
import sys
import time
import logging
import logging.handlers
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Dict, Optional, Any

if TYPE_CHECKING:
    from config.config_manager import LoggingConfig
from pathlib import Path
import threading
from collections import defaultdict, deque
import json


class PerformanceMetrics:
    """
    Tracks performance metrics for logging and monitoring.
    
    Collects and aggregates download performance data including bandwidth,
    success rates, and timing statistics for analysis and reporting.
    """
    
    def __init__(self, max_entries: int = 1000):
        self.max_entries = max_entries
        self.download_times = deque(maxlen=max_entries)
        self.error_counts = defaultdict(int)
        self.success_counts = defaultdict(int)
        self.bandwidth_samples = deque(maxlen=max_entries)
        self.start_time = time.time()
        self._lock = threading.Lock()
    
    def record_download(self, size_bytes: int, duration_seconds: float, 
                       status: str = "success"):
        """Record a download operation"""
        with self._lock:
            self.download_times.append({
                'timestamp': time.time(),
                'size_bytes': size_bytes,
                'duration_seconds': duration_seconds,
                'status': status,
                'bandwidth_mbps': (size_bytes / (1024 * 1024)) / duration_seconds if duration_seconds > 0 else 0
            })
            
            if status == "success":
                self.success_counts[datetime.now().strftime('%Y-%m-%d %H')] += 1
                self.bandwidth_samples.append(
                    (size_bytes / (1024 * 1024)) / duration_seconds if duration_seconds > 0 else 0
                )
            else:
                self.error_counts[status] += 1
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get comprehensive performance statistics"""
        with self._lock:
            current_time = time.time()
            recent_downloads = [
                d for d in self.download_times 
                if current_time - d['timestamp'] <= 3600  # Last hour
            ]
            
            successful_downloads = [d for d in recent_downloads if d['status'] == 'success']
            
            stats = {
                'uptime_seconds': current_time - self.start_time,
                'total_downloads': len(self.download_times),
                'recent_downloads_1h': len(recent_downloads),
                'successful_downloads': len(successful_downloads),
                'error_counts': dict(self.error_counts),
                'average_bandwidth_mbps': sum(self.bandwidth_samples) / len(self.bandwidth_samples) if self.bandwidth_samples else 0,
                'peak_bandwidth_mbps': max(self.bandwidth_samples) if self.bandwidth_samples else 0,
                'total_data_downloaded_mb': sum(d['size_bytes'] for d in self.download_times) / (1024 * 1024)
            }
            
            if successful_downloads:
                stats['average_download_time'] = sum(d['duration_seconds'] for d in successful_downloads) / len(successful_downloads)
                stats['fastest_download_time'] = min(d['duration_seconds'] for d in successful_downloads)
                stats['slowest_download_time'] = max(d['duration_seconds'] for d in successful_downloads)
            
            return stats


class ColoredFormatter(logging.Formatter):
    """
    Adds color coding to log messages for console output.
    
    Enhances readability by applying ANSI color codes to different log levels
    when outputting to terminals that support colored text.
    """
    
    # ANSI color codes
    COLORS = {
        'DEBUG': '\033[36m',     # Cyan
        'INFO': '\033[32m',      # Green
        'WARNING': '\033[33m',   # Yellow
        'ERROR': '\033[31m',     # Red
        'CRITICAL': '\033[35m',  # Magenta
        'RESET': '\033[0m'       # Reset
    }
    
    def format(self, record):
        # Add color to the level name
        levelname = record.levelname
        if levelname in self.COLORS:
            record.levelname = f"{self.COLORS[levelname]}{levelname}{self.COLORS['RESET']}"
        
        # Format the message
        formatted = super().format(record)
        
        # Reset the levelname for other formatters
        record.levelname = levelname
        
        return formatted


class DownloadProgressFilter(logging.Filter):
    """
    Filters repetitive download progress messages to reduce log spam.
    
    Implements intelligent filtering to prevent overwhelming the log with
    frequent progress updates while maintaining useful monitoring information.
    """
    
    def __init__(self, min_interval: float = 5.0):
        super().__init__()
        self.min_interval = min_interval
        self.last_progress_time = {}
    
    def filter(self, record):
        # Only filter progress messages
        if 'progress' not in record.getMessage().lower():
            return True
        
        # Extract identifier from the message (e.g., filename or mod ID)
        message = record.getMessage()
        identifier = self._extract_identifier(message)
        
        current_time = time.time()
        last_time = self.last_progress_time.get(identifier, 0)
        
        if current_time - last_time >= self.min_interval:
            self.last_progress_time[identifier] = current_time
            return True
        
        return False
    
    def _extract_identifier(self, message: str) -> str:
        """Extract a unique identifier from the progress message"""
        # Simple heuristic - use first 50 characters as identifier
        return message[:50]


class ErrorAggregatorHandler(logging.Handler):
    """
    Aggregates similar errors to prevent log spam.
    
    Groups similar error messages within a time window to prevent log flooding
    while ensuring critical error information is still captured and reported.
    """
    
    def __init__(self, max_similar_errors: int = 5, time_window: int = 300):
        super().__init__()
        self.max_similar_errors = max_similar_errors
        self.time_window = time_window
        self.error_counts = defaultdict(list)
        self._lock = threading.Lock()
    
    def emit(self, record):
        if record.levelno < logging.ERROR:
            return
        
        error_signature = self._get_error_signature(record)
        current_time = time.time()
        
        with self._lock:
            # Clean old entries
            self.error_counts[error_signature] = [
                timestamp for timestamp in self.error_counts[error_signature]
                if current_time - timestamp <= self.time_window
            ]
            
            # Add current error
            self.error_counts[error_signature].append(current_time)
            
            # Check if we should suppress this error
            count = len(self.error_counts[error_signature])
            if count <= self.max_similar_errors:
                # Let the error through
                pass
            elif count == self.max_similar_errors + 1:
                # Send suppression message
                suppression_record = logging.LogRecord(
                    name=record.name,
                    level=logging.WARNING,
                    pathname=record.pathname,
                    lineno=record.lineno,
                    msg=f"Suppressing similar errors (signature: {error_signature[:50]}...)",
                    args=(),
                    exc_info=None
                )
                super().emit(suppression_record)
    
    def _get_error_signature(self, record) -> str:
        """Generate a signature for the error to group similar errors"""
        # Use message template and exception type as signature
        message = record.getMessage()
        exc_type = record.exc_info[0].__name__ if record.exc_info else "NoException"
        return f"{exc_type}:{message[:100]}"


class NexusLogger:
    """
    Main logging configuration class for NexusDownloader.
    
    Provides centralized logging configuration with multiple handlers,
    performance tracking, and specialized logging methods for download operations.
    """

    @staticmethod
    def _resolve_log_dir(requested: str) -> Path:
        """Resolve a usable log directory, tolerating stale/foreign config paths.

        A machine-specific absolute path (e.g. a leftover ``X:/_work/...`` from a
        prior checkout location) must never brick startup. Relative values resolve
        against the *project root* (this file is ``src/utils/logging_config.py``, so
        the root is ``parents[2]``) rather than the CWD, so the GUI (launched from
        the repo root) and the CLI (launched from ``src/``) agree. We try, in order:
        the requested dir, the project-root ``logs``, then a per-user fallback."""
        try:
            project_root = Path(__file__).resolve().parents[2]
        except Exception:
            project_root = Path.cwd()

        def _drive_style(s: str) -> bool:
            # "X:/..." or "X:\\..." — a Windows drive-qualified path.
            return len(s) >= 2 and s[1] == ":" and s[0].isalpha()

        candidates = []
        if requested and not (_drive_style(requested) and os.name != "nt"):
            # (A Windows drive path is meaningless on a POSIX host — skip it so we
            #  don't accidentally create a literal "X:" folder under the CWD.)
            p = Path(requested)
            candidates.append(p if p.is_absolute() else (project_root / p))
        candidates.append(project_root / "logs")
        candidates.append(Path.home() / ".nexusdownloader" / "logs")

        for cand in candidates:
            try:
                cand.mkdir(parents=True, exist_ok=True)
                return cand
            except Exception:
                continue
        # Last resort: CWD-relative, so we always return a writable directory.
        fallback = Path.cwd() / "logs"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback

    def __init__(self, log_level: int = logging.INFO,
                 log_dir: str = "logs",
                 max_file_size: int = 10 * 1024 * 1024,  # 10MB
                 backup_count: int = 5,
                 enable_console: bool = True,
                 enable_colors: bool = True,
                 enable_performance_logging: bool = True):
        
        self.log_level = log_level
        self.max_file_size = max_file_size
        self.backup_count = backup_count
        self.enable_console = enable_console
        self.enable_colors = enable_colors and self._supports_color()

        # Resolve + create log directory (robust against stale/foreign paths in config)
        self.log_dir = self._resolve_log_dir(log_dir)
        
        # Initialize performance metrics
        self.performance_metrics = PerformanceMetrics() if enable_performance_logging else None
        
        # Setup logging
        self.logger = self._setup_logger()
        
        # Setup performance logging if enabled
        if enable_performance_logging:
            self._setup_performance_logging()
    
    def _supports_color(self) -> bool:
        """Check if the terminal supports color output"""
        return (
            hasattr(sys.stderr, "isatty") and sys.stderr.isatty() and
            os.environ.get('TERM') != 'dumb'
        )
    
    def _setup_logger(self) -> logging.Logger:
        """Setup the main logger with all handlers"""
        logger = logging.getLogger('nexusdownloader')
        logger.setLevel(self.log_level)
        
        # Clear any existing handlers
        logger.handlers.clear()
        
        # Setup file handlers
        self._setup_file_handlers(logger)
        
        # Setup console handler
        if self.enable_console:
            self._setup_console_handler(logger)
        
        # Setup error aggregation
        self._setup_error_aggregation(logger)
        
        return logger
    
    def _setup_file_handlers(self, logger: logging.Logger):
        """Setup rotating file handlers"""
        
        # Import custom rotating handler
        from .unified_logging import CustomRotatingFileHandler
        
        # Main log file (all messages) with proper rotation naming
        main_handler = CustomRotatingFileHandler(
            self.log_dir / "nexusdownloader.log",
            maxBytes=self.max_file_size,
            backupCount=self.backup_count,
            encoding='utf-8'
        )
        main_handler.setLevel(logging.DEBUG)
        main_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        logger.addHandler(main_handler)
        
        # Error log file (errors and warnings only) with proper rotation naming
        error_handler = CustomRotatingFileHandler(
            self.log_dir / "nexusdownloader_errors.log",
            maxBytes=self.max_file_size,
            backupCount=self.backup_count,
            encoding='utf-8'
        )
        error_handler.setLevel(logging.WARNING)
        error_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s\n'
            'Exception: %(exc_info)s\n' + '-' * 80,
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        logger.addHandler(error_handler)
        
        # Performance log file (if enabled) with proper rotation naming
        if self.performance_metrics:
            perf_handler = CustomRotatingFileHandler(
                self.log_dir / "nexusdownloader_performance.log",
                maxBytes=self.max_file_size,
                backupCount=self.backup_count,
                encoding='utf-8'
            )
            perf_handler.setLevel(logging.INFO)
            perf_handler.setFormatter(logging.Formatter(
                '%(asctime)s - %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            ))
            # Add filter to only log performance messages
            perf_handler.addFilter(lambda record: 'PERF:' in record.getMessage())
            logger.addHandler(perf_handler)
    
    def _setup_console_handler(self, logger: logging.Logger):
        """Setup console output handler"""
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        
        # Use colored formatter if colors are enabled
        if self.enable_colors:
            formatter = ColoredFormatter(
                '%(asctime)s - %(levelname)s - %(message)s',
                datefmt='%H:%M:%S'
            )
        else:
            formatter = logging.Formatter(
                '%(asctime)s - %(levelname)s - %(message)s',
                datefmt='%H:%M:%S'
            )
        
        console_handler.setFormatter(formatter)
        
        # Add progress filter to reduce spam
        console_handler.addFilter(DownloadProgressFilter(min_interval=2.0))
        
        logger.addHandler(console_handler)
    
    def _setup_error_aggregation(self, logger: logging.Logger):
        """Setup error aggregation to prevent spam"""
        error_aggregator = ErrorAggregatorHandler(
            max_similar_errors=3,
            time_window=300  # 5 minutes
        )
        logger.addHandler(error_aggregator)
    
    def _setup_performance_logging(self):
        """Setup automatic performance logging"""
        def log_performance_stats():
            if self.performance_metrics:
                stats = self.performance_metrics.get_statistics()
                self.logger.info(f"PERF: {json.dumps(stats, indent=2)}")
        
        # Log performance stats every 10 minutes
        def performance_logger():
            while True:
                time.sleep(600)  # 10 minutes
                try:
                    log_performance_stats()
                except Exception as e:
                    self.logger.error(f"Failed to log performance stats: {e}")
        
        # Start performance logging thread
        perf_thread = threading.Thread(target=performance_logger, daemon=True)
        perf_thread.start()
    
    def log_download_start(self, mod_name: str, file_size: int, url: str):
        """Log the start of a download"""
        self.logger.info(
            f"Starting download: {mod_name} "
            f"(Size: {file_size / (1024*1024):.1f} MB) "
            f"from {url[:50]}..."
        )
    
    def log_download_progress(self, mod_name: str, progress_percent: float, 
                            speed_mbps: float):
        """Log download progress"""
        self.logger.debug(
            f"Download progress: {mod_name} - "
            f"{progress_percent:.1f}% "
            f"(Speed: {speed_mbps:.1f} MB/s)"
        )
    
    def log_download_complete(self, mod_name: str, file_size: int, 
                            duration: float, success: bool = True):
        """Log download completion"""
        if success:
            speed = (file_size / (1024 * 1024)) / duration if duration > 0 else 0
            self.logger.info(
                f"Download completed: {mod_name} "
                f"(Size: {file_size / (1024*1024):.1f} MB, "
                f"Duration: {duration:.1f}s, "
                f"Speed: {speed:.1f} MB/s)"
            )
            
            # Record performance metrics
            if self.performance_metrics:
                self.performance_metrics.record_download(
                    file_size, duration, "success"
                )
        else:
            self.logger.error(f"Download failed: {mod_name}")
            if self.performance_metrics:
                self.performance_metrics.record_download(
                    file_size, duration, "failed"
                )
    
    def log_error_with_context(self, error: Exception, context: Dict[str, Any]):
        """Log an error with additional context"""
        context_str = ", ".join(f"{k}={v}" for k, v in context.items())
        self.logger.error(
            f"Error occurred: {error} (Context: {context_str})",
            exc_info=True
        )
    
    def get_performance_stats(self) -> Optional[Dict[str, Any]]:
        """Get current performance statistics"""
        if self.performance_metrics:
            return self.performance_metrics.get_statistics()
        return None
    
    def cleanup_old_logs(self, days_to_keep: int = 30):
        """Clean up old log files"""
        cutoff_date = datetime.now() - timedelta(days=days_to_keep)
        
        for log_file in self.log_dir.glob("*.log*"):
            try:
                file_time = datetime.fromtimestamp(log_file.stat().st_mtime)
                if file_time < cutoff_date:
                    log_file.unlink()
                    self.logger.info(f"Cleaned up old log file: {log_file.name}")
            except Exception as e:
                self.logger.warning(f"Failed to clean up {log_file.name}: {e}")
    
    def export_logs_for_debugging(self, output_file: str):
        """Export recent logs for debugging purposes"""
        try:
            recent_logs = []
            
            # Read from main log file
            main_log = self.log_dir / "nexusdownloader.log"
            if main_log.exists():
                with open(main_log, 'r', encoding='utf-8') as f:
                    recent_logs.extend(f.readlines()[-1000:])  # Last 1000 lines
            
            # Read from error log file
            error_log = self.log_dir / "errors.log"
            if error_log.exists():
                with open(error_log, 'r', encoding='utf-8') as f:
                    recent_logs.extend(f.readlines())
            
            # Write combined logs
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write("=== NexusDownloader Debug Logs ===\n")
                f.write(f"Generated: {datetime.now().isoformat()}\n")
                f.write("=" * 50 + "\n\n")
                f.writelines(recent_logs)
            
            self.logger.info(f"Debug logs exported to: {output_file}")
            
        except Exception as e:
            self.logger.error(f"Failed to export debug logs: {e}")


# Global logger instance
_global_logger = None

def get_logger() -> NexusLogger:
    """Get the global logger instance"""
    global _global_logger
    if _global_logger is None:
        _global_logger = NexusLogger()
    return _global_logger

def setup_logging(config: Optional['LoggingConfig'] = None) -> NexusLogger:
    """Setup global logging configuration"""
    global _global_logger
    
    if config:
        log_level_str = config.log_level.upper()
        log_level = getattr(logging, log_level_str, logging.INFO)
        
        _global_logger = NexusLogger(
            log_level=log_level,
            log_dir=config.log_directory,
            max_file_size=config.max_log_size_mb * 1024 * 1024,
            backup_count=config.log_backup_count,
            enable_console=config.log_to_console,
            enable_colors=config.enable_colored_output,
            enable_performance_logging=config.enable_performance_logging
        )
    else:
        # Fallback to default if no config provided
        _global_logger = NexusLogger()
        
    return _global_logger


# Example usage
def example_usage():
    """Example of how to use the logging system"""
    
    # Setup logging
    from ..config.config_manager import LoggingConfig
    example_config = LoggingConfig(
        log_level="DEBUG", 
        log_to_console=True, 
        enable_colored_output=True, 
        enable_performance_logging=True
    )
    logger = setup_logging(example_config)
    
    # Test various log levels
    logger.logger.debug("This is a debug message")
    logger.logger.info("Starting NexusDownloader application")
    logger.logger.warning("This is a warning message")
    logger.logger.error("This is an error message")
    
    # Test download logging
    logger.log_download_start("TestMod.zip", 50 * 1024 * 1024, "https://example.com/mod.zip")
    
    # Simulate download progress
    for progress in [25, 50, 75, 100]:
        logger.log_download_progress("TestMod.zip", progress, 5.2)
        time.sleep(0.1)
    
    logger.log_download_complete("TestMod.zip", 50 * 1024 * 1024, 12.5, success=True)
    
    # Test error logging with context
    try:
        raise ValueError("Example error for testing")
    except Exception as e:
        logger.log_error_with_context(e, {"mod_id": 12345, "file_id": 67890})
    
    # Get performance stats
    stats = logger.get_performance_stats()
    if stats:
        print(f"Performance stats: {json.dumps(stats, indent=2)}")
    
    # Export debug logs
    logger.export_logs_for_debugging("debug_export.log")


if __name__ == "__main__":
    example_usage()