# NexusDownloader

Welcome to **NexusDownloader** – your streamlined, no-frills solution for downloading Nexus Mods faster and more efficiently than traditional tools like Nexus Vortex. By keeping things simple and cutting out the excess, NexusDownloader aims to get you what you need without the clutter of a full user interface.

## Why NexusDownloader?

- **Speed and Efficiency**: Designed to work swiftly in the background, it lets you download mods without any waiting around for unnecessary User Interface components.
- **Lightweight and Focused**: No complicated user interface means less overhead and a cleaner, dedicated downloading experience.
- **Straight to the Point**: Whether you're a seasoned modder or a newcomer to the scene, NexusDownloader is built to be intuitive and hassle-free.
- **Intended for use with large Collections**: Large collections are becoming more common on Nexus, but who wants to wait hours for Vortex to download when you can download all the mods in minutes?

## Features

- **Background Operation**: Runs quietly in the background, ensuring you can focus on modding without distractions.
- **Optimized Downloads**: Streamlined functionality that prioritizes speed and reliability over fancy displays.
- **Simple Setup**: Easy to integrate with your workflow, making mod downloads smoother and less complicated.

## Getting Started
* I assume you already have python installed, if not, you need to install the latest version from here: https://www.python.org/downloads/
   * If you are installing python for the first time, I highly recommend you add the Python directory to your PATH environment variables. The instructions below assume that.
   * After the installation, open a shell to the location that you downloaded the files
   * Run these two commands: 
   ```bash
   python -m pip install --upgrade pip

   pip install -r requirements.txt
* Based on my research, you do not have to have Nexus Premium User License to get an API Key.

To set up NexusDownloader, follow these simple steps:

1. **Clone the Repository** (Or download the files from the SRC folder)  
   ```bash
   git clone https://github.com/jarvann/nexusdownloader.git
2. **Go to Nexus and Get or Create your API Key**:
   * Open Nexus to the following URL: https://next.nexusmods.com/settings/api-keys
   * Look for "Nexus Mod Manager" Integration, and if necessary, create the key.
   * Copy the API key for "Nexus Mod Manager" Integration.
   * Open the config.json file located in the ~/nexusdownloader/src folder
   * Paste the API key in between the quotes for "NexusAPIKey"
   ```json
    {
        "AccessControl": {
            "NexusAPIKey": "{YOUR API KEY}"
        }
    }
3. **Open Vortex on your machine**
4. **Configure (or Confirm) your Mods Download folder in Vortex**
5. **Copy the root folder (not including the game name) and paste it into the DownloadsFolderRoot the config file:**
   * Ensure that the file paths have double '\' character for each part of the path: "C:\\\VortexDownloads" and not "C:\VortexDownloads".
    ```json
    {
        "VortexSettings" : {
            "DownloadsFolderRoot": "",
        }
    }

## Running the app

### Get the Path to your collection.json
1. You need to have Nexus Vortex installed, sorry, can't get around this.
2. Download and enable the collection you want to download in Vortex, but do not start the installation (**THIS IS IMPORTANT**)
3. Go to the Mods tab in Vortex
   * Find Mod for that collection
   * Right Click, and select "Open in File Manager" (File Explorer window should open and you should see collection.json)
   * Click on the file to select it
   * Right-Click the file and in the context menu, select "Copy As Path"
   * Recommend pasting it into Notepad or other text editor to verify the path.
4. Close Vortex

### Running the downloader
**I highly recommend you make sure Vortex is closed when you do this**
1. Open a terminal or command prompt and change the director (cd XXX) to where the python files are located
   ```powershell
   cd c:\ThePlaceIPutNexusDownloader\src
2. Type the following command:
   ```powershell
   py .\loadcollection.py --json "{PASTE PATH TO COLLECTION.JSON FILE HERE}" --gamefolder "{JUST THE FOLDER NAME OF THE GAME IN THE VORTEX DOWNLOADS}" --maxthreads [NumberOfThreads]
3. You will paste the path to the collection.json file as the first parameter. I recommend using the full file path.
4. You will provide the number of Download Threads you would like to use for the second parameter. I don't recommend going above 15 personally.
5. Example:
   ```powershell
   py .\loadcollection.py --json "C:\VortexMods\cyberpunk2077\DYSTOPIA-An-NSFW-AIO-pack-by-dae-492875-7-1749633328\collection.json" --gamefolder "cyberpunk2077"  --maxthreads 15
   py .\loadcollection.py --json "C:\VortexMods\skyrimse\DOMAIN-An-AE-NSFW-AIO-pack-by-dae-485687-195-1748399387\collection.json" --gamefolder "skyrimse"  --maxthreads 15

6. **[Optional]**: If you would like to endorse the mods, you can do so at the end of the app run. The system will prompt you. I have noticed that there is a 24hr minimum from time of download for endorsing a mod, so it might not work immediately. You can type 'y' and hit enter to give it a shot. Assuming it doesn't work, and you are just altruistic because the Mod Authors deserve that recognition, wait a day or two, and then run the command like this, but adding the "--endorseonly" parameter. This will run the collection again, but with no downloads, and will use the number of threads you set, or 10 by default. It only takes a few seconds to run, but it will make a Mod Author's day!
   ```powershell
   py .\loadcollection.py --endorseonly --json "C:\VortexMods\cyberpunk2077\DYSTOPIA-An-NSFW-AIO-pack-by-dae-492875-7-1749633328\collection.json"  --maxthreads 15

6. Watch the output:
   * I will tell you how many downloads to expect at the beginning.
   * The system will spawn the number of threads that you set in the command line.
   * System manages the threads, each one will spawn a download for one of the files.
   * The system will make two web requests for each download
     * One to get the download URI (this counts against the daily max API calls)
     * One to download the file.
   * When the download completes, you will see it in the Vortex download folder for that game
   * I have put timers in for each download, and one timer for the whole system.
7. When download is complete, you can close the command line.

## Finish installing the collection
1. Open Vortex
2. Give it a moment, the Vortex client will notice all of the new downloads and add them to the catalog. You are back in slow Vortex, so please be patient, this isn't my fault.
3. Open the Collection Tab.
4. Install the Collection
   * The collection will recognize all of the downloads and not try to redownload them
   * Offsite mods may not work, so those likely will still need to be downloaded
