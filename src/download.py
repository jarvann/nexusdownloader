import os
import requests
import time
from datetime import timedelta
from config import get_config

CONFIG = get_config()

# Add this global variable to hold the logger instance
LOGGER = None

def set_download_logger(logger):
    global LOGGER
    LOGGER = logger

def get_download_url(game_domain, mod_id, file_id):
    header = {
        'apikey': CONFIG.AccessControl.NexusAPIKey,
        'Accept': 'application/json',
    }

    url = f'https://api.nexusmods.com/v1/games/{game_domain}/mods/{mod_id}/files/{file_id}/download_link.json'
    response = requests.get(url, headers=header)
    response.raise_for_status()
    download_info = response.json()

    # Pick the first CDN link
    return download_info[0]['URI'] if download_info else None

def download_file(game_domain, gamefolder, mod_id, file_id, current_counter):
    download_start = time.time()

    download_dir = os.path.join(CONFIG.VortexSettings.DownloadsFolderRoot, gamefolder)
    # Get download URL
    url = get_download_url(game_domain, mod_id, file_id)
    if not url:
        if LOGGER:
            LOGGER.verbose(f"{str(current_counter).zfill(4)}\tNo download URL found for mod {mod_id}, file {file_id}")
        return

    filename = os.path.basename(url.split('?')[0])
    file_path = os.path.join(download_dir, filename)

    # Check if the file already exists
    if os.path.exists(file_path):
        if LOGGER:
            LOGGER.verbose(
                f"{str(current_counter).zfill(4)}\tTime({timedelta(seconds=time.time() - download_start)})\tFile {filename} already exists. Skipping download.")
        return

    if LOGGER:
        LOGGER.verbose(
            f"{str(current_counter).zfill(4)}\tTime({timedelta(seconds=time.time() - download_start)})\tDownloading {filename}")

    # Proceed with downloading if the file doesn't exist
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        with open(file_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

    if LOGGER:
        LOGGER.verbose(
            f"{str(current_counter).zfill(4)}\tTime({timedelta(seconds=time.time() - download_start)})\tDownloaded {filename} to {file_path}")
