"""
Settings dialog for NexusDownloader configuration management.

This module provides a comprehensive settings interface for managing all
application configuration options including API settings, download preferences,
Vortex integration, logging, and security options.
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget, QFormLayout,
    QLineEdit, QPushButton, QSpinBox, QCheckBox, QComboBox, QGroupBox,
    QDialogButtonBox, QFileDialog, QMessageBox, QLabel
)
from typing import Optional
import logging

# Import Phase 1 modules
try:
    from config.config_manager import ConfigManager
    from utils.security import InputValidator
    PHASE1_AVAILABLE = True
except ImportError:
    PHASE1_AVAILABLE = False
    # Fallback for critical classes if imports fail
    class ConfigManager: pass
    class InputValidator: pass


class SettingsDialog(QDialog):
    """
    Comprehensive settings dialog for application configuration.
    
    Provides a tabbed interface for managing all aspects of the application
    configuration including API credentials, download settings, Vortex integration,
    logging preferences, and security options.
    """
    
    def __init__(self, config_manager: Optional['ConfigManager'] = None, parent=None):
        """
        Initialize the settings dialog.
        
        Args:
            config_manager: Configuration manager instance
            parent: Parent widget
        """
        super().__init__(parent)
        self.setWindowTitle("NexusDownloader Settings")
        self.setMinimumSize(600, 500)
        self.config_manager = config_manager
        self.validator = InputValidator() if PHASE1_AVAILABLE else None
        self.logger = logging.getLogger(__name__)
        
        self.logger.debug("Initializing settings dialog")
        self._setup_user_interface()
        self._load_current_configuration()
        self.logger.debug("Settings dialog initialization completed")

    def _setup_user_interface(self):
        """Setup the dialog user interface with tabbed sections."""
        layout = QVBoxLayout(self)
        
        # Create tabbed interface for organized settings
        self.tab_widget = QTabWidget()
        
        # Create configuration tabs
        self.tab_widget.addTab(self._create_api_tab(), "API & Authentication")
        self.tab_widget.addTab(self._create_downloads_tab(), "Downloads")
        self.tab_widget.addTab(self._create_vortex_tab(), "Vortex Integration")
        self.tab_widget.addTab(self._create_logging_tab(), "Logging")
        self.tab_widget.addTab(self._create_security_tab(), "Security")
        
        layout.addWidget(self.tab_widget)
        
        # Dialog action buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.Apply
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        button_box.button(QDialogButtonBox.Apply).clicked.connect(self._apply_settings)
        layout.addWidget(button_box)

    def _create_api_tab(self) -> QWidget:
        """
        Create API and authentication configuration tab.
        
        Returns:
            QWidget containing API configuration controls
        """
        widget = QWidget()
        layout = QFormLayout(widget)
        
        # API Key configuration with security features
        api_key_group = QGroupBox("Nexus API Configuration")
        api_layout = QFormLayout(api_key_group)
        
        # API Key input with show/hide functionality
        api_key_container = QHBoxLayout()
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        api_key_container.addWidget(self.api_key_edit)
        
        self.show_api_key_button = QPushButton("Show")
        self.show_api_key_button.setCheckable(True)
        self.show_api_key_button.toggled.connect(self._toggle_api_key_visibility)
        api_key_container.addWidget(self.show_api_key_button)
        
        api_layout.addRow("API Key:", api_key_container)
        
        # API Base URL
        self.base_url_edit = QLineEdit()
        api_layout.addRow("Base URL:", self.base_url_edit)
        
        # User Agent string
        self.user_agent_edit = QLineEdit()
        api_layout.addRow("User Agent:", self.user_agent_edit)
        
        layout.addRow(api_key_group)
        
        # Rate limiting configuration
        rate_limit_group = QGroupBox("Rate Limiting")
        rate_layout = QFormLayout(rate_limit_group)
        
        self.rate_limit_minute = QSpinBox()
        self.rate_limit_minute.setRange(1, 1000)
        self.rate_limit_minute.setSuffix(" requests/min")
        rate_layout.addRow("Per Minute:", self.rate_limit_minute)
        
        self.rate_limit_hour = QSpinBox()
        self.rate_limit_hour.setRange(1, 10000)
        self.rate_limit_hour.setSuffix(" requests/hour")
        rate_layout.addRow("Per Hour:", self.rate_limit_hour)
        
        self.timeout_seconds = QSpinBox()
        self.timeout_seconds.setRange(1, 300)
        self.timeout_seconds.setSuffix(" seconds")
        rate_layout.addRow("Timeout:", self.timeout_seconds)
        
        layout.addRow(rate_limit_group)
        
        return widget

    def _create_downloads_tab(self) -> QWidget:
        """
        Create download configuration tab.
        
        Returns:
            QWidget containing download configuration controls
        """
        widget = QWidget()
        layout = QFormLayout(widget)
        
        # Concurrency settings
        concurrency_group = QGroupBox("Download Concurrency")
        concurrency_layout = QFormLayout(concurrency_group)
        
        self.max_concurrent_downloads = QSpinBox()
        self.max_concurrent_downloads.setRange(1, 50)
        concurrency_layout.addRow("Max Concurrent Downloads:", self.max_concurrent_downloads)
        
        layout.addRow(concurrency_group)
        
        # Retry configuration
        retry_group = QGroupBox("Error Handling")
        retry_layout = QFormLayout(retry_group)
        
        self.max_retries = QSpinBox()
        self.max_retries.setRange(0, 10)
        retry_layout.addRow("Max Retries:", self.max_retries)
        
        self.retry_delay = QSpinBox()
        self.retry_delay.setRange(1, 60)
        self.retry_delay.setSuffix(" seconds")
        retry_layout.addRow("Retry Delay:", self.retry_delay)
        
        layout.addRow(retry_group)
        
        # Performance settings
        performance_group = QGroupBox("Performance")
        performance_layout = QFormLayout(performance_group)
        
        self.chunk_size = QSpinBox()
        self.chunk_size.setRange(1024, 1048576)
        self.chunk_size.setSuffix(" bytes")
        performance_layout.addRow("Chunk Size:", self.chunk_size)

        layout.addRow(performance_group)

        # Installation scratch / temp directory override
        temp_group = QGroupBox("Installation Temp Folder")
        temp_layout = QFormLayout(temp_group)

        temp_container = QHBoxLayout()
        self.install_temp_dir_edit = QLineEdit()
        self.install_temp_dir_edit.setPlaceholderText("(blank = system %TEMP%)")
        temp_container.addWidget(self.install_temp_dir_edit)
        temp_browse_button = QPushButton("Browse...")
        temp_browse_button.clicked.connect(self._browse_install_temp_dir)
        temp_container.addWidget(temp_browse_button)
        temp_layout.addRow("Temp/Scratch Folder:", temp_container)

        temp_hint = QLabel("Where archives are unpacked during install. Point this at "
                           "a drive with plenty of free space if your %TEMP% is a "
                           "small RAM disk.")
        temp_hint.setWordWrap(True)
        temp_layout.addRow(temp_hint)

        layout.addRow(temp_group)
        
        # Download options
        options_group = QGroupBox("Download Options")
        options_layout = QVBoxLayout(options_group)
        
        self.verify_checksums = QCheckBox("Verify file checksums")
        options_layout.addWidget(self.verify_checksums)
        
        self.resume_downloads = QCheckBox("Resume partial downloads")
        options_layout.addWidget(self.resume_downloads)
        
        self.cleanup_failed = QCheckBox("Cleanup failed downloads")
        options_layout.addWidget(self.cleanup_failed)
        
        layout.addRow(options_group)
        
        return widget

    def _create_vortex_tab(self) -> QWidget:
        """
        Create Vortex integration configuration tab.
        
        Returns:
            QWidget containing Vortex configuration controls
        """
        widget = QWidget()
        layout = QFormLayout(widget)
        
        # Folder configuration
        folders_group = QGroupBox("Folder Configuration")
        folders_layout = QFormLayout(folders_group)
        
        # Downloads folder selection
        downloads_folder_container = QHBoxLayout()
        self.downloads_folder_edit = QLineEdit()
        downloads_folder_container.addWidget(self.downloads_folder_edit)
        
        downloads_browse_button = QPushButton("Browse...")
        downloads_browse_button.clicked.connect(self._browse_downloads_folder)
        downloads_folder_container.addWidget(downloads_browse_button)
        
        folders_layout.addRow("Downloads Folder:", downloads_folder_container)
        
        # Vortex executable selection
        vortex_exe_container = QHBoxLayout()
        self.vortex_executable_edit = QLineEdit()
        vortex_exe_container.addWidget(self.vortex_executable_edit)
        
        vortex_browse_button = QPushButton("Browse...")
        vortex_browse_button.clicked.connect(self._browse_vortex_executable)
        vortex_exe_container.addWidget(vortex_browse_button)
        
        vortex_auto_detect_button = QPushButton("Auto-detect")
        vortex_auto_detect_button.clicked.connect(self._auto_detect_vortex)
        vortex_exe_container.addWidget(vortex_auto_detect_button)
        
        folders_layout.addRow("Vortex Executable:", vortex_exe_container)
        
        layout.addRow(folders_group)
        
        # Integration options
        integration_group = QGroupBox("Integration Options")
        integration_layout = QVBoxLayout(integration_group)
        
        self.auto_detect_vortex_path = QCheckBox("Auto-detect Vortex installation")
        integration_layout.addWidget(self.auto_detect_vortex_path)
        
        self.check_vortex_running = QCheckBox("Check if Vortex is running before downloads")
        integration_layout.addWidget(self.check_vortex_running)
        
        self.auto_close_vortex = QCheckBox("Automatically close Vortex before downloads")
        integration_layout.addWidget(self.auto_close_vortex)
        
        layout.addRow(integration_group)
        
        return widget

    def _create_logging_tab(self) -> QWidget:
        """
        Create logging configuration tab.
        
        Returns:
            QWidget containing logging configuration controls
        """
        widget = QWidget()
        layout = QFormLayout(widget)
        
        # Log level configuration
        level_group = QGroupBox("Log Level")
        level_layout = QFormLayout(level_group)
        
        self.log_level_combo = QComboBox()
        self.log_level_combo.addItems(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
        level_layout.addRow("Log Level:", self.log_level_combo)
        
        layout.addRow(level_group)
        
        # Log file configuration
        file_group = QGroupBox("Log Files")
        file_layout = QFormLayout(file_group)
        
        # Log directory selection
        log_dir_container = QHBoxLayout()
        self.log_directory_edit = QLineEdit()
        log_dir_container.addWidget(self.log_directory_edit)
        
        log_dir_browse_button = QPushButton("Browse...")
        log_dir_browse_button.clicked.connect(self._browse_log_directory)
        log_dir_container.addWidget(log_dir_browse_button)
        
        file_layout.addRow("Log Directory:", log_dir_container)
        
        self.max_log_size = QSpinBox()
        self.max_log_size.setRange(1, 1000)
        self.max_log_size.setSuffix(" MB")
        file_layout.addRow("Max Log File Size:", self.max_log_size)
        
        self.log_backup_count = QSpinBox()
        self.log_backup_count.setRange(1, 50)
        file_layout.addRow("Log Backup Count:", self.log_backup_count)
        
        layout.addRow(file_group)
        
        # Logging options
        options_group = QGroupBox("Logging Options")
        options_layout = QVBoxLayout(options_group)
        
        self.log_to_file = QCheckBox("Enable file logging")
        options_layout.addWidget(self.log_to_file)
        
        self.log_to_console = QCheckBox("Enable console logging")
        options_layout.addWidget(self.log_to_console)
        
        self.colored_output = QCheckBox("Use colored console output")
        options_layout.addWidget(self.colored_output)
        
        self.performance_logging = QCheckBox("Enable performance logging")
        options_layout.addWidget(self.performance_logging)
        
        layout.addRow(options_group)
        
        return widget

    def _create_security_tab(self) -> QWidget:
        """
        Create security configuration tab.
        
        Returns:
            QWidget containing security configuration controls
        """
        widget = QWidget()
        layout = QFormLayout(widget)
        
        # Security options
        security_group = QGroupBox("Security Settings")
        security_layout = QVBoxLayout(security_group)
        
        self.secure_api_storage = QCheckBox("Use secure API key storage (system keyring)")
        security_layout.addWidget(self.secure_api_storage)
        
        self.enable_rate_limiting = QCheckBox("Enable API rate limiting")
        security_layout.addWidget(self.enable_rate_limiting)
        
        self.validate_ssl_certificates = QCheckBox("Validate SSL certificates")
        security_layout.addWidget(self.validate_ssl_certificates)
        
        self.anonymize_logs = QCheckBox("Anonymize sensitive data in logs")
        security_layout.addWidget(self.anonymize_logs)
        
        layout.addRow(security_group)
        
        # Cleanup settings
        cleanup_group = QGroupBox("Data Cleanup")
        cleanup_layout = QFormLayout(cleanup_group)
        
        self.auto_cleanup_days = QSpinBox()
        self.auto_cleanup_days.setRange(1, 365)
        self.auto_cleanup_days.setSuffix(" days")
        cleanup_layout.addRow("Auto-cleanup logs after:", self.auto_cleanup_days)
        
        layout.addRow(cleanup_group)
        
        return widget

    def _load_current_configuration(self):
        """Load current configuration values into the form fields."""
        if not self.config_manager:
            return
            
        try:
            config = self.config_manager.get_config()
            
            # Load API settings
            self.api_key_edit.setText(self.config_manager.get_api_key() or "")
            self.base_url_edit.setText(config.nexus_api.base_url)
            self.user_agent_edit.setText(config.nexus_api.user_agent)
            self.rate_limit_minute.setValue(config.nexus_api.rate_limit_requests_per_minute)
            self.rate_limit_hour.setValue(config.nexus_api.rate_limit_requests_per_hour)
            self.timeout_seconds.setValue(config.nexus_api.timeout_seconds)
            
            # Load download settings
            self.max_concurrent_downloads.setValue(config.downloads.max_concurrent_downloads)
            self.max_retries.setValue(config.downloads.max_retries)
            self.retry_delay.setValue(int(config.downloads.retry_delay_seconds))
            self.chunk_size.setValue(config.downloads.chunk_size_bytes)
            self.verify_checksums.setChecked(config.downloads.verify_checksums)
            self.resume_downloads.setChecked(config.downloads.resume_partial_downloads)
            self.cleanup_failed.setChecked(config.downloads.cleanup_failed_downloads)
            self.install_temp_dir_edit.setText(config.downloads.install_temp_dir)
            
            # Load Vortex settings
            self.downloads_folder_edit.setText(config.vortex.downloads_folder_root)
            self.vortex_executable_edit.setText(config.vortex.vortex_executable_path)
            self.auto_detect_vortex_path.setChecked(config.vortex.auto_detect_vortex_path)
            self.check_vortex_running.setChecked(config.vortex.check_vortex_running)
            self.auto_close_vortex.setChecked(config.vortex.auto_close_vortex)
            
            # Load logging settings
            self.log_level_combo.setCurrentText(config.logging.log_level)
            self.log_directory_edit.setText(config.logging.log_directory)
            self.max_log_size.setValue(config.logging.max_log_size_mb)
            self.log_backup_count.setValue(config.logging.log_backup_count)
            self.log_to_file.setChecked(config.logging.log_to_file)
            self.log_to_console.setChecked(config.logging.log_to_console)
            self.colored_output.setChecked(config.logging.enable_colored_output)
            self.performance_logging.setChecked(config.logging.enable_performance_logging)
            
            # Load security settings
            self.secure_api_storage.setChecked(config.security.use_secure_api_storage)
            self.enable_rate_limiting.setChecked(config.security.enable_rate_limiting)
            self.validate_ssl_certificates.setChecked(config.security.validate_ssl_certificates)
            self.anonymize_logs.setChecked(config.security.anonymize_logs)
            self.auto_cleanup_days.setValue(config.security.auto_cleanup_logs_days)
            
        except Exception as e:
            QMessageBox.warning(self, "Configuration Error", 
                              f"Failed to load configuration: {str(e)}")

    def _apply_settings(self):
        """Apply current settings without closing the dialog."""
        self.logger.info("Applying settings changes")
        try:
            self._save_configuration()
            self.logger.info("Settings applied successfully")
            QMessageBox.information(self, "Settings Applied", 
                                  "Configuration has been saved successfully.")
        except Exception as e:
            self.logger.error(f"Failed to apply settings: {e}")
            QMessageBox.critical(self, "Save Error", 
                               f"Failed to save configuration: {str(e)}")

    def _save_configuration(self):
        """Save form data to the configuration manager."""
        if not self.config_manager:
            return False
            
        try:
            # Validate critical inputs
            if self.validator:
                self._validate_configuration_inputs()
            
            # Update configuration sections
            self.config_manager.update_config(
                nexus_api={
                    'base_url': self.base_url_edit.text().strip(),
                    'user_agent': self.user_agent_edit.text().strip(),
                    'rate_limit_requests_per_minute': self.rate_limit_minute.value(),
                    'rate_limit_requests_per_hour': self.rate_limit_hour.value(),
                    'timeout_seconds': self.timeout_seconds.value()
                },
                downloads={
                    'max_concurrent_downloads': self.max_concurrent_downloads.value(),
                    'max_retries': self.max_retries.value(),
                    'retry_delay_seconds': float(self.retry_delay.value()),
                    'chunk_size_bytes': self.chunk_size.value(),
                    'verify_checksums': self.verify_checksums.isChecked(),
                    'resume_partial_downloads': self.resume_downloads.isChecked(),
                    'cleanup_failed_downloads': self.cleanup_failed.isChecked(),
                    'install_temp_dir': self.install_temp_dir_edit.text().strip()
                },
                vortex={
                    'downloads_folder_root': self.downloads_folder_edit.text().strip(),
                    'vortex_executable_path': self.vortex_executable_edit.text().strip(),
                    'auto_detect_vortex_path': self.auto_detect_vortex_path.isChecked(),
                    'check_vortex_running': self.check_vortex_running.isChecked(),
                    'auto_close_vortex': self.auto_close_vortex.isChecked()
                },
                logging={
                    'log_level': self.log_level_combo.currentText(),
                    'log_directory': self.log_directory_edit.text().strip(),
                    'max_log_size_mb': self.max_log_size.value(),
                    'log_backup_count': self.log_backup_count.value(),
                    'log_to_file': self.log_to_file.isChecked(),
                    'log_to_console': self.log_to_console.isChecked(),
                    'enable_colored_output': self.colored_output.isChecked(),
                    'enable_performance_logging': self.performance_logging.isChecked()
                },
                security={
                    'use_secure_api_storage': self.secure_api_storage.isChecked(),
                    'enable_rate_limiting': self.enable_rate_limiting.isChecked(),
                    'validate_ssl_certificates': self.validate_ssl_certificates.isChecked(),
                    'anonymize_logs': self.anonymize_logs.isChecked(),
                    'auto_cleanup_logs_days': self.auto_cleanup_days.value()
                }
            )
            
            # Save API key separately for secure storage
            api_key = self.api_key_edit.text().strip()
            if api_key:
                self.config_manager.set_api_key(api_key)
            
            return True
            
        except Exception as e:
            raise Exception(f"Configuration validation failed: {str(e)}")

    def _validate_configuration_inputs(self):
        """Validate configuration inputs using the input validator."""
        # Validate API key format
        api_key = self.api_key_edit.text().strip()
        if api_key and len(api_key) < 10:
            raise ValueError("API key appears to be too short")
        
        # Validate base URL format
        base_url = self.base_url_edit.text().strip()
        if base_url and not self.validator.validate_url(base_url):
            raise ValueError("Invalid base URL format")

    def _toggle_api_key_visibility(self, show: bool):
        """
        Toggle API key visibility in the input field.
        
        Args:
            show: True to show API key, False to hide
        """
        if show:
            self.api_key_edit.setEchoMode(QLineEdit.Normal)
            self.show_api_key_button.setText("Hide")
        else:
            self.api_key_edit.setEchoMode(QLineEdit.Password)
            self.show_api_key_button.setText("Show")

    def _browse_downloads_folder(self):
        """Browse for and select the downloads folder."""
        folder = QFileDialog.getExistingDirectory(self, "Select Downloads Folder")
        if folder:
            self.downloads_folder_edit.setText(folder)

    def _browse_install_temp_dir(self):
        """Browse for and select the installation temp/scratch folder."""
        folder = QFileDialog.getExistingDirectory(self, "Select Installation Temp Folder")
        if folder:
            self.install_temp_dir_edit.setText(folder)

    def _browse_vortex_executable(self):
        """Browse for and select the Vortex executable."""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Vortex Executable", "", 
            "Executable Files (*.exe);;All Files (*)"
        )
        if file_path:
            self.vortex_executable_edit.setText(file_path)

    def _browse_log_directory(self):
        """Browse for and select the log directory."""
        folder = QFileDialog.getExistingDirectory(self, "Select Log Directory")
        if folder:
            self.log_directory_edit.setText(folder)

    def _auto_detect_vortex(self):
        """Attempt to auto-detect Vortex installation."""
        if self.config_manager:
            try:
                success = self.config_manager.auto_detect_vortex_settings()
                if success:
                    # Reload Vortex settings from config
                    config = self.config_manager.get_config()
                    self.vortex_executable_edit.setText(config.vortex.vortex_executable_path)
                    if config.vortex.downloads_folder_root:
                        self.downloads_folder_edit.setText(config.vortex.downloads_folder_root)
                    
                    QMessageBox.information(self, "Auto-detect Successful", 
                                          "Vortex installation detected and configured.")
                else:
                    QMessageBox.warning(self, "Auto-detect Failed", 
                                      "Could not detect Vortex installation automatically.")
            except Exception as e:
                QMessageBox.critical(self, "Auto-detect Error", 
                                   f"Failed to auto-detect Vortex: {str(e)}")

    def accept(self):
        """Accept dialog and save configuration."""
        try:
            self._save_configuration()
            super().accept()
        except Exception as e:
            QMessageBox.critical(self, "Save Error", 
                               f"Failed to save configuration: {str(e)}")