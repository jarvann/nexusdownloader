"""
Configuration Manager for NexusDownloader
Handles secure configuration storage, validation, and migration
"""

from __future__ import annotations
import os
import json
import threading
from pathlib import Path
from typing import Dict, Any, Callable, Optional, List
from dataclasses import dataclass, asdict
from enum import Enum
import logging

# Import our custom modules
from utils.security import SecureConfig, InputValidator
from utils.file_operations import AtomicFileWriter


class ConfigVersion(Enum):
    """Configuration version for migration support"""
    V1_0 = "1.0"
    V1_1 = "1.1" 
    CURRENT = V1_1


@dataclass
class NexusApiConfig:
    """Nexus API configuration"""
    api_key: str = ""
    base_url: str = "https://api.nexusmods.com"
    user_agent: str = "NexusDownloader/1.0"
    rate_limit_requests_per_minute: int = 60
    rate_limit_requests_per_hour: int = 1000
    timeout_seconds: int = 30


@dataclass 
class VortexConfig:
    """Vortex integration configuration"""
    downloads_folder_root: str = ""
    auto_detect_vortex_path: bool = True
    vortex_executable_path: str = ""
    check_vortex_running: bool = True
    auto_close_vortex: bool = False


@dataclass
class DownloadConfig:
    """Download behavior configuration"""
    max_concurrent_downloads: int = 4
    max_retries: int = 3
    retry_delay_seconds: float = 5.0
    chunk_size_bytes: int = 8192
    verify_checksums: bool = True
    resume_partial_downloads: bool = True
    cleanup_failed_downloads: bool = True
    # When true, verify_downloads.py validates archives by full MD5 (thorough,
    # slower) instead of the default fast byte-size check.
    verify_md5: bool = False
    # Override scratch/temp dir for extraction. Blank = system %TEMP%. Point this
    # at a drive with lots of free space if %TEMP% is a small RAM disk.
    install_temp_dir: str = ""


@dataclass
class LoggingConfig:
    """Logging configuration"""
    log_level: str = "INFO"
    log_to_file: bool = True
    log_to_console: bool = True
    log_directory: str = "logs"
    max_log_size_mb: int = 10
    log_backup_count: int = 5
    enable_performance_logging: bool = True
    enable_colored_output: bool = True


@dataclass
class SecurityConfig:
    """Security and privacy configuration"""
    use_secure_api_storage: bool = True
    enable_rate_limiting: bool = True
    validate_ssl_certificates: bool = True
    anonymize_logs: bool = False
    auto_cleanup_logs_days: int = 30


@dataclass
class UIPreferencesConfig:
    """User interface preferences and settings"""
    last_collection_directory: str = ""
    remember_collection_location: bool = True
    # Remembered Install-tab path pickers (persist across sessions).
    last_collection_file: str = ""
    last_downloads_folder: str = ""
    last_staging_folder: str = ""
    # Install/reset worker thread count (Install-tab spinbox), persisted across sessions.
    max_concurrent_installs: int = 0


@dataclass
class AppConfig:
    """Main application configuration"""
    version: str = ConfigVersion.CURRENT.value
    nexus_api: Optional[NexusApiConfig] = None
    vortex: Optional[VortexConfig] = None
    downloads: Optional[DownloadConfig] = None
    logging: Optional[LoggingConfig] = None
    security: Optional[SecurityConfig] = None
    ui_preferences: Optional[UIPreferencesConfig] = None

    def __post_init__(self):
        """Initialize nested configurations if None"""
        if self.nexus_api is None:
            self.nexus_api = NexusApiConfig()
        if self.vortex is None:
            self.vortex = VortexConfig()
        if self.downloads is None:
            self.downloads = DownloadConfig()
        if self.logging is None:
            self.logging = LoggingConfig()
        if self.security is None:
            self.security = SecurityConfig()
        if self.ui_preferences is None:
            self.ui_preferences = UIPreferencesConfig()


class ConfigurationError(Exception):
    """Custom exception for configuration errors"""


class ConfigManager:
    """Main configuration manager with secure storage and validation"""
    
    def __init__(self, config_file: str = "config.json", 
                 secure_storage: bool = True):
        self.config_file = Path(config_file)
        self.secure_storage = secure_storage
        self.logger = logging.getLogger(__name__)
        
        # Thread safety
        self._lock = threading.RLock()
        
        # Initialize secure config handler
        try:
            self.secure_config = SecureConfig() if secure_storage else None
        except Exception as e:
            self.logger.warning(f"Failed to initialize secure config: {e}")
            self.secure_config = None
            self.secure_storage = False
        
        # Initialize validator
        self.validator = InputValidator()
        
        # Current configuration
        self._config: Optional[AppConfig] = None
        
        # Configuration change callbacks
        self._change_callbacks: List[Callable[[AppConfig], None]] = []
        
        # Load or create initial configuration
        self.load_or_create_config()
    
    def load_or_create_config(self) -> AppConfig:
        """Load existing config or create default configuration"""
        try:
            with self._lock:
                if self.config_file.exists():
                    try:
                        self.logger.debug(f"Loading config from: {self.config_file}")
                        self._config = self._load_config()
                        self.logger.debug("Config loaded successfully")
                        self._migrate_config_if_needed()
                        self.logger.debug("Config migration check completed")
                    except Exception as e:
                        self.logger.error(f"Failed to load config: {e}")
                        self.logger.info("Creating backup and using default config")
                        self._backup_corrupted_config()
                        self._config = AppConfig()
                else:
                    self.logger.info("No config file found, creating default configuration")
                    self._config = AppConfig()
                    try:
                        # For initial config creation, use simple file writing
                        config_dict = self._config_to_dict(self._config)
                        with open(self.config_file, 'w', encoding='utf-8') as f:
                            json.dump(config_dict, f, indent=2, ensure_ascii=False)
                        self.logger.debug("Default config saved successfully")
                    except Exception as e:
                        self.logger.warning(f"Failed to save default config: {e}")
                
                return self._config
        except Exception as e:
            self.logger.error(f"Critical error in load_or_create_config: {e}")
            # Fallback: create minimal config
            self._config = AppConfig()
            return self._config
    
    def _load_config(self) -> AppConfig:
        """Load configuration from file"""
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                config_data = json.load(f)
            
            # Handle legacy format migration
            if 'AccessControl' in config_data or 'VortexSettings' in config_data:
                config_data = self._migrate_legacy_format(config_data)
            
            return self._dict_to_config(config_data)
            
        except json.JSONDecodeError as e:
            raise ConfigurationError(f"Invalid JSON in config file: {e}")
        except Exception as e:
            raise ConfigurationError(f"Failed to load config: {e}")
    
    def _dict_to_config(self, config_dict: Dict[str, Any]) -> AppConfig:
        """Convert dictionary to AppConfig object.

        Unknown keys within a section are dropped (with a warning) rather than
        raising -- a stray field written by an older/newer build must never brick
        startup. Only genuinely malformed structures still raise.
        """
        from dataclasses import fields as _dc_fields

        def _filtered(cls, data: Dict[str, Any]):
            known = {f.name for f in _dc_fields(cls)}
            extra = set(data) - known
            if extra:
                self.logger.warning("Ignoring unknown %s keys in config: %s",
                                    cls.__name__, ", ".join(sorted(extra)))
            return cls(**{k: v for k, v in data.items() if k in known})

        try:
            return AppConfig(
                version=config_dict.get('version', ConfigVersion.CURRENT.value),
                nexus_api=_filtered(NexusApiConfig, config_dict.get('nexus_api', {})),
                vortex=_filtered(VortexConfig, config_dict.get('vortex', {})),
                downloads=_filtered(DownloadConfig, config_dict.get('downloads', {})),
                logging=_filtered(LoggingConfig, config_dict.get('logging', {})),
                security=_filtered(SecurityConfig, config_dict.get('security', {})),
                ui_preferences=_filtered(UIPreferencesConfig,
                                         config_dict.get('ui_preferences', {})),
            )
        except TypeError as e:
            raise ConfigurationError(f"Invalid configuration structure: {e}")
    
    def _migrate_legacy_format(self, legacy_config: Dict[str, Any]) -> Dict[str, Any]:
        """Migrate legacy configuration format"""
        self.logger.info("Migrating legacy configuration format")
        
        migrated = {
            'version': ConfigVersion.CURRENT.value,
            'nexus_api': {},
            'vortex': {},
            'downloads': {},
            'logging': {},
            'security': {}
        }
        
        # Migrate legacy AccessControl section
        if 'AccessControl' in legacy_config:
            access_control = legacy_config['AccessControl']
            if 'NexusAPIKey' in access_control:
                migrated['nexus_api']['api_key'] = access_control['NexusAPIKey']
        
        # Migrate legacy VortexSettings section  
        if 'VortexSettings' in legacy_config:
            vortex_settings = legacy_config['VortexSettings']
            if 'DownloadsFolderRoot' in vortex_settings:
                migrated['vortex']['downloads_folder_root'] = vortex_settings['DownloadsFolderRoot']
        
        return migrated
    
    def _migrate_config_if_needed(self):
        """Migrate configuration to current version if needed"""
        current_version = ConfigVersion(self._config.version)
        
        if current_version != ConfigVersion.CURRENT:
            self.logger.info(f"Migrating config from {current_version.value} to {ConfigVersion.CURRENT.value}")
            
            # Create backup before migration
            backup_path = f"{self.config_file}.backup.{current_version.value}"
            self.save_config_to_file(backup_path)
            
            # Perform migration based on version
            if current_version == ConfigVersion.V1_0:
                self._migrate_v1_0_to_v1_1()
            
            # Update version
            self._config.version = ConfigVersion.CURRENT.value
            
            # Save migrated config
            self.save_config()
            
            self.logger.info("Configuration migration completed")
    
    def _migrate_v1_0_to_v1_1(self):
        """Migrate from version 1.0 to 1.1"""
        # Add any version-specific migration logic here
        # For example, new fields, changed defaults, etc.
    
    def _backup_corrupted_config(self):
        """Create backup of corrupted configuration"""
        try:
            import time
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            backup_path = f"{self.config_file}.corrupted.{timestamp}"
            
            if self.config_file.exists():
                self.config_file.rename(backup_path)
                self.logger.info(f"Corrupted config backed up to: {backup_path}")
        except Exception as e:
            self.logger.error(f"Failed to backup corrupted config: {e}")
    
    def save_config(self) -> bool:
        """Save current configuration to file"""
        return self.save_config_to_file(str(self.config_file))
    
    def save_config_to_file(self, file_path: str) -> bool:
        """Save configuration to specified file"""
        with self._lock:
            try:
                config_dict = self._config_to_dict(self._config)
                self.logger.debug(f"Config dict created, saving to: {file_path}")
                
                # Use atomic writing to prevent corruption
                with AtomicFileWriter(file_path, mode='w') as f:
                    json.dump(config_dict, f, indent=2, ensure_ascii=False)
                
                self.logger.debug(f"Configuration saved to: {file_path}")
                
                # Notify change callbacks
                try:
                    self._notify_change_callbacks()
                    self.logger.debug("Change callbacks notified")
                except Exception as callback_error:
                    self.logger.warning(f"Error in change callbacks: {callback_error}")
                
                return True
                
            except Exception as e:
                self.logger.error(f"Failed to save config: {e}")
                import traceback
                self.logger.error(f"Stack trace: {traceback.format_exc()}")
                return False
    
    def _config_to_dict(self, config: AppConfig) -> Dict[str, Any]:
        """Convert AppConfig object to dictionary"""
        return {
            'version': config.version,
            'nexus_api': asdict(config.nexus_api),
            'vortex': asdict(config.vortex), 
            'downloads': asdict(config.downloads),
            'logging': asdict(config.logging),
            'security': asdict(config.security),
            'ui_preferences': asdict(config.ui_preferences)
        }
    
    def get_config(self) -> AppConfig:
        """Get current configuration"""
        with self._lock:
            return self._config
    
    def update_config(self, **kwargs) -> bool:
        """Update configuration with new values"""
        with self._lock:
            try:
                # Update configuration sections
                for section_name, section_values in kwargs.items():
                    if hasattr(self._config, section_name):
                        section = getattr(self._config, section_name)
                        if isinstance(section_values, dict):
                            for key, value in section_values.items():
                                if hasattr(section, key):
                                    setattr(section, key, value)
                                else:
                                    self.logger.warning(f"Unknown config key: {section_name}.{key}")
                        else:
                            setattr(self._config, section_name, section_values)
                    else:
                        self.logger.warning(f"Unknown config section: {section_name}")
                
                # Validate updated configuration
                try:
                    self._validate_config()
                    self.logger.debug("Configuration validation passed")
                except Exception as validation_error:
                    self.logger.error(f"Configuration validation failed: {validation_error}")
                    return False
                
                # Save changes
                return self.save_config()
                
            except Exception as e:
                self.logger.error(f"Failed to update config: {e}")
                return False
    
    def _validate_config(self):
        """Validate configuration values"""
        config = self._config
        
        # Validate Nexus API configuration
        if config.nexus_api.api_key and len(config.nexus_api.api_key) < 10:
            raise ConfigurationError("API key appears to be invalid (too short)")
        
        try:
            if not self.validator.validate_url(config.nexus_api.base_url):
                raise ConfigurationError(f"Invalid base URL: {config.nexus_api.base_url}")
        except Exception as e:
            self.logger.warning(f"URL validation failed: {e}")
        
        if config.nexus_api.rate_limit_requests_per_minute < 1:
            raise ConfigurationError("Rate limit per minute must be at least 1")
        
        if config.nexus_api.timeout_seconds < 1:
            raise ConfigurationError("Timeout must be at least 1 second")
        
        # Validate Vortex configuration
        if config.vortex.downloads_folder_root:
            try:
                if not self.validator.validate_file_path(config.vortex.downloads_folder_root, must_exist=False):
                    raise ConfigurationError(f"Invalid downloads folder path: {config.vortex.downloads_folder_root}")
            except Exception as e:
                self.logger.warning(f"Path validation failed: {e}")
        
        # Validate download configuration
        if config.downloads.max_concurrent_downloads < 1:
            raise ConfigurationError("Max concurrent downloads must be at least 1")
        
        if config.downloads.max_concurrent_downloads > 20:
            self.logger.warning("High concurrent download count may cause performance issues")
        
        if config.downloads.max_retries < 0:
            raise ConfigurationError("Max retries cannot be negative")
        
        if config.downloads.retry_delay_seconds < 0:
            raise ConfigurationError("Retry delay cannot be negative")
        
        if config.downloads.chunk_size_bytes < 1024:
            raise ConfigurationError("Chunk size must be at least 1024 bytes")
        
        # Validate logging configuration
        valid_log_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
        if config.logging.log_level not in valid_log_levels:
            raise ConfigurationError(f"Invalid log level: {config.logging.log_level}")
        
        if config.logging.max_log_size_mb < 1:
            raise ConfigurationError("Max log size must be at least 1 MB")
        
        if config.logging.log_backup_count < 0:
            raise ConfigurationError("Log backup count cannot be negative")
        
        # Validate security configuration
        if config.security.auto_cleanup_logs_days < 1:
            raise ConfigurationError("Auto cleanup days must be at least 1")
    
    def get_api_key(self) -> Optional[str]:
        """Get API key using secure storage if available"""
        with self._lock:
            if self.secure_storage and self.secure_config:
                # Try to get from secure storage first
                secure_key = self.secure_config.get_api_key_secure()
                if secure_key:
                    return secure_key
            
            # Fallback to config file
            return self._config.nexus_api.api_key
    
    def set_api_key(self, api_key: str) -> bool:
        """Set API key using secure storage if available"""
        with self._lock:
            try:
                if self.secure_storage and self.secure_config:
                    # Store in secure storage
                    if self.secure_config.store_api_key_secure(api_key):
                        # Clear from config file for security
                        self._config.nexus_api.api_key = ""
                        # Save the change to the main config file
                        return self.save_config()
                
                # Fallback to config file storage
                self._config.nexus_api.api_key = api_key
                return self.save_config()
                
            except Exception as e:
                self.logger.error(f"Failed to set API key: {e}")
                return False
    
    def remove_api_key(self) -> bool:
        """Remove API key from all storage locations"""
        with self._lock:
            try:
                success = True
                
                # Remove from secure storage
                if self.secure_storage and self.secure_config:
                    self.secure_config.remove_api_key()
                
                # Remove from config file
                self._config.nexus_api.api_key = ""
                # Use simple file writing to avoid timeout
                config_dict = self._config_to_dict(self._config)
                with open(self.config_file, 'w', encoding='utf-8') as f:
                    json.dump(config_dict, f, indent=2, ensure_ascii=False)
                
                return success
                
            except Exception as e:
                self.logger.error(f"Failed to remove API key: {e}")
                return False
    
    def get_downloads_path(self, game_folder: str) -> str:
        """Get downloads path for a specific game"""
        base_path = self._config.vortex.downloads_folder_root
        if not base_path:
            # Use default path if not configured
            base_path = os.path.expanduser("~/Downloads/NexusDownloader")
        
        return os.path.join(base_path, game_folder)
    
    def auto_detect_vortex_settings(self) -> bool:
        """Auto-detect Vortex installation and settings"""
        try:
            # Only try winreg on Windows
            if os.name != 'nt':
                self.logger.info("Auto-detection only supported on Windows")
                return False
            
            import winreg
            
            # Try to find Vortex through Windows registry
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Vortex") as key:
                    vortex_path = winreg.QueryValueEx(key, "InstallPath")[0]
                    self._config.vortex.vortex_executable_path = os.path.join(vortex_path, "Vortex.exe")
                    
                    # Try to find default downloads path
                    downloads_path = os.path.join(vortex_path, "downloads")
                    if os.path.exists(downloads_path):
                        self._config.vortex.downloads_folder_root = downloads_path
                    
                    self.save_config()
                    self.logger.info(f"Auto-detected Vortex installation: {vortex_path}")
                    return True
                    
            except FileNotFoundError:
                pass
            
            # Fallback: Check common installation locations
            common_paths = [
                os.path.expanduser(r"~\AppData\Local\Black Tree Gaming Ltd\Vortex"),
                r"C:\Program Files\Black Tree Gaming Ltd\Vortex",
                os.path.expanduser(r"~\AppData\Roaming\Vortex")
            ]
            
            for path in common_paths:
                vortex_exe = os.path.join(path, "Vortex.exe")
                if os.path.exists(vortex_exe):
                    self._config.vortex.vortex_executable_path = vortex_exe
                    
                    downloads_path = os.path.join(path, "downloads")
                    if os.path.exists(downloads_path):
                        self._config.vortex.downloads_folder_root = downloads_path
                    
                    self.save_config()
                    self.logger.info(f"Auto-detected Vortex installation: {path}")
                    return True
            
            self.logger.warning("Could not auto-detect Vortex installation")
            return False
            
        except Exception as e:
            self.logger.error(f"Failed to auto-detect Vortex settings: {e}")
            return False
    
    def add_change_callback(self, callback: Callable[[AppConfig], None]):
        """Add callback to be notified of configuration changes"""
        self._change_callbacks.append(callback)
    
    def remove_change_callback(self, callback: Callable[[AppConfig], None]):
        """Remove configuration change callback"""
        if callback in self._change_callbacks:
            self._change_callbacks.remove(callback)
    
    def _notify_change_callbacks(self):
        """Notify all registered callbacks of configuration changes"""
        for callback in self._change_callbacks:
            try:
                callback(self._config)
            except Exception as e:
                self.logger.error(f"Error in config change callback: {e}")
    
    def reset_to_defaults(self) -> bool:
        """Reset configuration to default values"""
        with self._lock:
            try:
                # Preserve API key if it exists
                current_api_key = self.get_api_key()
                
                # Reset to defaults
                self._config = AppConfig()
                
                # Restore API key
                if current_api_key:
                    self.set_api_key(current_api_key)
                
                return self.save_config()
                
            except Exception as e:
                self.logger.error(f"Failed to reset config to defaults: {e}")
                return False
    
    def export_config(self, export_path: str, include_sensitive: bool = False) -> bool:
        """Export configuration to a file"""
        try:
            config_dict = self._config_to_dict(self._config)
            
            # Remove sensitive information if requested
            if not include_sensitive:
                config_dict['nexus_api']['api_key'] = "[REDACTED]"
            
            with open(export_path, 'w', encoding='utf-8') as f:
                json.dump(config_dict, f, indent=2, ensure_ascii=False)
            
            self.logger.info(f"Configuration exported to: {export_path}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to export config: {e}")
            return False
    
    def import_config(self, import_path: str, preserve_api_key: bool = True) -> bool:
        """Import configuration from a file"""
        with self._lock:
            try:
                # Backup current API key if preserving
                current_api_key = None
                if preserve_api_key:
                    current_api_key = self.get_api_key()
                
                # Load and validate imported config
                with open(import_path, 'r', encoding='utf-8') as f:
                    imported_data = json.load(f)
                
                imported_config = self._dict_to_config(imported_data)
                
                # Update current config
                self._config = imported_config
                
                # Restore API key if preserving
                if preserve_api_key and current_api_key:
                    self.set_api_key(current_api_key)
                
                # Validate and save
                self._validate_config()
                return self.save_config()
                
            except Exception as e:
                self.logger.error(f"Failed to import config: {e}")
                return False
    
    def get_config_summary(self) -> Dict[str, Any]:
        """Get a summary of current configuration for display"""
        config = self._config
        
        return {
            "version": config.version,
            "api_key_configured": bool(self.get_api_key()),
            "downloads_folder": config.vortex.downloads_folder_root or "[Not Set]",
            "max_concurrent_downloads": config.downloads.max_concurrent_downloads,
            "rate_limit_per_minute": config.nexus_api.rate_limit_requests_per_minute,
            "checksum_verification": config.downloads.verify_checksums,
            "secure_storage": self.secure_storage,
            "log_level": config.logging.log_level,
            "vortex_integration": bool(config.vortex.vortex_executable_path)
        }


# Global configuration manager instance
_global_config_manager: Optional[ConfigManager] = None

def get_config_manager(config_file: str = "config.json") -> ConfigManager:
    """Get the global configuration manager instance"""
    global _global_config_manager
    if _global_config_manager is None:
        _global_config_manager = ConfigManager(config_file)
    return _global_config_manager

def setup_config_manager(config_file: str = "config.json", 
                        secure_storage: bool = True) -> ConfigManager:
    """Setup global configuration manager"""
    global _global_config_manager
    _global_config_manager = ConfigManager(config_file, secure_storage)
    return _global_config_manager


# Example usage
def example_usage():
    """Example of how to use the configuration manager"""
    
    # Setup logging
    logging.basicConfig(level=logging.INFO)

    # Create configuration manager
    config_manager = ConfigManager("example_config.json")
    
    # Get current configuration
    config = config_manager.get_config()
    print(f"Current config version: {config.version}")
    
    # Update some settings
    config_manager.update_config(
        downloads={'max_concurrent_downloads': 6},
        logging={'log_level': 'DEBUG'}
    )
    
    # Set API key securely
    test_api_key = "example_api_key_12345"
    if config_manager.set_api_key(test_api_key):
        print("API key set successfully")
    
    # Get API key
    retrieved_key = config_manager.get_api_key()
    print(f"Retrieved API key: {retrieved_key[:10]}..." if retrieved_key else "No API key")
    
    # Auto-detect Vortex (may not work in demo environment)
    if config_manager.auto_detect_vortex_settings():
        print("Vortex settings auto-detected")
    else:
        print("Could not auto-detect Vortex settings")
    
    # Get configuration summary
    summary = config_manager.get_config_summary()
    print(f"Configuration summary: {summary}")
    
    # Export configuration
    config_manager.export_config("exported_config.json", include_sensitive=False)
    print("Configuration exported")
    
    # Clean up
    try:
        os.unlink("example_config.json")
        os.unlink("exported_config.json")
        config_manager.remove_api_key()
    except:
        pass


if __name__ == "__main__":
    example_usage()