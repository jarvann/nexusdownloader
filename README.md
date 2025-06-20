# NexusDownloader

**NexusDownloader** is the fast lane for mod collectors! Tired of waiting hours for Vortex to fetch a giant collection? NexusDownloader is a streamlined, no-nonsense tool that lets you download entire Nexus collections in minutes, not hours. With a simple, user-friendly GUI and robust command-line support, it’s built for both newcomers and power users who want speed, control, and reliability—without the bloat.

**How does it work?**  
Just launch the included `run.bat`—if you don’t have Python, it’ll help you install it. The GUI walks you through entering your Nexus API key, picking your downloads folder, and selecting your collection JSON. Choose your download speed (thread count), and hit Start. NexusDownloader checks that Vortex isn’t running (and can close it for you), then downloads all your mods in parallel, showing live progress and error counts. When finished, you’ll be prompted to endorse the mods—supporting the authors who make your favorite content possible.

Whether you prefer a click-and-go interface or want to automate everything from the command line, NexusDownloader is the fastest way to get your Nexus collections ready for Vortex—so you can spend less time waiting and more time playing.

---

## Why NexusDownloader?

- **Speed and Efficiency**: Designed to work swiftly in the background, it lets you download mods without any waiting around for unnecessary User Interface components.
- **Lightweight and Focused**: No complicated user interface means less overhead and a cleaner, dedicated downloading experience.
- **Straight to the Point**: Whether you're a seasoned modder or a newcomer to the scene, NexusDownloader is built to be intuitive and hassle-free.
- **Intended for use with large Collections**: Large collections are becoming more common on Nexus, but who wants to wait hours for Vortex to download when you can download all the mods in minutes?

---

## Features

- **Background Operation**: Runs quietly in the background, ensuring you can focus on modding without distractions.
- **Optimized Downloads**: Streamlined functionality that prioritizes speed and reliability over fancy displays.
- **Simple Setup**: Easy to integrate with your workflow, making mod downloads smoother and less complicated.
- **User-Friendly GUI**: A graphical interface for easy configuration and operation.
- **Command-Line Support**: Advanced users can still use the command line for automation or scripting.

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
   - Paste your API key under `"NexusAPIKey"`.
   - Set your Vortex downloads folder under `"DownloadsFolderRoot"`.  
     Example:
     ```json
     {
         "AccessControl": {
             "NexusAPIKey": "YOUR_API_KEY_HERE"
         },
         "VortexSettings": {
             "DownloadsFolderRoot": "C:\\VortexDownloads"
         }
     }
     ```

---

## Running NexusDownloader

### Option 1: Using the GUI (Recommended)

1. **Double-click `run.bat`** in the project root.
   - If Python is not installed, you will be prompted to install it.
   - The GUI will launch automatically.

2. **Configure Settings**
   - Go to `File > Settings` to enter your Nexus API key and set your downloads folder.

3. **Select Your Collection**
   - Use the "Browse..." button to select your `collection.json` file.
   - Set the downloads folder if not already set.

4. **Choose Download Threads**
   - Select your preferred download speed (number of threads) from the dropdown.

5. **Start Download**
   - Click "Start Download".
   - If Vortex is running, you will be prompted to close it.
   - Progress and errors will be displayed in the GUI.
   - When downloads finish, you will be prompted to endorse mods (optional).

6. **After Download**
   - Open Vortex and allow it to detect the new downloads.
   - Install the collection as usual.

---

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

## How It Works

- The system will tell you how many downloads to expect.
- It will spawn the number of threads you set.
- Each thread downloads one file at a time.
- Download progress and errors are shown in the GUI or command line.
- When complete, your mods will appear in the Vortex downloads folder for your game.

---

## After Downloading

1. **Open Vortex**
2. Wait for Vortex to detect the new downloads.
3. Go to the Collection Tab and install the collection.
   - The collection will recognize all the downloads and not try to redownload them.
   - Offsite mods may still need to be downloaded manually.

---

## Troubleshooting

- **Vortex must be closed** before downloading. The GUI will prompt you if it is running.
- **API Key or Download Folder not set?** Use the GUI's Settings dialog.
- **Python not installed?** The `run.bat` will help you install it.
- **Still have issues?** Check the output/error messages in the GUI or terminal for more details.

---

## Thanks

Thank you for supporting mod authors by endorsing their work!
