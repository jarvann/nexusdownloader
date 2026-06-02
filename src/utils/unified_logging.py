"""
Unified Logging System for NexusDownloader.

Provides consistent logging across all modules with proper file rotation,
standardized naming conventions, and centralized configuration.
"""

import os
import sys
import time
import logging
import logging.handlers
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any
import threading
import json


class CustomRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """
    Custom rotating file handler that names backup files with numbers before the extension.
    
    Instead of: nexusdownloader.log.1, nexusdownloader.log.2
    Uses: nexusdownloader_001.log, nexusdownloader_002.log
    """
    
    def rotation_filename(self, default_name):
        """
        Generate backup filename with number before extension.
        
        RotatingFileHandler calls this with names like 'filename.ext.1', 'filename.ext.2'
        We convert them to 'filename_001.ext', 'filename_002.ext'
        """
        # Default format from RotatingFileHandler: 'filename.ext.N'
        path = Path(default_name)
        
        # Split on dots to find the rotation number
        parts = path.name.split('.')
        if len(parts) >= 2 and parts[-1].isdigit():
            rotation_num = int(parts[-1])
            # Reconstruct base name and extension
            if len(parts) == 3:  # e.g., 'nexusdownloader.log.1'
                base_name = parts[0]
                extension = '.' + parts[1]
            else:  # More complex cases
                base_name = '.'.join(parts[:-2])
                extension = '.' + parts[-2]
        else:
            # Fallback case
            rotation_num = 1
            base_name = path.stem
            extension = path.suffix
        
        # Create new filename: basename_NNN.ext
        new_filename = f"{base_name}_{rotation_num:03d}{extension}"
        return str(path.parent / new_filename)


class UnifiedLogger:
    """
    Centralized logging system for all NexusDownloader modules.
    
    Provides consistent logging behavior, proper file rotation with correct naming,
    and standardized log file locations.
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        """Singleton pattern to ensure one logger instance."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance
    
    def __init__(self):
        """Initialize the unified logger."""
        if self._initialized:
            return
            
        # Set up base configuration - ensure project root regardless of working directory
        # Find project root by looking for specific project markers
        current_path = Path(__file__).resolve()
        project_root = None
        
        # Walk up the directory tree to find project root
        for parent in current_path.parents:
            # Look for project markers that indicate the root
            if (parent / "src" / "config.json").exists() or (parent / ".git").exists() or (parent.name == "nexusdownloader" and (parent / "src").exists()):
                project_root = parent
                break
        
        if project_root is None:
            # Fallback: assume unified_logging.py is in src/utils/ and go up 3 levels
            project_root = current_path.parent.parent.parent
        
        self.log_dir = project_root / "logs"
        self.log_dir.mkdir(exist_ok=True)
        
        # Configuration
        self.max_file_size = 10 * 1024 * 1024  # 10MB
        self.backup_count = 5
        self.log_level = logging.DEBUG
        
        # Initialize loggers
        self._setup_main_logger()
        self._setup_module_loggers()
        
        self._initialized = True
    
    def _setup_main_logger(self):
        """Setup the main application logger."""
        self.main_logger = logging.getLogger('nexusdownloader')
        self.main_logger.setLevel(self.log_level)
        
        # Clear any existing handlers
        self.main_logger.handlers.clear()
        
        # Main log file with custom rotation
        main_handler = CustomRotatingFileHandler(
            self.log_dir / "nexusdownloader.log",
            maxBytes=self.max_file_size,
            backupCount=self.backup_count,
            encoding='utf-8'
        )
        main_handler.setLevel(logging.DEBUG)
        main_handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(name)s - %(filename)s:%(lineno)d - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        self.main_logger.addHandler(main_handler)
        
        # Error log file
        error_handler = CustomRotatingFileHandler(
            self.log_dir / "nexusdownloader_errors.log",
            maxBytes=self.max_file_size,
            backupCount=self.backup_count,
            encoding='utf-8'
        )
        error_handler.setLevel(logging.WARNING)
        error_handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(name)s - %(filename)s:%(lineno)d\\n'
            'Message: %(message)s\\n'
            'Exception: %(exc_info)s\\n' + '-' * 80,
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        self.main_logger.addHandler(error_handler)
        
        # Console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%H:%M:%S'
        ))
        self.main_logger.addHandler(console_handler)
    
    def _setup_module_loggers(self):
        """Setup specialized loggers for different modules."""
        # Download operations logger
        self.download_logger = logging.getLogger('nexusdownloader.download')
        self.download_logger.setLevel(self.log_level)
        
        download_handler = CustomRotatingFileHandler(
            self.log_dir / "nexusdownloader_download.log",
            maxBytes=self.max_file_size,
            backupCount=self.backup_count,
            encoding='utf-8'
        )
        download_handler.setLevel(logging.DEBUG)
        download_handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(threadName)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        self.download_logger.addHandler(download_handler)
        
        # Installation operations logger
        self.install_logger = logging.getLogger('nexusdownloader.install')
        self.install_logger.setLevel(self.log_level)
        
        install_handler = CustomRotatingFileHandler(
            self.log_dir / "nexusdownloader_install.log",
            maxBytes=self.max_file_size,
            backupCount=self.backup_count,
            encoding='utf-8'
        )
        install_handler.setLevel(logging.DEBUG)
        install_handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(threadName)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        self.install_logger.addHandler(install_handler)
        
        # Performance/metrics logger
        self.performance_logger = logging.getLogger('nexusdownloader.performance')
        self.performance_logger.setLevel(logging.INFO)
        
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
        self.performance_logger.addHandler(perf_handler)
    
    def get_logger(self, module_name: str = None) -> logging.Logger:
        """
        Get a logger for a specific module.
        
        Args:
            module_name: Name of the module (download, install, performance, or None for main)
            
        Returns:
            Configured logger instance
        """
        if module_name == 'download':
            return self.download_logger
        elif module_name == 'install':
            return self.install_logger
        elif module_name == 'performance':
            return self.performance_logger
        else:
            return self.main_logger
    
    def create_operation_logger(self, operation_type: str, game_domain: str = "unknown") -> logging.Logger:
        """
        Create a specialized logger for a specific operation with timestamped filename.
        
        Args:
            operation_type: Type of operation (download, install, etc.)
            game_domain: Game domain for context
            
        Returns:
            Configured logger with timestamped file
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        logger_name = f"nexusdownloader.{operation_type}.{game_domain}.{timestamp}"
        
        logger = logging.getLogger(logger_name)
        logger.setLevel(self.log_level)
        
        # Create timestamped log file
        log_filename = f"nexusdownloader_{operation_type}_{game_domain}_{timestamp}.log"
        handler = logging.FileHandler(
            self.log_dir / log_filename,
            mode='w',
            encoding='utf-8'
        )
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(threadName)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        logger.addHandler(handler)
        
        return logger
    
    def log_performance_metrics(self, metrics: Dict[str, Any]):
        """Log performance metrics in JSON format."""
        self.performance_logger.info(f"METRICS: {json.dumps(metrics, indent=2)}")
    
    def cleanup_old_logs(self, days_to_keep: int = 30):
        """Clean up old log files."""
        from datetime import timedelta
        cutoff_date = datetime.now() - timedelta(days=days_to_keep)
        
        cleaned_count = 0
        for log_file in self.log_dir.glob("nexusdownloader*.log*"):
            try:
                file_time = datetime.fromtimestamp(log_file.stat().st_mtime)
                if file_time < cutoff_date:
                    log_file.unlink()
                    cleaned_count += 1
                    self.main_logger.info(f"Cleaned up old log file: {log_file.name}")
            except Exception as e:
                self.main_logger.warning(f"Failed to clean up {log_file.name}: {e}")
        
        if cleaned_count > 0:
            self.main_logger.info(f"Log cleanup completed: {cleaned_count} files removed")


# Global unified logger instance
_unified_logger = None

def get_unified_logger() -> UnifiedLogger:
    """Get the global unified logger instance."""
    global _unified_logger
    if _unified_logger is None:
        _unified_logger = UnifiedLogger()
    return _unified_logger

def get_logger(module_name: str = None) -> logging.Logger:
    """
    Get a logger for the specified module.
    
    Args:
        module_name: Module name (download, install, performance, or None for main)
        
    Returns:
        Configured logger instance
    """
    return get_unified_logger().get_logger(module_name)

def create_operation_logger(operation_type: str, game_domain: str = "unknown") -> logging.Logger:
    """
    Create a logger for a specific operation.
    
    Args:
        operation_type: Type of operation (download, install, etc.)
        game_domain: Game domain for context
        
    Returns:
        Configured logger with timestamped file
    """
    return get_unified_logger().create_operation_logger(operation_type, game_domain)

def setup_logging():
    """Initialize the unified logging system."""
    logger = get_unified_logger()
    logger.main_logger.info("Unified logging system initialized")
    return logger

def cleanup_old_logs(days_to_keep: int = 30):
    """Clean up old log files."""
    get_unified_logger().cleanup_old_logs(days_to_keep)


if __name__ == "__main__":
    # Test the unified logging system
    setup_logging()
    
    main_logger = get_logger()
    download_logger = get_logger('download')
    install_logger = get_logger('install')
    
    main_logger.info("Testing unified logging system")
    download_logger.info("Testing download logger")
    install_logger.info("Testing install logger")
    
    # Test operation logger
    op_logger = create_operation_logger("test", "skyrimse")
    op_logger.info("Testing operation-specific logger")
    
    print("Unified logging test completed. Check logs/ directory for output files.")