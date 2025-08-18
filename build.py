#!/usr/bin/env python3
"""
Cross-platform build script for NexusDownloader
Handles building executables and installers for Windows and Linux
"""

import os
import sys
import subprocess
import platform
import shutil
from pathlib import Path
import argparse

class Builder:
    def __init__(self):
        self.root_dir = Path(__file__).parent
        self.dist_dir = self.root_dir / "dist"
        self.build_dir = self.root_dir / "build"
        self.system = platform.system().lower()
        
    def log(self, message, level="INFO"):
        """Log a message with color coding"""
        colors = {
            "INFO": "\033[0;34m",
            "SUCCESS": "\033[0;32m", 
            "WARNING": "\033[1;33m",
            "ERROR": "\033[0;31m"
        }
        reset = "\033[0m"
        color = colors.get(level, colors["INFO"])
        print(f"{color}[{level}]{reset} {message}")
    
    def run_command(self, command, cwd=None, shell=False):
        """Run a command and return success status"""
        try:
            if isinstance(command, str) and not shell:
                command = command.split()
            
            result = subprocess.run(
                command, 
                cwd=cwd or self.root_dir,
                shell=shell,
                capture_output=True,
                text=True
            )
            
            if result.returncode != 0:
                self.log(f"Command failed: {' '.join(command) if isinstance(command, list) else command}", "ERROR")
                self.log(f"Error output: {result.stderr}", "ERROR")
                return False
            
            return True
        except Exception as e:
            self.log(f"Exception running command: {e}", "ERROR")
            return False
    
    def check_python(self):
        """Check Python version and availability"""
        self.log("Checking Python installation...")
        
        try:
            version = sys.version_info
            if version.major < 3 or (version.major == 3 and version.minor < 8):
                self.log(f"Python 3.8+ required. Found: {version.major}.{version.minor}", "ERROR")
                return False
            
            self.log(f"Python {version.major}.{version.minor}.{version.micro} OK", "SUCCESS")
            return True
        except Exception as e:
            self.log(f"Error checking Python: {e}", "ERROR")
            return False
    
    def setup_environment(self):
        """Setup build environment"""
        self.log("Setting up build environment...")
        
        # Check for virtual environment
        if not os.environ.get('VIRTUAL_ENV'):
            self.log("Creating virtual environment...", "WARNING")
            if not self.run_command([sys.executable, "-m", "venv", "venv"]):
                return False
            
            # Activate virtual environment
            if self.system == "windows":
                venv_python = self.root_dir / "venv" / "Scripts" / "python.exe"
                venv_pip = self.root_dir / "venv" / "Scripts" / "pip.exe"
            else:
                venv_python = self.root_dir / "venv" / "bin" / "python"
                venv_pip = self.root_dir / "venv" / "bin" / "pip"
            
            self.python_cmd = str(venv_python)
            self.pip_cmd = str(venv_pip)
        else:
            self.log("Using existing virtual environment", "INFO")
            self.python_cmd = sys.executable
            self.pip_cmd = "pip"
        
        # Upgrade pip
        self.log("Upgrading pip...")
        if not self.run_command([self.python_cmd, "-m", "pip", "install", "--upgrade", "pip"]):
            return False
        
        # Install build dependencies
        self.log("Installing build dependencies...")
        build_deps = ["pyinstaller[encryption]"]
        
        if not self.run_command([self.pip_cmd, "install"] + build_deps):
            return False
        
        # Install project dependencies
        requirements_file = self.root_dir / "requirements.txt"
        if requirements_file.exists():
            self.log("Installing project dependencies...")
            if not self.run_command([self.pip_cmd, "install", "-r", "requirements.txt"]):
                return False
        else:
            self.log("Installing minimal dependencies...", "WARNING")
            min_deps = ["PySide6>=6.4.0", "requests>=2.25.0", "urllib3>=1.26.0", 
                       "cryptography>=3.4.8", "keyring>=23.0.0", "colorama>=0.4.4"]
            if not self.run_command([self.pip_cmd, "install"] + min_deps):
                return False
        
        return True
    
    def clean_build(self):
        """Clean previous build artifacts"""
        self.log("Cleaning previous builds...")
        
        dirs_to_clean = [self.dist_dir, self.build_dir]
        for dir_path in dirs_to_clean:
            if dir_path.exists():
                shutil.rmtree(dir_path)
        
        # Clean Python cache
        for cache_dir in self.root_dir.rglob("__pycache__"):
            shutil.rmtree(cache_dir, ignore_errors=True)
        
        for pyc_file in self.root_dir.rglob("*.pyc"):
            pyc_file.unlink(ignore_errors=True)
    
    def create_assets(self):
        """Create or verify assets directory"""
        assets_dir = self.root_dir / "assets"
        assets_dir.mkdir(exist_ok=True)
        
        # Check for icons
        icon_files = {
            "windows": assets_dir / "icon.ico",
            "linux": assets_dir / "icon.png"
        }
        
        missing_icons = []
        for platform, icon_path in icon_files.items():
            if not icon_path.exists():
                missing_icons.append(f"{icon_path} ({platform})")
        
        if missing_icons:
            self.log(f"Missing icon files: {', '.join(missing_icons)}", "WARNING")
            self.log("Application will use default system icons", "WARNING")
    
    def build_executable(self):
        """Build executable using PyInstaller"""
        self.log("Building executable...")
        
        spec_file = self.root_dir / "nexusdownloader.spec"
        if not spec_file.exists():
            self.log("nexusdownloader.spec not found!", "ERROR")
            return False
        
        if not self.run_command([self.python_cmd, "-m", "PyInstaller", "nexusdownloader.spec"]):
            return False
        
        exe_path = self.dist_dir / "NexusDownloader"
        if exe_path.exists():
            self.log("Executable built successfully!", "SUCCESS")
            return True
        else:
            self.log("Executable not found after build", "ERROR")
            return False
    
    def build_windows_installer(self):
        """Build Windows installer using NSIS"""
        if self.system != "windows":
            self.log("Skipping Windows installer (not on Windows)", "WARNING")
            return True
        
        # Check for NSIS
        if not shutil.which("makensis"):
            self.log("NSIS not found in PATH", "WARNING")
            self.log("Install NSIS from https://nsis.sourceforge.io/ to create installer", "INFO")
            return True
        
        self.log("Creating Windows installer...")
        installer_script = self.root_dir / "installer" / "windows" / "nexusdownloader_installer.nsi"
        
        if not installer_script.exists():
            self.log("NSIS installer script not found", "ERROR")
            return False
        
        if self.run_command(["makensis", str(installer_script)], cwd=installer_script.parent):
            self.log("Windows installer created successfully!", "SUCCESS")
            return True
        else:
            return False
    
    def create_linux_package(self):
        """Create Linux distribution package"""
        if self.system == "windows":
            self.log("Skipping Linux package (on Windows)", "WARNING")
            return True
        
        self.log("Creating Linux package...")
        
        package_name = "nexusdownloader-linux-x86_64"
        package_dir = self.root_dir / package_name
        
        # Clean and create package directory
        if package_dir.exists():
            shutil.rmtree(package_dir)
        package_dir.mkdir()
        
        # Copy files
        files_to_copy = [
            ("dist/NexusDownloader", "NexusDownloader"),
            ("installer/linux/install.sh", "install.sh"),
            ("README.md", "README.md"),
            ("LICENSE", "LICENSE"),
            ("assets", "assets")
        ]
        
        for src, dst in files_to_copy:
            src_path = self.root_dir / src
            dst_path = package_dir / dst
            
            if src_path.exists():
                if src_path.is_dir():
                    shutil.copytree(src_path, dst_path)
                else:
                    shutil.copy2(src_path, dst_path)
            else:
                if dst in ["README.md", "LICENSE", "assets"]:
                    self.log(f"Optional file not found: {src}", "WARNING")
                else:
                    self.log(f"Required file not found: {src}", "ERROR")
                    return False
        
        # Make scripts executable
        install_script = package_dir / "install.sh"
        executable = package_dir / "NexusDownloader" / "NexusDownloader"
        
        for script in [install_script, executable]:
            if script.exists():
                script.chmod(0o755)
        
        # Create installation instructions
        install_txt = package_dir / "INSTALL.txt"
        install_txt.write_text("""NexusDownloader Linux Installation Instructions
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
""")
        
        # Create tarball
        import tarfile
        tarball_path = self.root_dir / f"{package_name}.tar.gz"
        with tarfile.open(tarball_path, "w:gz") as tar:
            tar.add(package_dir, arcname=package_name)
        
        # Clean up temporary directory
        shutil.rmtree(package_dir)
        
        self.log(f"Linux package created: {tarball_path.name}", "SUCCESS")
        return True
    
    def build(self, skip_installer=False):
        """Main build process"""
        self.log("Starting NexusDownloader build process...")
        
        if not self.check_python():
            return False
        
        if not self.setup_environment():
            return False
        
        self.clean_build()
        self.create_assets()
        
        if not self.build_executable():
            return False
        
        if not skip_installer:
            if self.system == "windows":
                self.build_windows_installer()
            else:
                self.create_linux_package()
        
        self.log("Build process completed!", "SUCCESS")
        self.print_summary()
        return True
    
    def print_summary(self):
        """Print build summary"""
        print("\n" + "="*50)
        print("BUILD SUMMARY")
        print("="*50)
        
        exe_path = self.dist_dir / "NexusDownloader"
        if exe_path.exists():
            print(f"✓ Executable: {exe_path}")
        
        if self.system == "windows":
            installer_path = self.root_dir / "installer" / "windows" / "NexusDownloader_Setup_v2.0.0.exe"
            if installer_path.exists():
                print(f"✓ Windows Installer: {installer_path}")
            else:
                print("⚠ Windows Installer: Not created (NSIS required)")
        else:
            package_path = self.root_dir / "nexusdownloader-linux-x86_64.tar.gz"
            if package_path.exists():
                print(f"✓ Linux Package: {package_path}")
        
        print("\nTo run the application:")
        if self.system == "windows":
            print(f"  {exe_path}\\NexusDownloader.exe")
        else:
            print(f"  {exe_path}/NexusDownloader")
        
        print("\nTo install:")
        if self.system == "windows":
            print("  Run the installer or copy the dist/NexusDownloader folder")
        else:
            print("  Extract the .tar.gz and run: sudo ./install.sh")

def main():
    parser = argparse.ArgumentParser(description="NexusDownloader build script")
    parser.add_argument("--skip-installer", action="store_true", 
                       help="Skip creating installer/package")
    parser.add_argument("--clean-only", action="store_true",
                       help="Only clean build artifacts")
    
    args = parser.parse_args()
    
    builder = Builder()
    
    if args.clean_only:
        builder.clean_build()
        builder.log("Build artifacts cleaned", "SUCCESS")
        return
    
    success = builder.build(skip_installer=args.skip_installer)
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()