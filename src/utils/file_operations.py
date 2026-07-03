"""
File Operations Module for NexusDownloader.

Provides comprehensive file handling capabilities including atomic operations,
integrity verification, and safe file management for download operations.
"""

import os
import shutil
import hashlib
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional, List, Callable
import logging
from contextlib import contextmanager
import threading
from dataclasses import dataclass
from enum import Enum


class FileOperation(Enum):
    """Types of file operations"""
    COPY = "copy"
    MOVE = "move"
    DELETE = "delete"
    CREATE = "create"
    VERIFY = "verify"


@dataclass
class FileInfo:
    """Information about a file"""
    path: str
    size: int
    modification_time: float
    checksum: Optional[str] = None
    checksum_algorithm: str = "md5"
    permissions: Optional[str] = None


class FileOperationError(Exception):
    """Custom exception for file operation errors"""
    def __init__(self, message: str, operation: FileOperation, 
                 file_path: str, details: Dict = None):
        super().__init__(message)
        self.operation = operation
        self.file_path = file_path
        self.details = details or {}


class AtomicFileWriter:
    """
    Provides atomic file writing operations.
    
    Ensures file writes are atomic by using temporary files and atomic moves,
    preventing corruption from interrupted write operations.
    """
    
    def __init__(self, target_path: str, mode: str = 'wb', 
                 temp_dir: Optional[str] = None):
        self.target_path = Path(target_path)
        self.mode = mode
        self.temp_dir = temp_dir
        self.temp_file = None
        self.temp_path = None
        
    def __enter__(self):
        """Enter context manager"""
        # Create temporary file in same directory as target for atomic move
        target_dir = self.target_path.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        
        self.temp_file = tempfile.NamedTemporaryFile(
            mode=self.mode,
            dir=self.temp_dir or target_dir,
            delete=False,
            prefix=f".{self.target_path.name}.",
            suffix=".tmp"
        )
        self.temp_path = Path(self.temp_file.name)
        return self.temp_file
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context manager"""
        if self.temp_file:
            self.temp_file.close()
        
        if exc_type is None:
            # Success - atomically move temp file to target
            try:
                if os.name == 'nt':  # Windows
                    # Windows doesn't support atomic replace if target exists
                    if self.target_path.exists():
                        self.target_path.unlink()
                    self.temp_path.rename(self.target_path)
                else:  # Unix-like systems
                    self.temp_path.replace(self.target_path)
            except Exception as e:
                # Clean up temp file if move fails
                try:
                    self.temp_path.unlink()
                except:
                    pass
                raise FileOperationError(
                    f"Failed to atomically write file: {e}",
                    FileOperation.CREATE,
                    str(self.target_path)
                )
        else:
            # Error occurred - clean up temp file
            try:
                if self.temp_path and self.temp_path.exists():
                    self.temp_path.unlink()
            except:
                pass


class FileManager:
    """
    Main file management class with comprehensive operations.
    
    Provides a wide range of file operations including copying, moving, deletion,
    integrity verification, and directory management with proper error handling.
    """
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self._operation_lock = threading.Lock()
        self._active_operations = {}
    
    def get_file_info(self, file_path: str, calculate_checksum: bool = True,
                     checksum_algorithm: str = "md5") -> FileInfo:
        """
        Get comprehensive information about a file
        
        Args:
            file_path: Path to the file
            calculate_checksum: Whether to calculate file checksum
            checksum_algorithm: Algorithm to use for checksum
            
        Returns:
            FileInfo object with file details
        """
        path = Path(file_path)
        
        if not path.exists():
            raise FileOperationError(
                f"File does not exist: {file_path}",
                FileOperation.VERIFY,
                file_path
            )
        
        try:
            stat = path.stat()
            
            file_info = FileInfo(
                path=str(path.absolute()),
                size=stat.st_size,
                modification_time=stat.st_mtime,
                checksum_algorithm=checksum_algorithm
            )
            
            # Calculate checksum if requested
            if calculate_checksum:
                file_info.checksum = self.calculate_checksum(
                    file_path, checksum_algorithm
                )
            
            # Get permissions (Unix-like systems)
            try:
                file_info.permissions = oct(stat.st_mode)[-3:]
            except:
                file_info.permissions = None
            
            return file_info
            
        except Exception as e:
            raise FileOperationError(
                f"Failed to get file info: {e}",
                FileOperation.VERIFY,
                file_path
            )
    
    def calculate_checksum(self, file_path: str, algorithm: str = "md5",
                          chunk_size: int = 8192,
                          progress_callback: Optional[Callable] = None) -> str:
        """
        Calculate file checksum with progress reporting
        
        Args:
            file_path: Path to the file
            algorithm: Hash algorithm to use
            chunk_size: Size of chunks to read
            progress_callback: Optional callback for progress updates
            
        Returns:
            Hexadecimal checksum string
        """
        try:
            hash_obj = hashlib.new(algorithm)
            file_size = os.path.getsize(file_path)
            bytes_read = 0
            
            with open(file_path, 'rb') as f:
                while chunk := f.read(chunk_size):
                    hash_obj.update(chunk)
                    bytes_read += len(chunk)
                    
                    if progress_callback:
                        progress_percent = (bytes_read / file_size) * 100
                        progress_callback(progress_percent)
            
            return hash_obj.hexdigest()
            
        except Exception as e:
            raise FileOperationError(
                f"Failed to calculate {algorithm} checksum: {e}",
                FileOperation.VERIFY,
                file_path
            )
    
    def verify_file_integrity(self, file_path: str, expected_checksum: str,
                            algorithm: str = "md5") -> bool:
        """
        Verify file integrity against expected checksum
        
        Args:
            file_path: Path to the file
            expected_checksum: Expected checksum value
            algorithm: Hash algorithm used
            
        Returns:
            True if file is valid
        """
        try:
            calculated_checksum = self.calculate_checksum(file_path, algorithm)
            is_valid = calculated_checksum.lower() == expected_checksum.lower()
            
            if is_valid:
                self.logger.debug(f"File integrity verified: {file_path}")
            else:
                self.logger.warning(
                    f"File integrity check failed: {file_path} "
                    f"(expected: {expected_checksum}, got: {calculated_checksum})"
                )
            
            return is_valid
            
        except Exception as e:
            self.logger.error(f"File integrity verification failed: {e}")
            return False
    
    def safe_copy(self, source: str, destination: str, 
                  verify_integrity: bool = True,
                  preserve_metadata: bool = True,
                  progress_callback: Optional[Callable] = None) -> bool:
        """
        Safely copy a file with optional integrity verification
        
        Args:
            source: Source file path
            destination: Destination file path
            verify_integrity: Whether to verify file after copy
            preserve_metadata: Whether to preserve file metadata
            progress_callback: Optional progress callback
            
        Returns:
            True if copy was successful
        """
        source_path = Path(source)
        dest_path = Path(destination)
        
        if not source_path.exists():
            raise FileOperationError(
                f"Source file does not exist: {source}",
                FileOperation.COPY,
                source
            )
        
        try:
            # Create destination directory if needed
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Get source file info for verification
            source_info = self.get_file_info(source, calculate_checksum=verify_integrity)
            
            # Perform copy with progress tracking
            if progress_callback:
                self._copy_with_progress(source_path, dest_path, progress_callback)
            else:
                shutil.copy2(source_path, dest_path)
            
            # Preserve metadata if requested
            if preserve_metadata:
                shutil.copystat(source_path, dest_path)
            
            # Verify integrity if requested
            if verify_integrity and source_info.checksum:
                if not self.verify_file_integrity(
                    str(dest_path), source_info.checksum, source_info.checksum_algorithm
                ):
                    dest_path.unlink()  # Remove corrupted copy
                    raise FileOperationError(
                        "File integrity verification failed after copy",
                        FileOperation.COPY,
                        source
                    )
            
            self.logger.info(f"Successfully copied: {source} -> {destination}")
            return True
            
        except Exception as e:
            # Clean up partial copy on failure
            try:
                if dest_path.exists():
                    dest_path.unlink()
            except:
                pass
            
            raise FileOperationError(
                f"Failed to copy file: {e}",
                FileOperation.COPY,
                source
            )
    
    def _copy_with_progress(self, source_path: Path, dest_path: Path,
                          progress_callback: Callable, chunk_size: int = 1024*1024):
        """Copy file with progress reporting"""
        file_size = source_path.stat().st_size
        bytes_copied = 0
        
        with open(source_path, 'rb') as src, open(dest_path, 'wb') as dst:
            while chunk := src.read(chunk_size):
                dst.write(chunk)
                bytes_copied += len(chunk)
                progress_percent = (bytes_copied / file_size) * 100
                progress_callback(progress_percent)
    
    def safe_move(self, source: str, destination: str,
                  verify_integrity: bool = True) -> bool:
        """
        Safely move a file with optional integrity verification
        
        Args:
            source: Source file path
            destination: Destination file path
            verify_integrity: Whether to verify file after move
            
        Returns:
            True if move was successful
        """
        source_path = Path(source)
        dest_path = Path(destination)
        
        if not source_path.exists():
            raise FileOperationError(
                f"Source file does not exist: {source}",
                FileOperation.MOVE,
                source
            )
        
        try:
            # Get source file info for verification
            source_info = None
            if verify_integrity:
                source_info = self.get_file_info(source, calculate_checksum=True)
            
            # Create destination directory if needed
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Try atomic move first (same filesystem)
            try:
                source_path.rename(dest_path)
                moved_atomically = True
            except OSError:
                # Cross-filesystem move - copy and delete
                moved_atomically = False
                shutil.copy2(source_path, dest_path)
                source_path.unlink()
            
            # Verify integrity if requested
            if verify_integrity and source_info and source_info.checksum:
                if not self.verify_file_integrity(
                    str(dest_path), source_info.checksum, source_info.checksum_algorithm
                ):
                    dest_path.unlink()  # Remove corrupted file
                    raise FileOperationError(
                        "File integrity verification failed after move",
                        FileOperation.MOVE,
                        source
                    )
            
            self.logger.info(
                f"Successfully moved: {source} -> {destination} "
                f"({'atomic' if moved_atomically else 'copy+delete'})"
            )
            return True
            
        except Exception as e:
            raise FileOperationError(
                f"Failed to move file: {e}",
                FileOperation.MOVE,
                source
            )
    
    def safe_delete(self, file_path: str, secure: bool = False) -> bool:
        """
        Safely delete a file with optional secure deletion
        
        Args:
            file_path: Path to the file to delete
            secure: Whether to perform secure deletion (overwrite)
            
        Returns:
            True if deletion was successful
        """
        path = Path(file_path)
        
        if not path.exists():
            self.logger.warning(f"File does not exist for deletion: {file_path}")
            return True
        
        try:
            if secure:
                self._secure_delete(path)
            else:
                path.unlink()
            
            self.logger.info(f"Successfully deleted: {file_path}")
            return True
            
        except Exception as e:
            raise FileOperationError(
                f"Failed to delete file: {e}",
                FileOperation.DELETE,
                file_path
            )
    
    def _secure_delete(self, file_path: Path, passes: int = 3):
        """
        Securely delete a file by overwriting with random data
        
        Args:
            file_path: Path to the file
            passes: Number of overwrite passes
        """
        import random
        
        file_size = file_path.stat().st_size
        
        with open(file_path, 'r+b') as f:
            for _ in range(passes):
                f.seek(0)
                # Overwrite with random data
                for _ in range(0, file_size, 4096):
                    chunk_size = min(4096, file_size - f.tell())
                    if chunk_size <= 0:
                        break
                    random_data = bytes(random.getrandbits(8) for _ in range(chunk_size))
                    f.write(random_data)
                f.flush()
                os.fsync(f.fileno())
        
        # Finally delete the file
        file_path.unlink()
    
    def create_directory_tree(self, path: str, permissions: Optional[str] = None) -> bool:
        """
        Create directory tree with proper error handling
        
        Args:
            path: Directory path to create
            permissions: Optional permissions (Unix-like systems)
            
        Returns:
            True if successful
        """
        try:
            dir_path = Path(path)
            dir_path.mkdir(parents=True, exist_ok=True)
            
            # Set permissions if specified (Unix-like systems)
            if permissions and os.name != 'nt':
                try:
                    os.chmod(dir_path, int(permissions, 8))
                except:
                    pass
            
            self.logger.debug(f"Created directory tree: {path}")
            return True
            
        except Exception as e:
            raise FileOperationError(
                f"Failed to create directory tree: {e}",
                FileOperation.CREATE,
                path
            )
    
    def get_directory_size(self, directory: str) -> int:
        """
        Calculate total size of directory and all subdirectories
        
        Args:
            directory: Directory path
            
        Returns:
            Total size in bytes
        """
        total_size = 0
        dir_path = Path(directory)
        
        try:
            for file_path in dir_path.rglob('*'):
                if file_path.is_file():
                    total_size += file_path.stat().st_size
            return total_size
            
        except Exception as e:
            self.logger.error(f"Failed to calculate directory size: {e}")
            return 0
    
    def find_files_by_pattern(self, directory: str, pattern: str,
                            recursive: bool = True) -> List[str]:
        """
        Find files matching a pattern
        
        Args:
            directory: Directory to search
            pattern: File pattern (glob style)
            recursive: Whether to search subdirectories
            
        Returns:
            List of matching file paths
        """
        dir_path = Path(directory)
        
        if not dir_path.exists():
            return []
        
        try:
            if recursive:
                matches = dir_path.rglob(pattern)
            else:
                matches = dir_path.glob(pattern)
            
            return [str(match) for match in matches if match.is_file()]
            
        except Exception as e:
            self.logger.error(f"Failed to find files by pattern: {e}")
            return []
    
    @contextmanager
    def temporary_file(self, suffix: str = "", prefix: str = "nexus_",
                      directory: Optional[str] = None):
        """
        Context manager for temporary files
        
        Args:
            suffix: File suffix
            prefix: File prefix
            directory: Directory for temp file
            
        Yields:
            Path to temporary file
        """
        temp_file = None
        try:
            temp_file = tempfile.NamedTemporaryFile(
                suffix=suffix,
                prefix=prefix,
                dir=directory,
                delete=False
            )
            temp_path = temp_file.name
            temp_file.close()
            
            yield temp_path
            
        finally:
            # Clean up temporary file
            if temp_file:
                try:
                    Path(temp_path).unlink()
                except:
                    pass
    
    def backup_file(self, file_path: str, backup_dir: Optional[str] = None,
                   timestamp_suffix: bool = True) -> str:
        """
        Create a backup of a file
        
        Args:
            file_path: Original file path
            backup_dir: Directory for backup (defaults to same as original)
            timestamp_suffix: Whether to add timestamp to backup name
            
        Returns:
            Path to backup file
        """
        source_path = Path(file_path)
        
        if not source_path.exists():
            raise FileOperationError(
                f"Source file does not exist: {file_path}",
                FileOperation.COPY,
                file_path
            )
        
        # Determine backup location
        if backup_dir:
            backup_path = Path(backup_dir) / source_path.name
        else:
            backup_path = source_path.parent / f"{source_path.name}.backup"
        
        # Add timestamp if requested
        if timestamp_suffix:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            backup_path = backup_path.with_suffix(f".{timestamp}{backup_path.suffix}")
        
        # Create backup
        self.safe_copy(str(source_path), str(backup_path))
        
        self.logger.info(f"Created backup: {file_path} -> {backup_path}")
        return str(backup_path)
    
    def cleanup_old_files(self, directory: str, pattern: str = "*",
                         max_age_days: int = 30) -> int:
        """
        Clean up old files in a directory
        
        Args:
            directory: Directory to clean
            pattern: File pattern to match
            max_age_days: Maximum age in days
            
        Returns:
            Number of files deleted
        """
        deleted_count = 0
        cutoff_time = time.time() - (max_age_days * 24 * 60 * 60)
        
        try:
            for file_path in self.find_files_by_pattern(directory, pattern):
                file_stat = Path(file_path).stat()
                if file_stat.st_mtime < cutoff_time:
                    self.safe_delete(file_path)
                    deleted_count += 1
            
            self.logger.info(f"Cleaned up {deleted_count} old files from {directory}")
            return deleted_count
            
        except Exception as e:
            self.logger.error(f"Failed to cleanup old files: {e}")
            return deleted_count


class DownloadFileManager(FileManager):
    """
    Specialized file manager for download operations.
    
    Extends FileManager with download-specific functionality including
    path sanitization, partial download support, and download finalization.
    """
    
    def __init__(self, download_dir: str, logger: Optional[logging.Logger] = None):
        super().__init__(logger)
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
    
    def get_download_path(self, filename: str, mod_id: Optional[int] = None,
                         file_id: Optional[int] = None) -> str:
        """
        Generate a safe download path for a file
        
        Args:
            filename: Original filename
            mod_id: Optional mod ID
            file_id: Optional file ID
            
        Returns:
            Full download path
        """
        # Sanitize filename
        safe_filename = self._sanitize_filename(filename)
        
        # Add IDs if provided
        if mod_id and file_id:
            name, ext = os.path.splitext(safe_filename)
            safe_filename = f"{name}_{mod_id}_{file_id}{ext}"
        
        return str(self.download_dir / safe_filename)
    
    def _sanitize_filename(self, filename: str) -> str:
        """Sanitize filename for safe storage"""
        # Remove or replace invalid characters
        invalid_chars = '<>:"/\\|?*'
        for char in invalid_chars:
            filename = filename.replace(char, '_')
        
        # Remove control characters
        filename = ''.join(char for char in filename if ord(char) >= 32)
        
        # Limit length
        if len(filename) > 200:
            name, ext = os.path.splitext(filename)
            filename = name[:200-len(ext)] + ext
        
        return filename or "unnamed_file"
    
    def create_partial_download_file(self, target_path: str) -> str:
        """
        Create a partial download file for resumable downloads
        
        Args:
            target_path: Final target path
            
        Returns:
            Path to partial download file
        """
        partial_path = f"{target_path}.partial"
        
        # Create empty partial file
        with open(partial_path, 'wb'):
            pass
        
        return partial_path
    
    def finalize_download(self, partial_path: str, target_path: str,
                         expected_checksum: Optional[str] = None) -> bool:
        """
        Finalize a download by moving partial file to final location
        
        Args:
            partial_path: Path to partial download file
            target_path: Final target path
            expected_checksum: Optional checksum for verification
            
        Returns:
            True if successful
        """
        try:
            # Verify checksum if provided
            if expected_checksum:
                if not self.verify_file_integrity(partial_path, expected_checksum):
                    raise FileOperationError(
                        "Download checksum verification failed",
                        FileOperation.VERIFY,
                        partial_path
                    )
            
            # Move partial file to final location
            self.safe_move(partial_path, target_path, verify_integrity=False)
            
            self.logger.info(f"Download finalized: {target_path}")
            return True
            
        except Exception as e:
            # Clean up partial file on failure
            try:
                Path(partial_path).unlink()
            except:
                pass
            
            raise FileOperationError(
                f"Failed to finalize download: {e}",
                FileOperation.MOVE,
                partial_path
            )


# Example usage
def example_usage():
    """Example of how to use the file operations module"""
    
    # Setup logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    
    # Create file manager
    file_manager = FileManager(logger)
    
    # Test file info
    test_file = "test_file.txt"
    with open(test_file, 'w') as f:
        f.write("This is a test file for demonstration.")
    
    try:
        # Get file info
        file_info = file_manager.get_file_info(test_file)
        print(f"File info: {file_info}")
        
        # Test atomic writing
        with AtomicFileWriter("atomic_test.txt", mode='w') as f:
            f.write("This is written atomically!")
        
        # Test safe copy
        file_manager.safe_copy(test_file, "copy_test.txt")
        
        # Test directory operations
        file_manager.create_directory_tree("test_dir/subdir")
        
        # Test pattern finding
        matches = file_manager.find_files_by_pattern(".", "*.txt")
        print(f"Found text files: {matches}")
        
        # Test download file manager
        download_manager = DownloadFileManager("downloads")
        download_path = download_manager.get_download_path(
            "test_mod.zip", mod_id=12345, file_id=67890
        )
        print(f"Download path: {download_path}")
        
    finally:
        # Cleanup
        for cleanup_file in ["test_file.txt", "atomic_test.txt", "copy_test.txt"]:
            try:
                os.unlink(cleanup_file)
            except:
                pass
        
        try:
            shutil.rmtree("test_dir")
            shutil.rmtree("downloads")
        except:
            pass


if __name__ == "__main__":
    example_usage()