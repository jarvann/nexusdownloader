# NexusDownloader

**NexusDownloader** is the fast lane for mod collectors! Tired of waiting hours for Vortex to fetch a giant collection? NexusDownloader is a streamlined, no-nonsense tool that lets you download entire Nexus collections in minutes, not hours. With a simple, user-friendly GUI and robust command-line support, it's built for both newcomers and power users who want speed, control, and reliability—without the bloat.

**How does it work?**  
Just launch the included `run.bat`—if you don't have Python, it'll help you install it. The GUI walks you through entering your Nexus API key, picking your downloads folder, and selecting your collection JSON. Choose your download speed (thread count), and hit Start. NexusDownloader checks that Vortex isn't running (and can close it for you), then downloads all your mods in parallel, showing live progress and error counts. When finished, you'll be prompted to endorse the mods—supporting the authors who make your favorite content possible.

Whether you prefer a click-and-go interface or want to automate everything from the command line, NexusDownloader is the fastest way to get your Nexus collections ready for Vortex—so you can spend less time waiting and more time playing.

---

## Why NexusDownloader?

- **Speed and Efficiency**: Multi-threaded downloads with up to 35 concurrent connections for maximum throughput
- **User-Friendly GUI**: Modern interface with real-time progress tracking and comprehensive settings management
- **Resume Downloads**: Automatically resumes interrupted downloads and tracks completed files
- **Smart Vortex Integration**: Detects and manages Vortex state, downloads directly to your game folders
- **Robust Error Handling**: Advanced retry logic, detailed logging, and graceful error recovery
- **Cross-Platform**: Works on Windows and Linux with automated build and packaging systems
- **Intended for Large Collections**: Optimized for downloading hundreds of mods quickly and reliably

---

## Features

- **Multi-threaded Downloads**: Download multiple mods simultaneously with configurable thread counts
- **Real-time Progress Tracking**: Live progress bars, download speeds, and ETA estimates
- **Advanced Configuration**: Comprehensive settings for API limits, download behavior, and Vortex integration
- **Resume Capability**: Automatically resume interrupted downloads from where they left off
- **Mod Endorsement**: Optional batch endorsement of downloaded mods to support authors
- **Detailed Logging**: Comprehensive logging with performance metrics and error tracking
- **Security Features**: Secure API key storage, SSL validation, and input sanitization
- **Modern GUI**: Built with PySide6 for a responsive, native interface experience

---

## Getting Started

### 1. Prerequisites

- **Python 3.12 or newer** is required.  
  If you do not have Python installed, the included `run.bat` will help you install it automatically.

### 2. Installation

1. **Download or Clone the Repository**
   ```bash
   git clone https://github.com/jarvann/nexusdownloader.git
   ```
   Or download the files and extract them to a folder of your choice.

2. **Install Python Dependencies**
   - Open a terminal or command prompt in the project folder. You may not need to do this, because it should check and confirm when you run the BAT file.
   - Run:
     ```bash
     python -m pip install --upgrade pip
     pip install -r requirements.txt
     ```

---

## Configuration

1. **Get Your Nexus API Key**
   - Go to: https://next.nexusmods.com/settings/api-keys
   - Look for "Nexus Mod Manager" Integration, and create or copy your API key.

2. **Configure the Application**
   - Open the GUI (see below) and go to `File > Settings`, or manually edit `config.json` in the `src` folder.
   - The configuration uses the new JSON format with sections for API, Vortex, Downloads, Logging, and Security settings.

---

## Running NexusDownloader

### Option 1: Using the GUI (Recommended)

1. **Double-click `run.bat`** in the project root.
   - If Python is not installed, you will be prompted to install it.
   - The GUI will launch automatically.

2. **Configure Settings**
   - Go to `File > Settings` to enter your Nexus API key and set your downloads folder.
   - Configure download threads, retry settings, and other preferences.

3. **Select Your Collection**
   - Use the "Browse..." button to select your `collection.json` file.
   - Set the downloads folder if not already set.

4. **Choose Download Threads**
   - Select your preferred download speed (number of threads) from the dropdown.

5. **Start Download**
   - Click "Start Download".
   - If Vortex is running, you will be prompted to close it.
   - Progress and errors will be displayed in the GUI with real-time updates.
   - Downloads can be paused, resumed, or cancelled as needed.

6. **After Download**
   - Open Vortex and allow it to detect the new downloads.
   - Install the collection as usual.
   - Optionally endorse downloaded mods to support the authors.

### Option 2: Using the Command Line

1. **Open a terminal or command prompt** and change directory to the `src` folder:
   ```powershell
   cd c:\ThePlaceIPutNexusDownloader\src
   ```

2. **Run the downloader:**
   ```powershell
   py .\loadcollection.py --json "C:\Path\To\Your\collection.json" --gamefolder "gamefoldername" --maxthreads 15
   ```
   - Replace the paths and values as needed.
   - Example:
     ```powershell
     py .\loadcollection.py --json "C:\VortexMods\cyberpunk2077\DYSTOPIA-An-NSFW-AIO-pack-by-dae-492875-7-1749633328\collection.json" --gamefolder "cyberpunk2077" --maxthreads 15
     ```

3. **[Optional] Endorse Mods**
   - After downloads, you can endorse mods by running:
     ```powershell
     py .\loadcollection.py --endorseonly --json "C:\Path\To\Your\collection.json" --gamefolder "gamefoldername" --maxthreads 15
     ```
   - Note: Nexus requires a 24-hour wait after download before you can endorse mods.

---

## Advanced Features

### Resume Downloads
- Automatically detects and resumes interrupted downloads
- Creates resume files to track completion state
- Handles both individual file resumption and collection-level progress

### Build System
- Cross-platform build scripts for creating executables
- Windows: `build_windows.bat` creates standalone .exe with NSIS installer
- Linux: `build_linux.sh` creates AppImage and .deb packages
- Universal: `build.py` provides cross-platform build automation

### Logging and Monitoring
- Comprehensive logging with configurable levels (DEBUG, INFO, WARNING, ERROR)
- Performance metrics tracking for download speeds and completion times
- Automatic log rotation and cleanup
- Optional colored console output

---

## How It Works

- The system analyzes your collection file to determine download requirements
- Spawns the configured number of download threads (default: 35)
- Each thread downloads files concurrently with progress reporting
- Real-time GUI updates show individual file progress and overall statistics
- Completed downloads are tracked to enable resume functionality
- When complete, mods appear in your Vortex downloads folder ready for installation

---

## After Downloading

1. **Open Vortex**
2. Wait for Vortex to detect the new downloads.
3. Go to the Collection Tab and install the collection.
   - The collection will recognize all downloads and not try to redownload them.
   - Offsite mods may still need to be downloaded manually.

---

## Building and Distribution

### Creating Executables

- **Windows**: Run `build_windows.bat` to create standalone executable and installer
- **Linux**: Run `build_linux.sh` to create AppImage and Debian packages
- **Cross-platform**: Use `python build.py` with options for different build targets

### Requirements for Building
- PyInstaller for executable creation
- NSIS (Windows) for installer creation
- Standard Linux build tools for packaging

---

## Troubleshooting

- **Vortex must be closed** before downloading. The GUI will prompt you if it is running.
- **API Key or Download Folder not set?** Use the GUI's Settings dialog or check `src/config.json`.
- **Python not installed?** The `run.bat` will help you install it automatically.
- **Downloads failing?** Check the logs folder for detailed error information.
- **GUI not starting?** Ensure PySide6 is installed: `pip install PySide6>=6.4.0`
- **Still have issues?** Check the output/error messages in the GUI or terminal for more details.

---

## Requirements

- **Python 3.12+** (automatically managed by `run.bat`)
- **PySide6 6.4.0+** for GUI functionality
- **requests 2.28.0+** for HTTP operations
- **cryptography 3.4.8+** for security features
- **Additional dependencies** as listed in `requirements.txt`

---

## License

MIT License - See LICENSE file for details.

---

## Thanks

Thank you for supporting mod authors by endorsing their work!