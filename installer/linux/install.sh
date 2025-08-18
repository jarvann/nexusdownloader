#!/bin/bash
# NexusDownloader Linux Installer Script
# Supports Ubuntu, Debian, CentOS, RHEL, Fedora, openSUSE, Arch Linux

set -e

# Application details
APP_NAME="NexusDownloader"
APP_VERSION="2.0.0"
APP_DESCRIPTION="Advanced Nexus Mods collection downloader with GUI"
INSTALL_DIR="/opt/nexusdownloader"
DESKTOP_FILE="/usr/share/applications/nexusdownloader.desktop"
ICON_PATH="/usr/share/pixmaps/nexusdownloader.png"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Logging functions
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

# Check if running as root
check_root() {
    if [[ $EUID -eq 0 ]]; then
        log_error "This installer should not be run as root for security reasons."
        log_info "Please run as a regular user. The installer will prompt for sudo when needed."
        exit 1
    fi
}

# Detect Linux distribution
detect_distro() {
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        DISTRO=$ID
        VERSION=$VERSION_ID
    elif [[ -f /etc/redhat-release ]]; then
        DISTRO="rhel"
    elif [[ -f /etc/debian_version ]]; then
        DISTRO="debian"
    else
        DISTRO="unknown"
    fi
    
    log_info "Detected distribution: $DISTRO $VERSION"
}

# Install Python dependencies
install_python() {
    log_info "Checking Python installation..."
    
    # Check if Python 3.8+ is available
    if command -v python3 &> /dev/null; then
        PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
        log_info "Found Python $PYTHON_VERSION"
        
        # Check if version is 3.8+
        if python3 -c 'import sys; exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
            log_success "Python $PYTHON_VERSION is compatible"
        else
            log_error "Python 3.8+ is required. Found: $PYTHON_VERSION"
            install_python_package
        fi
    else
        log_warning "Python 3 not found. Installing..."
        install_python_package
    fi
    
    # Check for pip
    if ! command -v pip3 &> /dev/null; then
        log_warning "pip3 not found. Installing..."
        install_pip_package
    fi
}

# Install Python package based on distribution
install_python_package() {
    case $DISTRO in
        ubuntu|debian)
            log_info "Installing Python via apt..."
            sudo apt update
            sudo apt install -y python3 python3-pip python3-venv python3-dev
            ;;
        fedora)
            log_info "Installing Python via dnf..."
            sudo dnf install -y python3 python3-pip python3-devel
            ;;
        rhel|centos)
            log_info "Installing Python via yum..."
            sudo yum install -y python3 python3-pip python3-devel
            ;;
        opensuse*)
            log_info "Installing Python via zypper..."
            sudo zypper install -y python3 python3-pip python3-devel
            ;;
        arch)
            log_info "Installing Python via pacman..."
            sudo pacman -S --noconfirm python python-pip
            ;;
        *)
            log_error "Unsupported distribution: $DISTRO"
            log_info "Please install Python 3.8+ manually and re-run this installer."
            exit 1
            ;;
    esac
}

# Install pip package
install_pip_package() {
    case $DISTRO in
        ubuntu|debian)
            sudo apt install -y python3-pip
            ;;
        fedora)
            sudo dnf install -y python3-pip
            ;;
        rhel|centos)
            sudo yum install -y python3-pip
            ;;
        opensuse*)
            sudo zypper install -y python3-pip
            ;;
        arch)
            sudo pacman -S --noconfirm python-pip
            ;;
    esac
}

# Install system dependencies for GUI
install_gui_dependencies() {
    log_info "Installing GUI dependencies..."
    
    case $DISTRO in
        ubuntu|debian)
            sudo apt install -y \
                libxcb-xinerama0 \
                libxcb-cursor0 \
                libgl1-mesa-glx \
                libglib2.0-0 \
                libxkbcommon-x11-0 \
                libxcb-icccm4 \
                libxcb-image0 \
                libxcb-keysyms1 \
                libxcb-randr0 \
                libxcb-render-util0 \
                libxcb-shape0 \
                libxcb-sync1 \
                libxcb-xfixes0
            ;;
        fedora)
            sudo dnf install -y \
                xcb-util-cursor \
                mesa-libGL \
                glib2 \
                libxkbcommon-x11
            ;;
        rhel|centos)
            sudo yum install -y \
                xcb-util-cursor \
                mesa-libGL \
                glib2 \
                libxkbcommon-x11
            ;;
        opensuse*)
            sudo zypper install -y \
                libxcb-cursor0 \
                Mesa-libGL1 \
                glib2 \
                libxkbcommon-x11-0
            ;;
        arch)
            sudo pacman -S --noconfirm \
                xcb-util-cursor \
                mesa \
                glib2 \
                libxkbcommon-x11
            ;;
    esac
}

# Create installation directory and copy files
install_application() {
    log_info "Installing $APP_NAME to $INSTALL_DIR..."
    
    # Create installation directory
    sudo mkdir -p "$INSTALL_DIR"
    
    # Copy application files
    if [[ -d "dist/NexusDownloader" ]]; then
        sudo cp -r dist/NexusDownloader/* "$INSTALL_DIR/"
        sudo chmod +x "$INSTALL_DIR/NexusDownloader"
    else
        log_error "Application files not found. Please ensure you've built the application first."
        exit 1
    fi
    
    # Set proper permissions
    sudo chown -R root:root "$INSTALL_DIR"
    sudo chmod -R 755 "$INSTALL_DIR"
    
    # Create symlink for command-line access
    sudo ln -sf "$INSTALL_DIR/NexusDownloader" "/usr/local/bin/nexusdownloader"
    
    log_success "Application files installed successfully"
}

# Create desktop entry
create_desktop_entry() {
    log_info "Creating desktop entry..."
    
    # Copy icon if available
    if [[ -f "assets/icon.png" ]]; then
        sudo cp "assets/icon.png" "$ICON_PATH"
    fi
    
    # Create desktop file
    sudo tee "$DESKTOP_FILE" > /dev/null << EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=$APP_NAME
Comment=$APP_DESCRIPTION
Exec=$INSTALL_DIR/NexusDownloader
Icon=nexusdownloader
Terminal=false
Categories=Game;Utility;
Keywords=nexus;mods;download;vortex;
StartupNotify=true
MimeType=application/json;
EOF
    
    # Update desktop database
    if command -v update-desktop-database &> /dev/null; then
        sudo update-desktop-database
    fi
    
    log_success "Desktop entry created"
}

# Install Python dependencies
install_python_deps() {
    log_info "Installing Python dependencies..."
    
    # Create virtual environment in installation directory
    sudo python3 -m venv "$INSTALL_DIR/venv"
    
    # Install dependencies
    if [[ -f "requirements.txt" ]]; then
        sudo "$INSTALL_DIR/venv/bin/pip" install -r requirements.txt
    else
        log_warning "requirements.txt not found. Installing minimal dependencies..."
        sudo "$INSTALL_DIR/venv/bin/pip" install PySide6 requests urllib3 cryptography keyring colorama
    fi
    
    log_success "Python dependencies installed"
}

# Create uninstaller
create_uninstaller() {
    log_info "Creating uninstaller..."
    
    sudo tee "$INSTALL_DIR/uninstall.sh" > /dev/null << 'EOF'
#!/bin/bash
# NexusDownloader Uninstaller

echo "Uninstalling NexusDownloader..."

# Remove installation directory
sudo rm -rf /opt/nexusdownloader

# Remove desktop entry
sudo rm -f /usr/share/applications/nexusdownloader.desktop

# Remove icon
sudo rm -f /usr/share/pixmaps/nexusdownloader.png

# Remove symlink
sudo rm -f /usr/local/bin/nexusdownloader

# Update desktop database
if command -v update-desktop-database &> /dev/null; then
    sudo update-desktop-database
fi

echo "NexusDownloader has been uninstalled successfully."
EOF
    
    sudo chmod +x "$INSTALL_DIR/uninstall.sh"
    log_success "Uninstaller created at $INSTALL_DIR/uninstall.sh"
}

# Main installation process
main() {
    echo "=================================="
    echo "  $APP_NAME v$APP_VERSION Installer"
    echo "=================================="
    echo
    
    check_root
    detect_distro
    
    log_info "Starting installation process..."
    
    # Install dependencies
    install_python
    install_gui_dependencies
    
    # Install application
    install_application
    install_python_deps
    
    # Create desktop integration
    create_desktop_entry
    create_uninstaller
    
    echo
    log_success "Installation completed successfully!"
    echo
    echo "You can now:"
    echo "  • Launch from applications menu"
    echo "  • Run 'nexusdownloader' from command line"
    echo "  • Uninstall using: sudo $INSTALL_DIR/uninstall.sh"
    echo
}

# Handle command line arguments
case "${1:-}" in
    --help|-h)
        echo "NexusDownloader Linux Installer"
        echo
        echo "Usage: $0 [OPTIONS]"
        echo
        echo "Options:"
        echo "  --help, -h    Show this help message"
        echo "  --uninstall   Uninstall NexusDownloader"
        echo
        exit 0
        ;;
    --uninstall)
        if [[ -f "$INSTALL_DIR/uninstall.sh" ]]; then
            bash "$INSTALL_DIR/uninstall.sh"
        else
            log_error "Uninstaller not found. NexusDownloader may not be installed."
        fi
        exit 0
        ;;
    *)
        main
        ;;
esac