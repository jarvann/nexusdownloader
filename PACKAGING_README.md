# NexusDownloader Packaging System

This directory contains all the necessary files to build NexusDownloader as standalone executables with installers for Windows and Linux.

## 🚀 Quick Start

### Option 1: Cross-Platform Build Script (Recommended)
```bash
python build.py
```

### Option 2: Platform-Specific Scripts
```bash
# Windows
build_windows.bat

# Linux  
./build_linux.sh
```

### Option 3: Using Makefile
```bash
make build
```

## 📦 What Gets Built

### Windows
- **Standalone Executable**: `dist/NexusDownloader/NexusDownloader.exe`
- **Windows Installer**: `NexusDownloader_Setup_v2.0.0.exe`
  - Automatic Python dependency installation
  - Desktop and Start Menu shortcuts
  - Proper uninstaller with registry cleanup
  - Add to system PATH option

### Linux
- **Standalone Executable**: `dist/NexusDownloader/NexusDownloader`  
- **Distribution Package**: `nexusdownloader-linux-x86_64.tar.gz`
  - Smart dependency detection for all major Linux distributions
  - Automatic GUI library installation
  - Desktop entry creation
  - Command-line integration (`nexusdownloader` command)
  - Clean uninstaller

## 🔧 Build Features

### Automatic Dependency Management
- **Python Version Detection**: Ensures Python 3.8+ compatibility
- **Virtual Environment**: Creates isolated build environment
- **Dependency Installation**: Automatically installs all required packages
- **Platform Detection**: Adapts build process to Windows/Linux

### Smart Installer Features
- **Windows**: Detects and installs Python if missing
- **Linux**: Auto-detects distribution (Ubuntu, CentOS, Arch, etc.) and uses appropriate package manager
- **Both**: Creates proper shortcuts, file associations, and system integration

### Cross-Platform Support
- **Single Command**: `python build.py` works on both platforms
- **Platform-Specific Optimizations**: Each installer uses native installation methods
- **Consistent Experience**: Same application functionality across platforms

## 📋 Requirements

### Development Machine
```bash
pip install -r requirements-build.txt
pip install -r requirements.txt
```

### Windows Additional Requirements
- **NSIS** (for installer creation): https://nsis.sourceforge.io/
- **Visual Studio Build Tools** (if needed for dependencies)

### Linux Additional Requirements  
- Build essentials (`sudo apt install build-essential python3-dev`)
- Typically handled automatically by the build script

## 🎯 Build Process

1. **Environment Setup**
   - Creates virtual environment
   - Installs build dependencies
   - Verifies Python version

2. **Executable Creation**
   - Uses PyInstaller with optimized configuration
   - Bundles all dependencies (no external Python required)
   - Creates platform-appropriate executable

3. **Installer Generation**
   - **Windows**: NSIS-based installer with dependency management
   - **Linux**: Shell script installer with multi-distro support

4. **Testing & Validation**
   - Verifies executable functionality
   - Checks installer integrity

## 🛠️ Customization

### Icons and Branding
```
assets/
├── icon.ico          # Windows icon (256x256)
├── icon.png          # Linux icon (256x256)  
└── installer_banner.bmp  # Windows installer banner (optional)
```

### Configuration Files
- `nexusdownloader.spec` - PyInstaller configuration
- `installer/windows/nexusdownloader_installer.nsi` - Windows installer script
- `installer/linux/install.sh` - Linux installer script

## 🧪 Testing

### Test Built Executable
```bash
# Quick test
make test

# Manual test
./dist/NexusDownloader/NexusDownloader --help
```

### Test Installer
```bash
# Windows: Run the .exe installer
# Linux: 
tar -xzf nexusdownloader-linux-x86_64.tar.gz
cd nexusdownloader-linux-x86_64
sudo ./install.sh
```

## 📁 File Structure

```
├── build.py                    # Cross-platform build script
├── build_windows.bat          # Windows build script  
├── build_linux.sh            # Linux build script
├── nexusdownloader.spec       # PyInstaller specification
├── requirements-build.txt     # Build dependencies
├── Makefile                  # Make-based automation
├── BUILD_INSTRUCTIONS.md     # Detailed build documentation
├── installer/
│   ├── windows/
│   │   └── nexusdownloader_installer.nsi  # Windows installer script
│   └── linux/
│       └── install.sh         # Linux installer script
└── assets/                   # Icons and branding
    ├── icon.ico             # Windows icon
    └── icon.png             # Linux icon
```

## 🆘 Troubleshooting

### Common Issues
- **"Python not found"**: Ensure Python 3.8+ is installed and in PATH
- **PyInstaller errors**: Check `requirements.txt` and ensure all dependencies are compatible
- **NSIS not found**: Install NSIS and add to PATH for Windows installer creation
- **Permission denied**: Use `chmod +x` for shell scripts on Linux

### Debug Build
```bash
python build.py --skip-installer  # Build without installer for faster testing
```

### Clean Build
```bash
python build.py --clean-only     # Clean all build artifacts
make clean                       # Alternative using Makefile
```

## 🚀 Distribution

Once built, distribute:
- **Windows**: `NexusDownloader_Setup_v2.0.0.exe` (full installer)
- **Linux**: `nexusdownloader-linux-x86_64.tar.gz` (distribution package)

Both include automatic dependency management and proper system integration.

## 📞 Support

For packaging issues:
1. Check `BUILD_INSTRUCTIONS.md` for detailed information
2. Verify all prerequisites are installed
3. Test with a clean virtual environment
4. Check build logs for specific error messages