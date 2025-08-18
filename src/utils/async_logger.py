"""
Asynchronous logging system to prevent UI blocking.

This module provides a queue-based asynchronous logging system that processes
log messages in a background thread, preventing UI blocking during high-frequency
logging operations.
"""

import logging
import threading
import queue
import time
from typing import Optional, Any

try:
    from PySide6.QtCore import QObject, Signal, QThread
    QT_AVAILABLE = True
except ImportError:
    # Fallback to standard threading if Qt not available
    QT_AVAILABLE = False
    QObject = object
    QThread = threading.Thread


class AsyncLoggerThread(QThread if QT_AVAILABLE else threading.Thread):
    """Background thread for processing log messages asynchronously."""
    
    def __init__(self, logger: logging.Logger, max_queue_size: int = 10000):
        """
        Initialize the async logger thread.
        
        Args:
            logger: The underlying logger to write messages to
            max_queue_size: Maximum number of queued log messages
        """
        if QT_AVAILABLE:
            super().__init__()
        else:
            super().__init__(daemon=True)
        self.logger = logger
        self.log_queue = queue.Queue(maxsize=max_queue_size)
        self.running = False
        if not QT_AVAILABLE:
            self.daemon = True
        
    def run(self):
        """Main thread loop for processing log messages."""
        self.running = True
        while self.running:
            try:
                # Wait for a log message with timeout to allow clean shutdown
                try:
                    log_entry = self.log_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                
                if log_entry is None:  # Shutdown signal
                    break
                
                level, message, args, kwargs = log_entry
                
                # Process the log message
                if args or kwargs:
                    self.logger.log(level, message, *args, **kwargs)
                else:
                    self.logger.log(level, message)
                
                self.log_queue.task_done()
                
            except Exception as e:
                # Don't let logging errors crash the thread
                # Use stderr for async logger errors to avoid recursion
                import sys
                print(f"AsyncLogger error: {e}", file=sys.stderr)
    
    def stop(self):
        """Stop the async logger thread gracefully."""
        self.running = False
        # Send shutdown signal
        try:
            self.log_queue.put_nowait(None)
        except queue.Full:
            pass
        
        if QT_AVAILABLE:
            self.wait(2000)  # Wait up to 2 seconds for thread to finish
        else:
            self.join(timeout=2.0)
    
    def log_async(self, level: int, message: str, *args, **kwargs):
        """
        Queue a log message for asynchronous processing.
        
        Args:
            level: Log level (e.g., logging.INFO)
            message: Log message
            *args: Additional args for logger
            **kwargs: Additional kwargs for logger
        """
        if not self.running:
            return
            
        try:
            # Non-blocking queue operation - drop messages if queue is full
            self.log_queue.put_nowait((level, message, args, kwargs))
        except queue.Full:
            # Queue is full, drop the message to prevent blocking
            # Could optionally increment a dropped message counter here
            pass


class AsyncLogger(QObject):
    """
    Asynchronous logger wrapper that provides non-blocking logging.
    
    This class wraps a standard Python logger and processes log messages
    asynchronously in a background thread to prevent UI blocking.
    """
    
    def __init__(self, logger: logging.Logger, max_queue_size: int = 10000):
        """
        Initialize the async logger.
        
        Args:
            logger: The underlying logger to wrap
            max_queue_size: Maximum number of queued log messages
        """
        super().__init__()
        self.logger = logger
        self.async_thread = AsyncLoggerThread(logger, max_queue_size)
        self.async_thread.start()
    
    def log(self, level: int, message: str, *args, **kwargs):
        """Log a message asynchronously."""
        self.async_thread.log_async(level, message, *args, **kwargs)
    
    def debug(self, message: str, *args, **kwargs):
        """Log a debug message asynchronously."""
        self.log(logging.DEBUG, message, *args, **kwargs)
    
    def info(self, message: str, *args, **kwargs):
        """Log an info message asynchronously."""
        self.log(logging.INFO, message, *args, **kwargs)
    
    def warning(self, message: str, *args, **kwargs):
        """Log a warning message asynchronously."""
        self.log(logging.WARNING, message, *args, **kwargs)
    
    def error(self, message: str, *args, **kwargs):
        """Log an error message asynchronously."""
        self.log(logging.ERROR, message, *args, **kwargs)
    
    def critical(self, message: str, *args, **kwargs):
        """Log a critical message asynchronously."""
        self.log(logging.CRITICAL, message, *args, **kwargs)
    
    def flush(self):
        """
        Flush all pending log messages synchronously.
        
        This blocks until all queued messages are processed.
        Use sparingly, typically only during shutdown.
        """
        if self.async_thread.running:
            self.async_thread.log_queue.join()
    
    def stop(self):
        """Stop the async logger and clean up resources."""
        self.async_thread.stop()
    
    def get_queue_size(self) -> int:
        """Get the current number of queued log messages."""
        return self.async_thread.log_queue.qsize()
    
    def is_queue_full(self) -> bool:
        """Check if the log queue is full."""
        return self.async_thread.log_queue.full()


class AsyncLogHandler(QObject):
    """
    Qt-compatible async log handler for GUI applications.
    
    This handler processes log messages from Qt signals asynchronously,
    preventing UI thread blocking during high-frequency logging.
    """
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        """
        Initialize the async log handler.
        
        Args:
            logger: Optional logger instance. If None, uses root logger.
        """
        super().__init__()
        
        # Handle different types of logger objects
        actual_logger = None
        if logger is None:
            actual_logger = logging.getLogger()
        elif hasattr(logger, 'logger') and hasattr(logger.logger, 'debug'):
            # NexusLogger case - extract the underlying Python logger
            actual_logger = logger.logger
        elif hasattr(logger, 'debug'):
            # Standard Python logger
            actual_logger = logger
        else:
            # Fallback to root logger
            actual_logger = logging.getLogger()
        
        self.async_logger = AsyncLogger(actual_logger)
    
    def handle_log_message(self, level: str, message: str):
        """
        Handle a log message from a Qt signal.
        
        Args:
            level: Log level string (INFO, WARNING, ERROR, etc.)
            message: Log message content
        """
        try:
            log_level = getattr(logging, level.upper(), logging.INFO)
            self.async_logger.log(log_level, message)
        except Exception as e:
            # Fallback to synchronous logging if async fails
            # Use stderr for async log handler errors to avoid recursion
            import sys
            print(f"AsyncLogHandler error: {e}", file=sys.stderr)
            
            # Try to log the error using the underlying logger
            try:
                underlying_logger = None
                if hasattr(self.async_logger, 'logger') and hasattr(self.async_logger.logger, 'logger'):
                    # NexusLogger case: self.async_logger.logger is NexusLogger, .logger is the Python logger
                    underlying_logger = self.async_logger.logger.logger
                elif hasattr(self.async_logger, 'logger'):
                    # Direct logger case
                    underlying_logger = self.async_logger.logger
                
                if underlying_logger and hasattr(underlying_logger, 'error'):
                    underlying_logger.error(f"AsyncLogHandler error: {e}")
            except:
                pass  # Ignore errors in error handling
    
    def stop(self):
        """Stop the async logger and clean up resources."""
        self.async_logger.stop()
    
    def flush(self):
        """Flush all pending log messages."""
        self.async_logger.flush()
    
    def get_stats(self) -> dict:
        """Get logging statistics."""
        return {
            'queue_size': self.async_logger.get_queue_size(),
            'queue_full': self.async_logger.is_queue_full(),
            'thread_running': self.async_logger.async_thread.running
        }