"""
Progress monitoring widget for displaying active download information.

This module provides a comprehensive widget for monitoring multiple concurrent
downloads with real-time updates, thread utilization tracking, and intelligent
display scaling based on the number of active operations.
"""

from PySide6.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QHBoxLayout, QTableWidget, 
    QTableWidgetItem, QProgressBar, QHeaderView, QGroupBox,
    QScrollArea, QFrame, QCheckBox, QPushButton
)
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont, QColor
from typing import List, Dict, Any
from datetime import timedelta
import time

from progress_tracking import DownloadProgress


class ProgressMonitorWidget(QWidget):
    """
    Widget for monitoring active download progress with dynamic scaling.
    
    Provides real-time visualization of concurrent downloads including individual
    progress bars, transfer speeds, estimated completion times, and thread utilization.
    Automatically adapts display based on the number of active downloads and configured
    thread count.
    """
    
    # Signal emitted when user requests thread count change
    thread_count_change_requested = Signal(int)
    
    def __init__(self, initial_max_threads: int = 10):
        """
        Initialize the progress monitor widget.
        
        Args:
            initial_max_threads: Initial maximum thread count for display scaling
        """
        super().__init__()
        self.max_threads = initial_max_threads
        self.active_downloads = {}
        self.display_mode = "standard"  # standard, compact
        
        self._setup_user_interface()
        self._setup_update_timer()
        
    def _setup_user_interface(self):
        """Setup the user interface components."""
        layout = QVBoxLayout(self)
        
        # Header section with controls and information
        header_section = self._create_header_section()
        layout.addWidget(header_section)
        
        # Main downloads table with scrolling support
        downloads_area = self._create_downloads_table()
        layout.addWidget(downloads_area)
        
        # Summary statistics section
        summary_section = self._create_summary_section()
        layout.addWidget(summary_section)
        
    def _create_header_section(self) -> QWidget:
        """
        Create header section with thread information and display controls.
        
        Returns:
            QWidget containing header controls
        """
        header = QGroupBox("Download Monitor")
        layout = QHBoxLayout(header)
        
        # Thread configuration display
        self.thread_info_label = QLabel(f"Max Threads: {self.max_threads}")
        self.thread_info_label.setFont(QFont("Arial", 9, QFont.Bold))
        layout.addWidget(self.thread_info_label)
        
        # Active download count
        self.active_count_label = QLabel("Active: 0")
        layout.addWidget(self.active_count_label)
        
        layout.addStretch()
        
        # Display options
        self.compact_mode_checkbox = QCheckBox("Compact View")
        self.compact_mode_checkbox.toggled.connect(self._toggle_display_mode)
        layout.addWidget(self.compact_mode_checkbox)
        
        self.auto_resize_checkbox = QCheckBox("Auto-resize")
        self.auto_resize_checkbox.setChecked(True)
        layout.addWidget(self.auto_resize_checkbox)
        
        return header
        
    def _create_downloads_table(self) -> QWidget:
        """
        Create the main downloads table with scrolling capability.
        
        Returns:
            QWidget containing the downloads table
        """
        # Scroll area for dynamic content
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        
        # Main downloads table
        self.downloads_table = QTableWidget()
        self.downloads_table.setColumnCount(6)
        self.downloads_table.setHorizontalHeaderLabels([
            "File", "Progress", "Speed", "ETA", "Thread", "Status"
        ])
        
        self._configure_table_appearance()
        
        scroll_area.setWidget(self.downloads_table)
        return scroll_area
        
    def _configure_table_appearance(self):
        """Configure table appearance and column sizing."""
        header = self.downloads_table.horizontalHeader()
        
        # Configure column resize behavior
        header.setSectionResizeMode(0, QHeaderView.Stretch)  # File name stretches
        header.setSectionResizeMode(1, QHeaderView.Fixed)    # Progress fixed width
        header.setSectionResizeMode(2, QHeaderView.Fixed)    # Speed fixed width
        header.setSectionResizeMode(3, QHeaderView.Fixed)    # ETA fixed width
        header.setSectionResizeMode(4, QHeaderView.Fixed)    # Thread fixed width
        header.setSectionResizeMode(5, QHeaderView.Fixed)    # Status fixed width
        
        # Set fixed column widths
        self._apply_column_widths_for_mode(self.display_mode)
        
        # Table visual properties
        self.downloads_table.setAlternatingRowColors(True)
        self.downloads_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.downloads_table.setShowGrid(True)
        self.downloads_table.verticalHeader().setVisible(False)
        
    def _apply_column_widths_for_mode(self, mode: str):
        """
        Apply column widths appropriate for the display mode.
        
        Args:
            mode: Display mode ("standard" or "compact")
        """
        if mode == "compact":
            widths = [100, 60, 50, 60, 50, 70]  # Compact widths
        else:
            widths = [120, 80, 60, 80, 60, 80]  # Standard widths
            
        for i, width in enumerate(widths[1:], 1):  # Skip first column (stretches)
            self.downloads_table.setColumnWidth(i, width)
            
    def _create_summary_section(self) -> QWidget:
        """
        Create summary statistics section.
        
        Returns:
            QWidget containing summary information
        """
        summary_frame = QFrame()
        summary_frame.setFrameStyle(QFrame.Box)
        layout = QHBoxLayout(summary_frame)
        
        # Summary statistics labels
        self.total_speed_label = QLabel("Total Speed: 0.0 MB/s")
        layout.addWidget(self.total_speed_label)
        
        self.average_speed_label = QLabel("Avg Speed: 0.0 MB/s")
        layout.addWidget(self.average_speed_label)
        
        layout.addStretch()
        
        self.utilization_label = QLabel("Thread Utilization: 0%")
        layout.addWidget(self.utilization_label)
        
        return summary_frame
        
    def _setup_update_timer(self):
        """Setup timer for periodic display updates."""
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self._refresh_display)
        self.update_timer.start(500)  # Update every 500ms
        
    def set_max_threads(self, max_threads: int):
        """
        Update the maximum thread count and adjust display accordingly.
        
        Args:
            max_threads: New maximum thread count
        """
        self.max_threads = max_threads
        self.thread_info_label.setText(f"Max Threads: {max_threads}")
        self._adjust_display_for_thread_count()
        
    def update_downloads(self, active_downloads: List[DownloadProgress]):
        """
        Update the display with current active downloads.
        
        Args:
            active_downloads: List of currently active download progress objects
        """
        # Store current downloads for reference
        self.active_downloads = {
            f"{download.mod_id}_{download.file_id}": download 
            for download in active_downloads
        }
        
        # Update active count display
        active_count = len(active_downloads)
        self.active_count_label.setText(f"Active: {active_count}")
        
        # Adjust table size for current content
        self._adjust_table_size(active_count)
        
        # Clear existing content and repopulate
        self.downloads_table.setRowCount(0)
        
        # Sort downloads by thread ID for organized display
        sorted_downloads = sorted(active_downloads, key=lambda x: x.thread_id or "")
        
        # Populate table rows
        for row_index, download in enumerate(sorted_downloads):
            self.downloads_table.insertRow(row_index)
            self._populate_table_row(row_index, download)
        
        # Apply auto-resize if enabled
        if self.auto_resize_checkbox.isChecked():
            self.downloads_table.resizeRowsToContents()
        
        # Update summary statistics
        self._update_summary_statistics(active_downloads)
        
    def _populate_table_row(self, row: int, download: DownloadProgress):
        """
        Populate a single table row with download information.
        
        Args:
            row: Table row index
            download: Download progress information
        """
        # File name with intelligent truncation
        filename = self._format_filename(download.filename)
        file_item = QTableWidgetItem(filename)
        file_item.setToolTip(f"Full name: {download.filename}")
        self.downloads_table.setItem(row, 0, file_item)
        
        # Progress bar with visual enhancements
        progress_bar = self._create_progress_bar(download)
        self.downloads_table.setCellWidget(row, 1, progress_bar)
        
        # Download speed with color coding
        speed_item = self._create_speed_item(download)
        self.downloads_table.setItem(row, 2, speed_item)
        
        # Estimated time to completion
        eta_item = self._create_eta_item(download)
        self.downloads_table.setItem(row, 3, eta_item)
        
        # Thread identifier with color coding
        thread_item = self._create_thread_item(download)
        self.downloads_table.setItem(row, 4, thread_item)
        
        # Status with appropriate coloring
        status_item = self._create_status_item(download)
        self.downloads_table.setItem(row, 5, status_item)
        
    def _format_filename(self, filename: str) -> str:
        """
        Format filename for display based on current display mode.
        
        Args:
            filename: Original filename
            
        Returns:
            Formatted filename string
        """
        max_length = 20 if self.display_mode == "compact" else 35
        
        if len(filename) <= max_length:
            return filename
        
        # Preserve file extension when truncating
        if '.' in filename:
            name, extension = filename.rsplit('.', 1)
            available_length = max_length - len(extension) - 4  # Account for "..." and "."
            if available_length > 8:
                return f"{name[:available_length]}...{extension}"
        
        return f"{filename[:max_length-3]}..."
        
    def _create_progress_bar(self, download: DownloadProgress) -> QProgressBar:
        """
        Create a progress bar widget with visual enhancements.
        
        Args:
            download: Download progress information
            
        Returns:
            Configured QProgressBar widget
        """
        progress_bar = QProgressBar()
        progress_bar.setMinimum(0)
        progress_bar.setMaximum(100)
        progress_bar.setValue(int(download.progress_percent))
        progress_bar.setMaximumHeight(20)
        
        # Format display text based on progress
        if download.progress_percent < 1:
            format_text = f"{download.progress_percent:.2f}%"
        elif download.progress_percent < 10:
            format_text = f"{download.progress_percent:.1f}%"
        else:
            format_text = f"{download.progress_percent:.0f}%"
            
        progress_bar.setFormat(format_text)
        
        # Apply color coding based on download speed
        self._apply_progress_bar_styling(progress_bar, download)
        
        return progress_bar
        
    def _apply_progress_bar_styling(self, progress_bar: QProgressBar, download: DownloadProgress):
        """
        Apply visual styling to progress bar based on download performance.
        
        Args:
            progress_bar: Progress bar widget to style
            download: Download progress information
        """
        if download.download_speed > 0:
            speed_mbps = download.download_speed / (1024 * 1024)
            if speed_mbps > 2.0:
                # High speed - green
                progress_bar.setStyleSheet("QProgressBar::chunk { background-color: #4CAF50; }")
            elif speed_mbps > 1.0:
                # Medium speed - yellow
                progress_bar.setStyleSheet("QProgressBar::chunk { background-color: #FFC107; }")
            else:
                # Low speed - orange
                progress_bar.setStyleSheet("QProgressBar::chunk { background-color: #FF9800; }")
                
    def _create_speed_item(self, download: DownloadProgress) -> QTableWidgetItem:
        """
        Create speed display item with color coding.
        
        Args:
            download: Download progress information
            
        Returns:
            Configured QTableWidgetItem for speed display
        """
        if download.download_speed > 0:
            speed_mbps = download.download_speed / (1024 * 1024)
            speed_text = f"{speed_mbps:.1f}"
        else:
            speed_text = "..."
        
        item = QTableWidgetItem(speed_text)
        
        # Apply color coding based on speed
        if download.download_speed > 0:
            speed_mbps = download.download_speed / (1024 * 1024)
            if speed_mbps > 2.0:
                item.setForeground(QColor(76, 175, 80))   # Green
            elif speed_mbps > 1.0:
                item.setForeground(QColor(255, 193, 7))   # Yellow
            else:
                item.setForeground(QColor(255, 152, 0))   # Orange
        
        return item
        
    def _create_eta_item(self, download: DownloadProgress) -> QTableWidgetItem:
        """
        Create ETA display item with intelligent formatting.
        
        Args:
            download: Download progress information
            
        Returns:
            Configured QTableWidgetItem for ETA display
        """
        if download.eta_seconds > 0:
            if download.eta_seconds < 60:
                eta_text = f"{int(download.eta_seconds)}s"
            elif download.eta_seconds < 3600:
                eta_text = f"{int(download.eta_seconds/60)}m"
            else:
                eta_text = str(timedelta(seconds=int(download.eta_seconds)))
        else:
            eta_text = "..."
        
        return QTableWidgetItem(eta_text)
        
    def _create_thread_item(self, download: DownloadProgress) -> QTableWidgetItem:
        """
        Create thread identifier item with color coding.
        
        Args:
            download: Download progress information
            
        Returns:
            Configured QTableWidgetItem for thread display
        """
        thread_text = download.thread_id or "Unknown"
        
        # Simplify thread display format
        if thread_text.startswith("Thread-"):
            thread_text = f"T-{thread_text.split('-')[1]}"
        
        item = QTableWidgetItem(thread_text)
        
        # Apply color coding for thread identification
        if download.thread_id:
            thread_hash = hash(download.thread_id) % 6
            thread_colors = [
                QColor(33, 150, 243),   # Blue
                QColor(156, 39, 176),   # Purple
                QColor(0, 150, 136),    # Teal
                QColor(255, 87, 34),    # Deep Orange
                QColor(121, 85, 72),    # Brown
                QColor(96, 125, 139)    # Blue Grey
            ]
            item.setForeground(thread_colors[thread_hash])
        
        return item
        
    def _create_status_item(self, download: DownloadProgress) -> QTableWidgetItem:
        """
        Create status display item with appropriate styling.
        
        Args:
            download: Download progress information
            
        Returns:
            Configured QTableWidgetItem for status display
        """
        status_text = download.status.title()
        item = QTableWidgetItem(status_text)
        
        # Apply status-based color coding
        status_colors = {
            "Downloading": QColor(76, 175, 80),     # Green
            "Waiting": QColor(255, 193, 7),        # Yellow
            "Completed": QColor(67, 160, 71),      # Dark Green
            "Failed": QColor(244, 67, 54),         # Red
            "Paused": QColor(158, 158, 158),       # Grey
        }
        
        if status_text in status_colors:
            item.setForeground(status_colors[status_text])
        
        return item
        
    def _adjust_table_size(self, active_count: int):
        """
        Adjust table size based on the number of active downloads.
        
        Args:
            active_count: Number of currently active downloads
        """
        # Calculate optimal height
        base_height = 60   # Minimum height for headers
        row_height = 25    # Height per row
        max_height = 350   # Maximum before scrolling
        
        optimal_height = min(
            base_height + (active_count * row_height),
            max_height
        )
        
        self.downloads_table.setMinimumHeight(optimal_height)
        
        # Enable scrolling for large numbers of downloads
        if active_count > (max_height - base_height) // row_height:
            self.downloads_table.setMaximumHeight(max_height)
        else:
            self.downloads_table.setMaximumHeight(optimal_height)
            
    def _adjust_display_for_thread_count(self):
        """Adjust display parameters based on configured thread count."""
        # Adapt table size expectations based on thread count
        expected_active = min(self.max_threads, len(self.active_downloads))
        self._adjust_table_size(expected_active)
        
    def _update_summary_statistics(self, active_downloads: List[DownloadProgress]):
        """
        Update summary statistics display.
        
        Args:
            active_downloads: List of currently active downloads
        """
        if not active_downloads:
            self.total_speed_label.setText("Total Speed: 0.0 MB/s")
            self.average_speed_label.setText("Avg Speed: 0.0 MB/s")
            self.utilization_label.setText("Thread Utilization: 0%")
            return
        
        # Calculate speed statistics
        total_speed = sum(d.download_speed for d in active_downloads if d.download_speed > 0)
        active_speed_count = len([d for d in active_downloads if d.download_speed > 0])
        
        total_speed_mbps = total_speed / (1024 * 1024)
        avg_speed_mbps = (total_speed_mbps / active_speed_count) if active_speed_count > 0 else 0
        
        self.total_speed_label.setText(f"Total Speed: {total_speed_mbps:.1f} MB/s")
        self.average_speed_label.setText(f"Avg Speed: {avg_speed_mbps:.1f} MB/s")
        
        # Calculate thread utilization
        utilization = (len(active_downloads) / self.max_threads) * 100 if self.max_threads > 0 else 0
        self.utilization_label.setText(f"Thread Utilization: {utilization:.0f}%")
        
    def _toggle_display_mode(self, compact_enabled: bool):
        """
        Toggle between standard and compact display modes.
        
        Args:
            compact_enabled: True to enable compact mode, False for standard
        """
        self.display_mode = "compact" if compact_enabled else "standard"
        
        # Apply new column widths
        self._apply_column_widths_for_mode(self.display_mode)
        
        # Refresh current display
        self._refresh_display()
        
    def _refresh_display(self):
        """Refresh the current display with existing data."""
        if hasattr(self, 'active_downloads') and self.active_downloads:
            current_downloads = list(self.active_downloads.values())
            self._update_summary_statistics(current_downloads)
            
    def get_optimal_height(self) -> int:
        """
        Calculate optimal widget height for current content.
        
        Returns:
            Optimal height in pixels
        """
        base_height = 120  # Header and summary sections
        active_count = len(self.active_downloads)
        
        if active_count == 0:
            return base_height + 80  # Minimum table height
        
        row_height = 22 if self.display_mode == "compact" else 28
        table_height = min(active_count * row_height + 60, 300)
        
        return base_height + table_height