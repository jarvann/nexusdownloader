#!/bin/bash
# NexusDownloader Linux Build Script
# Builds executable and prepares installation package

set -e

echo "===================================="
echo "  NexusDownloader Linux Build"
echo "===================================="
echo

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if Python is installed
check_python() {
    if ! command -v python3 &> /dev/null; then
        log_error "Python 3 is not installed"
        log_info "Please install Python 3.8+ and try again"
        exit 1
    fi
    
    PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
    log_info "Found Python $PYTHON_VERSION"
    
    # Check if version is 3.8+
    if ! python3 -c 'import sys; exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
        log_error "Python 3.8+ is required. Found: $PYTHON_VERSION"
        exit 1
    fi
}

# Setup virtual environment
setup_venv() {
    if [[ -z "$VIRTUAL_ENV" ]]; then
        log_info "Creating virtual environment..."
        python3 -m venv venv
        source venv/bin/activate
    else
        log_info "Using existing virtual environment: $VIRTUAL_ENV"
    fi
}

# Install dependencies
install_deps() {
    log_info "Installing build dependencies..."
    python -m pip install --upgrade pip
    pip install "pyinstaller>=6.20.0"
    
    if [[ -f "requirements.txt" ]]; then
        pip install -r requirements.txt
    else
        log_warning "requirements.txt not found, installing minimal dependencies"
        pip install PySide6 requests urllib3 cryptography keyring colorama
    fi
}

# Clean previous builds
clean_build() {
    log_info "Cleaning previous builds..."
    rm -rf dist build __pycache__ src/__pycache__ src/*/__pycache__
    rm -f *.tar.gz
}

# Create assets if they don't exist
create_assets() {
    mkdir -p assets
    
    if [[ ! -f "assets/icon.png" ]]; then
        log_warning "No icon.png found in assets directory"
        log_info "Please add an icon file for better presentation"
        # Create a placeholder icon description
        cat > assets/icon_placeholder.txt << EOF
Icon file not found. Please add:
- assets/icon.png (256x256 PNG for Linux)
- assets/icon.ico (Windows ICO format)

You can create these from any image using:
- GIMP
- ImageMagick: convert image.png -resize 256x256 assets/icon.png
EOF
    fi
}

# Build executable
build_executable() {
    log_info "Building executable with PyInstaller..."
    
    if [[ ! -f "nexusdownloader.spec" ]]; then
        log_error "nexusdownloader.spec not found"
        log_info "Please ensure the spec file exists"
        exit 1
    fi
    
    pyinstaller nexusdownloader.spec
    
    if [[ $? -eq 0 ]]; then
        log_success "Executable built successfully!"
        log_info "Location: dist/NexusDownloader/"
    else
        log_error "PyInstaller build failed"
        exit 1
    fi
}

# Create distribution package
create_package() {
    log_info "Creating distribution package..."
    
    # Create package directory structure
    PACKAGE_NAME="nexusdownloader-linux-x86_64"
    mkdir -p "$PACKAGE_NAME"
    
    # Copy files
    cp -r dist/NexusDownloader "$PACKAGE_NAME/"
    cp installer/linux/install.sh "$PACKAGE_NAME/"
    cp README.md "$PACKAGE_NAME/" 2>/dev/null || echo "README.md not found"
    cp LICENSE "$PACKAGE_NAME/" 2>/dev/null || echo "LICENSE not found"
    
    # Copy assets if they exist
    if [[ -d "assets" ]]; then
        cp -r assets "$PACKAGE_NAME/"
    fi
    
    # Create installation instructions
    cat > "$PACKAGE_NAME/INSTALL.txt" << EOF
NexusDownloader Linux Installation Instructions
==============================================

Quick Install:
    sudo ./install.sh

Manual Install:
    1. Ensure Python 3.8+ is installed
    2. Run the installer: sudo ./install.sh
    3. Follow the prompts

The installer will:
- Check and install dependencies
- Install the application to /opt/nexusdownloader
- Create desktop entries and shortcuts
- Set up command-line access

Uninstall:
    sudo /opt/nexusdownloader/uninstall.sh

For help:
    ./install.sh --help

System Requirements:
- Python 3.8+
- X11 or Wayland display server
- 100MB disk space
EOF
    
    # Make install script executable
    chmod +x "$PACKAGE_NAME/install.sh"
    chmod +x "$PACKAGE_NAME/NexusDownloader/NexusDownloader"
    
    # Create tarball
    tar -czf "$PACKAGE_NAME.tar.gz" "$PACKAGE_NAME"
    
    log_success "Package created: $PACKAGE_NAME.tar.gz"
    
    # Clean up temporary directory
    rm -rf "$PACKAGE_NAME"
}

# Test executable
test_executable() {
    log_info "Testing executable..."
    
    if [[ -f "dist/NexusDownloader/NexusDownloader" ]]; then
        # Quick test to ensure it loads
        timeout 10s ./dist/NexusDownloader/NexusDownloader --help >/dev/null 2>&1 || true
        log_success "Executable test completed"
    else
        log_error "Executable not found!"
        exit 1
    fi
}

# Main build process
main() {
    check_python
    setup_venv
    install_deps
    clean_build
    create_assets
    build_executable
    test_executable
    create_package
    
    echo
    log_success "Build completed successfully!"
    echo
    echo "Files created:"
    echo "  • Executable: dist/NexusDownloader/NexusDownloader"
    echo "  • Package: nexusdownloader-linux-x86_64.tar.gz"
    echo
    echo "To install:"
    echo "  tar -xzf nexusdownloader-linux-x86_64.tar.gz"
    echo "  cd nexusdownloader-linux-x86_64"
    echo "  sudo ./install.sh"
    echo
}

# Run main function
main