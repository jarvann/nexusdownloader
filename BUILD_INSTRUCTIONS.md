# NexusDownloader Build Instructions

This document provides comprehensive instructions for building NexusDownloader executables and installers for Windows and Linux.

## Prerequisites

### Common Requirements
- Python 3.8 or later
- Git (for cloning/development)
- 2GB free disk space (for build artifacts)

### Windows-Specific
- **NSIS** (Nullsoft Scriptable Install System) for creating installers
  - Download: https://nsis.sourceforge.io/
  - Add to PATH after installation
- **Visual Studio Build Tools** (may be required for some dependencies)

### Linux-Specific
- Build essentials: `sudo apt install build-essential` (Ubuntu/Debian)
- GUI libraries (handled automatically by installer)
- Python development headers: `sudo apt install python3-dev`

## Quick Build

### Cross-Platform Build Script (Recommended)
```bash
# Build executable and installer/package
python build.py

# Build executable only (skip installer)
python build.py --skip-installer

# Clean build artifacts only
python build.py --clean-only
```

### Platform-Specific Scripts

#### Windows
```cmd
# Build executable and installer
build_windows.bat

# OR manually:
python -m venv venv
venv\Scripts\activate
pip install -r requirements-build.txt
pip install -r requirements.txt
pyinstaller nexusdownloader.spec
makensis installer\windows\nexusdownloader_installer.nsi
```

#### Linux
```bash
# Build executable and package
./build_linux.sh

# OR manually:
python3 -m venv venv
source venv/bin/activate
pip install -r requirements-build.txt
pip install -r requirements.txt
pyinstaller nexusdownloader.spec
# Package creation is handled by build script
```

## Build Process Details

### 1. Environment Setup
- Creates virtual environment if none exists
- Installs build dependencies from `requirements-build.txt`
- Installs runtime dependencies from `requirements.txt`

### 2. Executable Creation
- Uses PyInstaller with `nexusdownloader.spec`
- Bundles all dependencies into standalone executable
- Creates `dist/NexusDownloader/` directory with executable and supporting files

### 3. Installer/Package Creation

#### Windows (NSIS)
- Creates `NexusDownloader_Setup_v2.0.0.exe`
- Includes Python dependency checking and auto-installation
- Adds uninstaller and registry entries
- Creates desktop and start menu shortcuts

#### Linux (Shell Script + Tarball)
- Creates `nexusdownloader-linux-x86_64.tar.gz`
- Includes dependency checking for multiple distributions
- Auto-detects package manager (apt, yum, dnf, zypper, pacman)
- Creates desktop entries and system integration

## Build Artifacts

After successful build, you'll find:

### Windows
```
dist/NexusDownloader/                    # Portable executable
├── NexusDownloader.exe                  # Main executable
├── _internal/                           # Bundled dependencies
└── ...

installer/windows/
└── NexusDownloader_Setup_v2.0.0.exe    # Windows installer
```

### Linux
```
dist/NexusDownloader/                    # Portable executable
├── NexusDownloader                      # Main executable
├── _internal/                           # Bundled dependencies
└── ...

nexusdownloader-linux-x86_64.tar.gz     # Distribution package
```

## Customization

### Icons and Branding
Place your custom icons in the `assets/` directory:
- `assets/icon.ico` - Windows icon (256x256, ICO format)
- `assets/icon.png` - Linux icon (256x256, PNG format)
- `assets/installer_banner.bmp` - Windows installer banner (optional)

### Version Information
Update version in these files:
- `build.py` - Main version constant
- `installer/windows/nexusdownloader_installer.nsi` - Windows installer version
- `installer/linux/install.sh` - Linux installer version

### Dependencies
- `requirements.txt` - Runtime dependencies
- `requirements-build.txt` - Build-time dependencies
- `nexusdownloader.spec` - PyInstaller configuration

## Troubleshooting

### Common Issues

#### "Python not found" Error
- Ensure Python 3.8+ is installed and in PATH
- On Windows: Check "Add Python to PATH" during installation
- On Linux: Install with package manager

#### PyInstaller Import Errors
- Add missing modules to `hiddenimports` in `nexusdownloader.spec`
- Check for Qt plugins: may need `--add-data` for Qt resources

#### NSIS Installer Errors (Windows)
- Install NSIS from official website
- Add NSIS installation directory to system PATH
- Check installer script syntax with NSIS

#### Linux Package Dependencies
- Install build essentials: `sudo apt install build-essential python3-dev`
- For GUI support: install Qt libraries (handled by installer)

### Build Optimization

#### Reduce Executable Size
```python
# In nexusdownloader.spec, add to excludes:
excludes=[
    'matplotlib',
    'numpy', 
    'pandas',
    'scipy',
    'tkinter',
    # Add other unused modules
]
```

#### Enable UPX Compression
```python
# In nexusdownloader.spec:
exe = EXE(
    # ... other parameters
    upx=True,  # Enable UPX compression
)
```

## Testing

### Executable Testing
```bash
# Test basic functionality
./dist/NexusDownloader/NexusDownloader --help

# Test GUI (should open without errors)
./dist/NexusDownloader/NexusDownloader
```

### Installer Testing

#### Windows
1. Uninstall any existing version
2. Run installer as administrator
3. Test all shortcuts and menu entries
4. Verify uninstaller works correctly

#### Linux  
1. Extract package: `tar -xzf nexusdownloader-linux-x86_64.tar.gz`
2. Run installer: `sudo ./install.sh`
3. Test command-line: `nexusdownloader --help`
4. Test desktop entry
5. Uninstall: `sudo /opt/nexusdownloader/uninstall.sh`

## Distribution

### Windows
- Upload `NexusDownloader_Setup_v2.0.0.exe` to distribution platform
- Provide SHA256 checksum for verification

### Linux
- Upload `nexusdownloader-linux-x86_64.tar.gz` to distribution platform
- Consider creating separate packages for different architectures

### Code Signing (Optional)
For production releases:
- Windows: Use `signtool.exe` with certificate
- Linux: Use GPG signing for package integrity

## Automated Building

### CI/CD Integration
Example GitHub Actions workflow:

```yaml
name: Build NexusDownloader
on: [push, release]
jobs:
  build-windows:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - run: python build.py
      
  build-linux:
    runs-on: ubuntu-latest  
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - run: python build.py
```

## Support

For build issues:
1. Check this documentation
2. Verify all prerequisites are installed
3. Check build logs for specific error messages
4. Test with clean virtual environment

## Build Environment Variables

Optional environment variables for customization:
- `NEXUS_BUILD_VERSION` - Override version number
- `NEXUS_BUILD_DEBUG` - Enable debug build options
- `NEXUS_BUILD_SKIP_TESTS` - Skip executable testing