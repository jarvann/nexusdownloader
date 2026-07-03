# NexusDownloader v2.0 Release Notes

## 🚀 Major Release: Complete GUI Overhaul & Enhanced Features

This major release brings a completely redesigned user experience with a modern PySide6 GUI, advanced progress tracking, and comprehensive configuration management. NexusDownloader v2.0 is faster, more reliable, and easier to use than ever before.

---

## 🎯 What's New

### 🖥️ Modern GUI Interface
- **Complete GUI redesign** built with PySide6 for native performance
- **Real-time progress tracking** with individual file progress bars
- **Live download statistics** showing speeds, ETA, and completion status  
- **Comprehensive settings dialog** with tabbed configuration sections
- **Application icon integration** for professional appearance
- **Intuitive file browser** for collection selection
- **Status bar updates** with real-time operation feedback

### ⚡ Enhanced Performance
- **Up to 35 concurrent downloads** (increased from previous limits)
- **Optimized thread management** with better resource utilization
- **Improved API rate limiting** (120 requests/minute, 10,000/hour)
- **Enhanced retry logic** with configurable delays and attempts
- **Faster endorsement processing** with multi-threaded operations

### 🔧 Advanced Configuration System
- **New JSON configuration format** with organized sections:
  - **API & Authentication**: Rate limits, timeouts, user agent
  - **Downloads**: Concurrent connections, retry behavior, chunk size
  - **Vortex Integration**: Auto-detection, executable paths, folder management
  - **Logging**: Comprehensive logging with performance metrics
  - **Security**: SSL validation, secure storage, input sanitization
- **Settings persistence** with automatic configuration saving
- **Configuration validation** with error checking and defaults

### 📊 Progress Tracking & Resume
- **Advanced progress monitoring** with per-file tracking
- **Resume capability** for interrupted downloads
- **State persistence** across application restarts
- **Download history tracking** with completion timestamps
- **Error reporting** with detailed failure analysis
- **Performance metrics** collection and display

### 🏗️ Build & Distribution System
- **Cross-platform build scripts** for Windows and Linux
- **Automated executable creation** with PyInstaller
- **Windows installer** creation with NSIS
- **Linux packaging** support (AppImage, .deb)
- **Universal build system** with `build.py` automation

### 🔒 Security & Reliability
- **Enhanced error handling** with graceful recovery
- **Secure API key storage** with encryption support
- **SSL certificate validation** for secure connections
- **Input sanitization** to prevent security issues
- **Comprehensive logging** with security event tracking
- **Automatic log cleanup** with configurable retention

---

## 🆕 New Features

### GUI Enhancements
- **Multi-threaded download visualization** showing all active downloads
- **Progress bars with percentage display** for each file
- **Download speed indicators** with real-time updates
- **Error count tracking** with detailed error messages
- **Pause/Resume functionality** for download management
- **Settings validation** with immediate feedback

### Command Line Improvements
- **Enhanced argument parsing** with better error messages  
- **Improved logging output** with colored console display
- **Better error reporting** for troubleshooting
- **Resume support** from command line interface

### Configuration Management
- **Automatic configuration migration** from older formats
- **Default configuration generation** for new installations
- **Configuration validation** with helpful error messages
- **Settings backup and restore** capabilities

---

## 🔧 Improvements & Bug Fixes

### Performance Optimizations
- **Reduced memory usage** through better thread management
- **Faster startup times** with optimized initialization
- **Improved download speeds** with better chunk handling
- **Enhanced error recovery** reducing failed downloads

### User Experience
- **Better first-run experience** with automatic setup prompts
- **Improved error messages** with actionable suggestions
- **Enhanced progress feedback** throughout operations
- **Cleaner interface design** with better organization

### Stability & Reliability  
- **Fixed threading issues** that could cause hangs
- **Improved exception handling** preventing crashes
- **Better resource cleanup** preventing memory leaks
- **Enhanced API error handling** for network issues

---

## 📋 Technical Changes

### Architecture Updates
- **Modular code organization** with separate utility modules
- **Enhanced logging system** with async handlers
- **Improved configuration management** with type validation
- **Better error handling hierarchy** with specific error types

### Dependencies
- **Updated to Python 3.12+** for latest features and performance
- **PySide6 6.4.0+** for modern GUI framework
- **Enhanced security libraries** (cryptography, keyring)
- **Development tools** (pytest, black, flake8) for code quality

### API Improvements
- **Updated User-Agent** to "NexusDownloader/2.0"
- **Enhanced rate limiting** compliance with Nexus requirements
- **Better API error handling** with specific response codes
- **Improved timeout management** for reliability

---

## 🚀 Getting Started

### Quick Start
1. **Download the release** and extract to your preferred location
2. **Run `run.bat`** (Windows) or execute the appropriate launcher
3. **Configure your API key** through File > Settings
4. **Select your collection JSON** file using the browser
5. **Choose download threads** and click "Start Download"

### System Requirements
- **Python 3.12+** (automatically managed by launcher)
- **Windows 10+** or **Linux** (Ubuntu 20.04+, similar distributions)
- **2GB RAM minimum** (4GB recommended for large collections)
- **Stable internet connection** for optimal performance

---

## 🔄 Migration from v1.x

### Automatic Configuration Migration
- **Configuration files** are automatically upgraded to the new format
- **Download history** is preserved and migrated
- **Settings preferences** are retained where applicable

### Manual Steps
1. **Update your shortcuts** to use the new GUI launcher
2. **Review settings** in the new Settings dialog
3. **Update any automation scripts** to use new command line arguments

---

## 🐛 Known Issues

- **First-time setup** may require manual configuration of download paths
- **Very large collections** (1000+ mods) may take longer to initialize
- **Some antivirus software** may flag the executable (false positive)

---

## 🙏 Acknowledgments

Thank you to all users who provided feedback and reported issues during development. Special thanks to the mod authors whose work makes this tool valuable to the community.

---

## 📞 Support & Feedback

- **Issues & Bug Reports**: [GitHub Issues](https://github.com/jarvann/nexusdownloader/issues)
- **Feature Requests**: [GitHub Discussions](https://github.com/jarvann/nexusdownloader/discussions)
- **Documentation**: See the updated README.md for comprehensive usage instructions

---

**Full Changelog**: [View all changes since v1.x](https://github.com/jarvann/nexusdownloader/compare/v1.0...v2.0)