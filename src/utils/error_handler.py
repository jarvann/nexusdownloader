"""
Error Handling Module for NexusDownloader.

Provides comprehensive error categorization, retry logic, and failure recovery
mechanisms for robust download operations.
"""

import time
import hashlib
import requests
from typing import Callable, Any, Optional, Dict
from enum import Enum
import logging


class ErrorType(Enum):
    """Categorize different types of errors"""
    NETWORK_ERROR = "network"
    API_ERROR = "api"
    FILE_ERROR = "file"
    VALIDATION_ERROR = "validation"
    PERMISSION_ERROR = "permission"
    UNKNOWN_ERROR = "unknown"


class DownloadError(Exception):
    """Base exception for download-related errors"""
    def __init__(self, message: str, error_type: ErrorType = ErrorType.UNKNOWN_ERROR, 
                 retryable: bool = False, details: Dict = None):
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable
        self.details = details or {}
        self.timestamp = time.time()


class RetryableError(DownloadError):
    """Errors that can be retried"""
    def __init__(self, message: str, error_type: ErrorType = ErrorType.NETWORK_ERROR, 
                 details: Dict = None):
        super().__init__(message, error_type, retryable=True, details=details)


class FatalError(DownloadError):
    """Errors that should stop the process"""
    def __init__(self, message: str, error_type: ErrorType = ErrorType.UNKNOWN_ERROR, 
                 details: Dict = None):
        super().__init__(message, error_type, retryable=False, details=details)


class ErrorHandler:
    """
    Comprehensive error handling with retry logic and categorization.
    
    Provides exponential backoff retry mechanisms, error categorization,
    and statistics tracking for robust error management in download operations.
    """
    
    def __init__(self, max_retries: int = 3, base_retry_delay: float = 2.0, 
                 max_retry_delay: float = 60.0, backoff_multiplier: float = 2.0):
        self.max_retries = max_retries
        self.base_retry_delay = base_retry_delay
        self.max_retry_delay = max_retry_delay
        self.backoff_multiplier = backoff_multiplier
        self.logger = logging.getLogger(__name__)
        
        # Statistics tracking
        self.error_counts = {error_type: 0 for error_type in ErrorType}
        self.retry_counts = 0
        self.recovery_counts = 0
    
    def retry_with_backoff(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute function with exponential backoff retry logic
        
        Args:
            func: Function to execute
            *args: Function arguments
            **kwargs: Function keyword arguments
            
        Returns:
            Function result if successful
            
        Raises:
            DownloadError: If all retries are exhausted
        """
        last_error = None
        
        for attempt in range(self.max_retries + 1):
            try:
                return func(*args, **kwargs)
                
            except Exception as e:
                categorized_error = self.categorize_error(e)
                last_error = categorized_error
                
                # Track error statistics
                self.error_counts[categorized_error.error_type] += 1
                
                # Don't retry fatal errors
                if not categorized_error.retryable:
                    self.logger.error(
                        f"Fatal error in {func.__name__}: {categorized_error}"
                    )
                    raise categorized_error
                
                # Don't retry on last attempt
                if attempt == self.max_retries:
                    self.logger.error(
                        f"All retries exhausted for {func.__name__}: {categorized_error}"
                    )
                    raise categorized_error
                
                # Calculate delay with exponential backoff
                delay = min(
                    self.base_retry_delay * (self.backoff_multiplier ** attempt),
                    self.max_retry_delay
                )
                
                self.retry_counts += 1
                self.logger.warning(
                    f"Attempt {attempt + 1}/{self.max_retries + 1} failed for "
                    f"{func.__name__}: {categorized_error}. Retrying in {delay:.1f}s..."
                )
                
                time.sleep(delay)
        
        # This should never be reached, but just in case
        raise last_error
    
    def categorize_error(self, error: Exception) -> DownloadError:
        """
        Categorize and wrap exceptions into DownloadError types
        
        Args:
            error: The original exception
            
        Returns:
            DownloadError: Categorized error with retry information
        """
        error_msg = str(error)
        error_details = {"original_type": type(error).__name__}
        
        # Network-related errors (retryable)
        if isinstance(error, (requests.exceptions.ConnectionError, 
                            requests.exceptions.Timeout,
                            requests.exceptions.ConnectTimeout,
                            requests.exceptions.ReadTimeout)):
            return RetryableError(
                f"Network error: {error_msg}",
                ErrorType.NETWORK_ERROR,
                error_details
            )
        
        # API-related errors
        if isinstance(error, requests.exceptions.HTTPError):
            status_code = getattr(error.response, 'status_code', None)
            error_details["status_code"] = status_code
            
            # Rate limiting (retryable)
            if status_code == 429:
                return RetryableError(
                    f"Rate limited: {error_msg}",
                    ErrorType.API_ERROR,
                    error_details
                )
            
            # Server errors (retryable)
            elif status_code and 500 <= status_code < 600:
                return RetryableError(
                    f"Server error: {error_msg}",
                    ErrorType.API_ERROR,
                    error_details
                )
            
            # Client errors (usually not retryable)
            elif status_code and 400 <= status_code < 500:
                return FatalError(
                    f"Client error: {error_msg}",
                    ErrorType.API_ERROR,
                    error_details
                )
        
        # File system errors
        if isinstance(error, (IOError, OSError, FileNotFoundError, 
                            PermissionError, FileExistsError)):
            if isinstance(error, PermissionError):
                return FatalError(
                    f"Permission denied: {error_msg}",
                    ErrorType.PERMISSION_ERROR,
                    error_details
                )
            elif isinstance(error, FileNotFoundError):
                return FatalError(
                    f"File not found: {error_msg}",
                    ErrorType.FILE_ERROR,
                    error_details
                )
            else:
                # Some file errors might be retryable (temporary locks, etc.)
                return RetryableError(
                    f"File system error: {error_msg}",
                    ErrorType.FILE_ERROR,
                    error_details
                )
        
        # JSON/parsing errors
        if isinstance(error, (ValueError, KeyError, TypeError)):
            return FatalError(
                f"Data validation error: {error_msg}",
                ErrorType.VALIDATION_ERROR,
                error_details
            )
        
        # Default: treat as retryable unknown error
        return RetryableError(
            f"Unknown error: {error_msg}",
            ErrorType.UNKNOWN_ERROR,
            error_details
        )
    
    def get_error_statistics(self) -> Dict:
        """Get error handling statistics"""
        return {
            "error_counts": dict(self.error_counts),
            "retry_counts": self.retry_counts,
            "recovery_counts": self.recovery_counts,
            "total_errors": sum(self.error_counts.values())
        }
    
    def reset_statistics(self):
        """Reset error statistics"""
        self.error_counts = {error_type: 0 for error_type in ErrorType}
        self.retry_counts = 0
        self.recovery_counts = 0


class FileIntegrityValidator:
    """
    Handles file integrity validation using checksums.
    
    Provides methods for calculating and verifying file checksums to ensure
    data integrity during and after download operations.
    """
    
    @staticmethod
    def calculate_file_hash(file_path: str, algorithm: str = 'md5') -> str:
        """
        Calculate hash of a file
        
        Args:
            file_path: Path to the file
            algorithm: Hash algorithm ('md5', 'sha256', etc.)
            
        Returns:
            Hexadecimal hash string
        """
        hash_obj = hashlib.new(algorithm)
        
        try:
            with open(file_path, 'rb') as f:
                # Read file in chunks to handle large files
                for chunk in iter(lambda: f.read(4096), b""):
                    hash_obj.update(chunk)
            return hash_obj.hexdigest()
        except Exception as e:
            raise FatalError(
                f"Failed to calculate {algorithm} hash for {file_path}: {e}",
                ErrorType.FILE_ERROR
            )
    
    @staticmethod
    def verify_file_integrity(file_path: str, expected_hash: str, 
                            algorithm: str = 'md5') -> bool:
        """
        Verify file integrity against expected hash
        
        Args:
            file_path: Path to the file
            expected_hash: Expected hash value
            algorithm: Hash algorithm used
            
        Returns:
            True if file is valid, False otherwise
        """
        try:
            calculated_hash = FileIntegrityValidator.calculate_file_hash(
                file_path, algorithm
            )
            return calculated_hash.lower() == expected_hash.lower()
        except Exception:
            return False


class PartialDownloadManager:
    """
    Manages partial download resumption capabilities.
    
    Provides functionality to detect server support for range requests
    and generate appropriate headers for resuming interrupted downloads.
    """
    
    @staticmethod
    def supports_range_requests(url: str, headers: Dict = None) -> bool:
        """
        Check if server supports range requests for resuming downloads
        
        Args:
            url: URL to check
            headers: Optional headers for the request
            
        Returns:
            True if range requests are supported
        """
        try:
            response = requests.head(url, headers=headers or {}, timeout=10)
            return response.headers.get('Accept-Ranges') == 'bytes'
        except Exception:
            return False
    
    @staticmethod
    def get_partial_download_headers(file_path: str) -> Dict[str, str]:
        """
        Generate headers for resuming a partial download
        
        Args:
            file_path: Path to the partial file
            
        Returns:
            Headers dict with Range header
        """
        try:
            import os
            if os.path.exists(file_path):
                file_size = os.path.getsize(file_path)
                return {'Range': f'bytes={file_size}-'}
            return {}
        except Exception:
            return {}


# Example usage and testing functions
def example_usage():
    """Example of how to use the error handler"""
    
    def unreliable_function():
        """Simulates an unreliable function that might fail"""
        import random
        if random.random() < 0.7:  # 70% chance of failure
            raise requests.exceptions.ConnectionError("Simulated network error")
        return "Success!"
    
    # Create error handler
    error_handler = ErrorHandler(max_retries=3, base_retry_delay=1.0)
    
    try:
        # Try to execute unreliable function with retry logic
        result = error_handler.retry_with_backoff(unreliable_function)
        print(f"Function succeeded: {result}")
        
    except DownloadError as e:
        print(f"Function failed after retries: {e}")
        print(f"Error type: {e.error_type}")
        print(f"Retryable: {e.retryable}")
    
    # Print statistics
    print("Error Statistics:", error_handler.get_error_statistics())


if __name__ == "__main__":
    example_usage()