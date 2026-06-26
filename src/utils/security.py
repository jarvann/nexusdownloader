"""
Security Module for NexusDownloader
Handles secure API key storage, rate limiting, and input validation
"""

import os
import time
import json
import base64
import hashlib
import logging
from typing import Dict, Optional, Any, List
from collections import deque
from datetime import datetime, timedelta
import re

# Optional imports for enhanced security
try:
    import keyring
    KEYRING_AVAILABLE = True
except ImportError:
    KEYRING_AVAILABLE = False
    print("Warning: keyring not available. API keys will be stored in plain text.")

try:
    from cryptography.fernet import Fernet
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False
    print("Warning: cryptography not available. Enhanced encryption disabled.")


class SecureConfig:
    """Manages secure configuration storage and retrieval"""
    
    def __init__(self, service_name: str = "nexusdownloader"):
        self.service_name = service_name
        self.logger = logging.getLogger(__name__)
        self._encryption_key = None
    
    def _get_encryption_key(self) -> bytes:
        """Get or generate encryption key for local storage"""
        if not CRYPTO_AVAILABLE:
            raise RuntimeError("Cryptography library not available")
        
        if self._encryption_key is None:
            # Try to load existing key from environment or generate new one
            key_env = os.environ.get('NEXUS_ENCRYPTION_KEY')
            if key_env:
                self._encryption_key = base64.urlsafe_b64decode(key_env.encode())
            else:
                self._encryption_key = Fernet.generate_key()
                # Optionally save to environment for persistence
                # Note: In production, consider more secure key storage
        
        return self._encryption_key
    
    def encrypt_data(self, data: str) -> str:
        """
        Encrypt sensitive data for local storage
        
        Args:
            data: Plain text data to encrypt
            
        Returns:
            Base64 encoded encrypted data
        """
        if not CRYPTO_AVAILABLE:
            self.logger.warning("Encryption not available, storing data in plain text")
            return data
        
        try:
            cipher_suite = Fernet(self._get_encryption_key())
            encrypted_data = cipher_suite.encrypt(data.encode())
            return base64.urlsafe_b64encode(encrypted_data).decode()
        except Exception as e:
            self.logger.error(f"Encryption failed: {e}")
            return data
    
    def decrypt_data(self, encrypted_data: str) -> str:
        """
        Decrypt data from local storage
        
        Args:
            encrypted_data: Base64 encoded encrypted data
            
        Returns:
            Decrypted plain text data
        """
        if not CRYPTO_AVAILABLE:
            return encrypted_data
        
        try:
            cipher_suite = Fernet(self._get_encryption_key())
            decoded_data = base64.urlsafe_b64decode(encrypted_data.encode())
            decrypted_data = cipher_suite.decrypt(decoded_data)
            return decrypted_data.decode()
        except Exception as e:
            self.logger.error(f"Decryption failed: {e}")
            return encrypted_data
    
    def store_api_key_secure(self, api_key: str) -> bool:
        """
        Store API key using most secure method available
        
        Args:
            api_key: The API key to store
            
        Returns:
            True if stored successfully
        """
        if not api_key or not isinstance(api_key, str):
            raise ValueError("Invalid API key provided")
        
        # Validate API key format (basic check)
        if not self._validate_api_key_format(api_key):
            raise ValueError("API key format appears invalid")
        
        try:
            if KEYRING_AVAILABLE:
                # Use system keyring (most secure)
                keyring.set_password(self.service_name, "api_key", api_key)
                self.logger.info("API key stored securely in system keyring")
                return True
            else:
                # Fallback to encrypted local storage
                encrypted_key = self.encrypt_data(api_key)
                self._store_encrypted_key_locally(encrypted_key)
                self.logger.warning("API key stored with local encryption")
                return True
                
        except Exception as e:
            self.logger.error(f"Failed to store API key securely: {e}")
            return False
    
    def get_api_key_secure(self) -> Optional[str]:
        """
        Retrieve API key using most secure method available
        
        Returns:
            The API key if found, None otherwise
        """
        try:
            if KEYRING_AVAILABLE:
                # Try system keyring first
                api_key = keyring.get_password(self.service_name, "api_key")
                if api_key:
                    return api_key
            
            # Fallback to encrypted local storage
            encrypted_key = self._load_encrypted_key_locally()
            if encrypted_key:
                return self.decrypt_data(encrypted_key)
                
        except Exception as e:
            self.logger.error(f"Failed to retrieve API key: {e}")
        
        return None
    
    def remove_api_key(self) -> bool:
        """
        Remove stored API key
        
        Returns:
            True if removed successfully
        """
        try:
            success = True
            
            if KEYRING_AVAILABLE:
                try:
                    keyring.delete_password(self.service_name, "api_key")
                except keyring.errors.PasswordDeleteError:
                    pass  # Key might not exist
            
            # Also remove from local storage
            self._remove_encrypted_key_locally()
            
            self.logger.info("API key removed from secure storage")
            return success
            
        except Exception as e:
            self.logger.error(f"Failed to remove API key: {e}")
            return False
    
    def _validate_api_key_format(self, api_key: str) -> bool:
        """
        Basic validation of API key format
        
        Args:
            api_key: The API key to validate
            
        Returns:
            True if format appears valid
        """
        # Basic checks for Nexus API key format
        # Adjust pattern based on actual Nexus API key format
        if len(api_key) < 20:  # Too short
            return False
        
        # Check for reasonable characters (alphanumeric + some symbols)
        if not re.match(r'^[a-zA-Z0-9\-_=+/]+$', api_key):
            return False
        
        return True
    
    def _store_encrypted_key_locally(self, encrypted_key: str):
        """Store encrypted key in local config file"""
        config_dir = os.path.expanduser("~/.nexusdownloader")
        os.makedirs(config_dir, exist_ok=True)
        
        config_file = os.path.join(config_dir, "secure_config.json")
        config = {"encrypted_api_key": encrypted_key}
        
        with open(config_file, 'w') as f:
            json.dump(config, f)
        
        # Set restrictive permissions on Unix-like systems
        try:
            os.chmod(config_file, 0o600)
        except (OSError, AttributeError):
            pass  # Windows or permission error
    
    def _load_encrypted_key_locally(self) -> Optional[str]:
        """Load encrypted key from local config file"""
        config_file = os.path.expanduser("~/.nexusdownloader/secure_config.json")
        
        try:
            if os.path.exists(config_file):
                with open(config_file, 'r') as f:
                    config = json.load(f)
                return config.get("encrypted_api_key")
        except Exception as e:
            self.logger.error(f"Failed to load local encrypted key: {e}")
        
        return None
    
    def _remove_encrypted_key_locally(self):
        """Remove encrypted key from local storage"""
        config_file = os.path.expanduser("~/.nexusdownloader/secure_config.json")
        try:
            if os.path.exists(config_file):
                os.remove(config_file)
        except Exception as e:
            self.logger.error(f"Failed to remove local encrypted key: {e}")


class RateLimiter:
    """Implements rate limiting to respect API quotas and server policies"""
    
    def __init__(self, requests_per_minute: int = 60, 
                 requests_per_hour: int = 1000,
                 burst_allowance: int = 10):
        self.requests_per_minute = requests_per_minute
        self.requests_per_hour = requests_per_hour
        self.burst_allowance = burst_allowance
        
        # Track request timestamps
        self.request_times = deque()
        self.hourly_request_times = deque()
        
        # Server-imposed rate limiting
        self.server_rate_limit_until = None
        self.server_requests_remaining = None
        self.server_rate_limit_reset = None
        
        self.logger = logging.getLogger(__name__)
    
    def wait_if_needed(self) -> float:
        """
        Check rate limits and wait if necessary
        
        Returns:
            Time waited in seconds
        """
        current_time = time.time()
        wait_time = 0
        
        # Check server-imposed rate limiting first
        if self.server_rate_limit_until and current_time < self.server_rate_limit_until:
            wait_time = self.server_rate_limit_until - current_time
            self.logger.warning(f"Server rate limit active. Waiting {wait_time:.1f} seconds")
            time.sleep(wait_time)
            return wait_time
        
        # Clean old request timestamps (older than 1 minute)
        minute_ago = current_time - 60
        while self.request_times and self.request_times[0] < minute_ago:
            self.request_times.popleft()
        
        # Clean old hourly timestamps (older than 1 hour)
        hour_ago = current_time - 3600
        while self.hourly_request_times and self.hourly_request_times[0] < hour_ago:
            self.hourly_request_times.popleft()
        
        # Check per-minute rate limit
        if len(self.request_times) >= self.requests_per_minute:
            wait_time = 60 - (current_time - self.request_times[0])
            if wait_time > 0:
                self.logger.info(f"Per-minute rate limit reached. Waiting {wait_time:.1f} seconds")
                time.sleep(wait_time)
        
        # Check per-hour rate limit
        if len(self.hourly_request_times) >= self.requests_per_hour:
            wait_time = 3600 - (current_time - self.hourly_request_times[0])
            if wait_time > 0:
                self.logger.warning(f"Per-hour rate limit reached. Waiting {wait_time:.1f} seconds")
                time.sleep(wait_time)
        
        # Record this request
        current_time = time.time()  # Update after potential waiting
        self.request_times.append(current_time)
        self.hourly_request_times.append(current_time)
        
        return wait_time
    
    def update_from_response_headers(self, response) -> Dict[str, Any]:
        """
        Update rate limiting info from HTTP response headers
        
        Args:
            response: HTTP response object
            
        Returns:
            Dict with rate limit information
        """
        headers = response.headers
        rate_limit_info = {}
        
        # Common rate limit headers
        remaining_headers = [
            'X-RateLimit-Remaining',
            'X-Rate-Limit-Remaining',
            'RateLimit-Remaining'
        ]
        
        reset_headers = [
            'X-RateLimit-Reset',
            'X-Rate-Limit-Reset',
            'RateLimit-Reset'
        ]
        
        limit_headers = [
            'X-RateLimit-Limit',
            'X-Rate-Limit-Limit',
            'RateLimit-Limit'
        ]
        
        # Extract rate limit information
        for header in remaining_headers:
            if header in headers:
                try:
                    self.server_requests_remaining = int(headers[header])
                    rate_limit_info['remaining'] = self.server_requests_remaining
                    break
                except ValueError:
                    pass
        
        for header in reset_headers:
            if header in headers:
                try:
                    reset_time = int(headers[header])
                    # Handle both timestamp and seconds-from-now formats
                    if reset_time > time.time():
                        self.server_rate_limit_reset = reset_time
                    else:
                        self.server_rate_limit_reset = time.time() + reset_time
                    rate_limit_info['reset_time'] = self.server_rate_limit_reset
                    break
                except ValueError:
                    pass
        
        for header in limit_headers:
            if header in headers:
                try:
                    rate_limit_info['limit'] = int(headers[header])
                    break
                except ValueError:
                    pass
        
        # Handle 429 Too Many Requests
        if response.status_code == 429:
            retry_after = headers.get('Retry-After')
            if retry_after:
                try:
                    wait_seconds = int(retry_after)
                    self.server_rate_limit_until = time.time() + wait_seconds
                    rate_limit_info['retry_after'] = wait_seconds
                except ValueError:
                    # Retry-After might be a date
                    pass
            
            self.logger.warning(f"Rate limited by server: {rate_limit_info}")
        
        # Log if we're getting close to limits
        if self.server_requests_remaining is not None and self.server_requests_remaining < 10:
            self.logger.warning(f"Only {self.server_requests_remaining} requests remaining")
        
        return rate_limit_info
    
    def get_status(self) -> Dict[str, Any]:
        """Get current rate limiting status"""
        current_time = time.time()
        
        # Clean old timestamps
        minute_ago = current_time - 60
        hour_ago = current_time - 3600
        
        recent_requests = len([t for t in self.request_times if t > minute_ago])
        hourly_requests = len([t for t in self.hourly_request_times if t > hour_ago])
        
        return {
            "requests_last_minute": recent_requests,
            "requests_last_hour": hourly_requests,
            "per_minute_limit": self.requests_per_minute,
            "per_hour_limit": self.requests_per_hour,
            "server_requests_remaining": self.server_requests_remaining,
            "server_rate_limit_until": self.server_rate_limit_until,
            "server_rate_limit_reset": self.server_rate_limit_reset
        }


class InputValidator:
    """Validates and sanitizes user inputs"""
    
    @staticmethod
    def validate_file_path(file_path: str, must_exist: bool = True) -> bool:
        """
        Validate file path for security and existence
        
        Args:
            file_path: Path to validate
            must_exist: Whether file must exist
            
        Returns:
            True if valid
        """
        if not file_path or not isinstance(file_path, str):
            return False
        
        # Check for path traversal attempts
        if '..' in file_path:
            return False
        
        # Allow absolute paths on Windows (drive letters) and Unix
        import os
        if os.name == 'nt':  # Windows
            # Check for invalid characters (excluding colon for drive letters)
            invalid_chars = ['<', '>', '"', '|', '?', '*']
            # Allow colon only in drive letter position (position 1)
            if any(char in file_path for char in invalid_chars):
                return False
            # Check if colon appears in invalid positions (not at position 1)
            colon_positions = [i for i, char in enumerate(file_path) if char == ':']
            if any(pos != 1 for pos in colon_positions):
                return False
        else:  # Unix-like systems
            # Check for invalid characters
            invalid_chars = ['<', '>', ':', '"', '|', '?', '*']
            if any(char in file_path for char in invalid_chars):
                return False
            # Reject paths starting with / unless they're absolute paths
            if file_path.startswith('/') and not os.path.isabs(file_path):
                return False
        
        # Check existence if required
        if must_exist and not os.path.exists(file_path):
            return False
        
        return True
    
    @staticmethod
    def sanitize_filename(filename: str, max_length: int = 255) -> str:
        """
        Sanitize filename for safe storage
        
        Args:
            filename: Original filename
            max_length: Maximum allowed length
            
        Returns:
            Sanitized filename
        """
        if not filename:
            return "unnamed_file"
        
        # Remove or replace invalid characters
        invalid_chars = '<>:"/\\|?*'
        for char in invalid_chars:
            filename = filename.replace(char, '_')
        
        # Remove control characters
        filename = ''.join(char for char in filename if ord(char) >= 32)
        
        # Limit length
        if len(filename) > max_length:
            name, ext = os.path.splitext(filename)
            filename = name[:max_length - len(ext)] + ext
        
        # Ensure it's not empty after sanitization
        if not filename.strip():
            filename = "unnamed_file"
        
        return filename
    
    @staticmethod
    def validate_url(url: str) -> bool:
        """
        Validate URL format and security
        
        Args:
            url: URL to validate
            
        Returns:
            True if valid
        """
        if not url or not isinstance(url, str):
            return False
        
        # Basic URL format check
        url_pattern = re.compile(
            r'^https?://'  # http:// or https://
            r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'  # domain...
            r'localhost|'  # localhost...
            r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'  # ...or ip
            r'(?::\d+)?'  # optional port
            r'(?:/?|[/?]\S+)$', re.IGNORECASE)
        
        return bool(url_pattern.match(url))


# Example usage
def example_usage():
    """Example of how to use the security module"""
    
    # Initialize secure config
    secure_config = SecureConfig()
    
    # Store API key
    test_api_key = "test_api_key_12345_example"
    if secure_config.store_api_key_secure(test_api_key):
        print("API key stored securely")
    
    # Retrieve API key
    retrieved_key = secure_config.get_api_key_secure()
    if retrieved_key == test_api_key:
        print("API key retrieved successfully")
    
    # Initialize rate limiter
    rate_limiter = RateLimiter(requests_per_minute=30, requests_per_hour=500)
    
    # Example of rate limiting
    print("Testing rate limiter...")
    for i in range(5):
        wait_time = rate_limiter.wait_if_needed()
        print(f"Request {i+1}: waited {wait_time:.2f} seconds")
        time.sleep(0.1)  # Simulate some processing time
    
    # Print rate limiter status
    status = rate_limiter.get_status()
    print(f"Rate limiter status: {status}")
    
    # Test input validation
    validator = InputValidator()
    
    test_paths = [
        "valid_file.txt",
        "../../../etc/passwd",  # Path traversal attempt
        "file<>.txt",  # Invalid characters
        "normal_collection.json"
    ]
    
    print("\nTesting path validation:")
    for path in test_paths:
        is_valid = validator.validate_file_path(path, must_exist=False)
        print(f"'{path}': {'Valid' if is_valid else 'Invalid'}")
    
    # Test filename sanitization
    test_filenames = [
        "normal_file.txt",
        "file<with>bad:chars.zip",
        "very_long_filename_that_exceeds_normal_limits_and_should_be_truncated.extension"
    ]
    
    print("\nTesting filename sanitization:")
    for filename in test_filenames:
        sanitized = validator.sanitize_filename(filename, max_length=50)
        print(f"'{filename}' -> '{sanitized}'")
    
    # Clean up
    secure_config.remove_api_key()
    print("\nAPI key removed from secure storage")


if __name__ == "__main__":
    example_usage()