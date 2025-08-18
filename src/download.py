import os
import requests
import time
import logging
from datetime import timedelta

# For backward compatibility, try new config system first, then fall back to old
try:
    from config.config_manager import ConfigManager
    _config_manager = ConfigManager('config.json')
    CONFIG = _config_manager.get_config()
    # Compatibility wrapper
    class LegacyConfig:
        def __init__(self, config):
            self.AccessControl = type('AccessControl', (), {
                'NexusAPIKey': config.nexus_api.api_key or _config_manager.get_api_key() or ""
            })()
            self.VortexSettings = type('VortexSettings', (), {
                'DownloadsFolderRoot': config.vortex.downloads_folder_root
            })()
    CONFIG = LegacyConfig(CONFIG)
except ImportError:
    try:
        # Fallback to old config system
        from config import get_config
        CONFIG = get_config()
    except (ImportError, FileNotFoundError, KeyError):
        # Create a minimal fallback config
        class MinimalConfig:
            def __init__(self):
                self.AccessControl = type('AccessControl', (), {
                    'NexusAPIKey': ""
                })()
                self.VortexSettings = type('VortexSettings', (), {
                    'DownloadsFolderRoot': "C:/VortexDownloads"
                })()
        CONFIG = MinimalConfig()

# Add this global variable to hold the logger instance
LOGGER = None

# Initialize module logger
_module_logger = logging.getLogger(__name__)

def set_download_logger(logger):
    global LOGGER
    LOGGER = logger

def get_download_url(game_domain, mod_id, file_id):
    """Get download URL for a specific mod file."""
    logger = LOGGER or _module_logger
    logger.debug(f"Requesting download URL for {game_domain}/{mod_id}/{file_id}")
    
    header = {
        'apikey': CONFIG.AccessControl.NexusAPIKey,
        'Accept': 'application/json',
    }

    url = f'https://api.nexusmods.com/v1/games/{game_domain}/mods/{mod_id}/files/{file_id}/download_link.json'
    
    try:
        logger.debug(f"Making API request to: {url}")
        response = requests.get(url, headers=header)
        response.raise_for_status()
        download_info = response.json()
        
        if download_info:
            download_url = download_info[0]['URI']
            logger.info(f"Successfully obtained download URL for mod {mod_id}, file {file_id}")
            return download_url
        else:
            logger.warning(f"No download links available for mod {mod_id}, file {file_id}")
            return None
            
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to get download URL for mod {mod_id}, file {file_id}: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error getting download URL: {e}")
        raise

def download_file(game_domain, gamefolder, mod_id, file_id, current_counter):
    """Download a specific mod file."""
    logger = LOGGER or _module_logger
    download_start = time.time()
    
    logger.info(f"Starting download for mod {mod_id}, file {file_id} (#{current_counter})")

    download_dir = os.path.join(CONFIG.VortexSettings.DownloadsFolderRoot, gamefolder)
    
    # Ensure download directory exists
    os.makedirs(download_dir, exist_ok=True)
    logger.debug(f"Download directory: {download_dir}")
    
    # Get download URL
    try:
        url = get_download_url(game_domain, mod_id, file_id)
        if not url:
            logger.warning(f"No download URL found for mod {mod_id}, file {file_id}")
            return
    except Exception as e:
        logger.error(f"Failed to get download URL for mod {mod_id}, file {file_id}: {e}")
        return

    filename = os.path.basename(url.split('?')[0])
    file_path = os.path.join(download_dir, filename)
    logger.debug(f"Target file path: {file_path}")

    # Check if the file already exists
    if os.path.exists(file_path):
        elapsed = timedelta(seconds=time.time() - download_start)
        logger.info(f"File {filename} already exists. Skipping download. (#{current_counter}, Time: {elapsed})")
        return

    logger.info(f"Starting download of {filename} (#{current_counter})")

    # Proceed with downloading if the file doesn't exist
    try:
        with requests.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            file_size = int(r.headers.get('content-length', 0))
            logger.debug(f"File size: {file_size} bytes")

            downloaded_size = 0
            with open(file_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
                    downloaded_size += len(chunk)

            elapsed = timedelta(seconds=time.time() - download_start)
            speed = downloaded_size / (time.time() - download_start) / 1024 / 1024  # MB/s
            logger.info(f"Successfully downloaded {filename} ({downloaded_size} bytes, {speed:.2f} MB/s, Time: {elapsed}) (#{current_counter})")
            return True

    except requests.exceptions.RequestException as e:
        logger.error(f"Download failed for {filename}: {e}")
        # Clean up partial file
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.debug(f"Cleaned up partial file: {file_path}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error downloading {filename}: {e}")
        # Clean up partial file
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.debug(f"Cleaned up partial file: {file_path}")
        raise