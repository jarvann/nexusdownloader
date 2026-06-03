import os
import threading
import requests
import time
import logging
from datetime import timedelta

# Thread-safe stdout for machine-readable per-thread progress records consumed
# by the GUI parser. Each record is a single tab-delimited line prefixed with
# "@DL". See gui/main_window.py:_parse_output_line for the consumer.
_stdout_lock = threading.Lock()


def emit_status(*fields):
    """Emit a single tab-delimited @DL status record to stdout, thread-safely."""
    line = "@DL\t" + "\t".join(str(f).replace("\t", " ") for f in fields)
    with _stdout_lock:
        print(line, flush=True)

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

# Import unified logging
try:
    from utils.unified_logging import get_logger
    _unified_available = True
except ImportError:
    _unified_available = False

# Add this global variable to hold the logger instance
LOGGER = None

# Initialize module logger
_module_logger = logging.getLogger(__name__)

# Retry settings for transient network failures during downloads
MAX_DOWNLOAD_RETRIES = 3
RETRY_BACKOFF_BASE = 3  # seconds; doubles each attempt (3s, 6s, ...)

def set_download_logger(logger):
    global LOGGER
    LOGGER = logger

def get_download_logger():
    """Get the download logger, preferring unified logging if available."""
    global LOGGER
    if LOGGER is None:
        if _unified_available:
            LOGGER = get_logger('download')
        else:
            LOGGER = _module_logger
    return LOGGER

def get_download_url(game_domain, mod_id, file_id):
    """Get download URL for a specific mod file."""
    logger = LOGGER or _module_logger
    logger.debug(f"Requesting download URL for {game_domain}/{mod_id}/{file_id}")
    
    header = {
        'apikey': CONFIG.AccessControl.NexusAPIKey,
        'Accept': 'application/json',
    }

    url = f'https://api.nexusmods.com/v1/games/{game_domain}/mods/{mod_id}/files/{file_id}/download_link.json'

    # Retry transient API failures (timeouts, 5xx, dropped connections). Without
    # this a single slow Nexus API response would skip the mod entirely -- and a
    # missing timeout could hang a worker thread forever.
    last_error = None
    for attempt in range(1, MAX_DOWNLOAD_RETRIES + 1):
        try:
            logger.debug(f"Making API request to: {url}")
            response = requests.get(url, headers=header, timeout=30)
            response.raise_for_status()
            download_info = response.json()

            if download_info:
                logger.info(f"Successfully obtained download URL for mod {mod_id}, file {file_id}")
                return download_info[0]['URI']
            logger.warning(f"No download links available for mod {mod_id}, file {file_id}")
            return None

        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < MAX_DOWNLOAD_RETRIES:
                backoff = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(f"URL fetch failed for mod {mod_id}, file {file_id} "
                               f"(attempt {attempt}/{MAX_DOWNLOAD_RETRIES}): {e}. Retrying in {backoff}s...")
                time.sleep(backoff)
                continue
            logger.error(f"Failed to get download URL for mod {mod_id}, file {file_id} "
                         f"after {MAX_DOWNLOAD_RETRIES} attempts: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error getting download URL for mod {mod_id}, file {file_id}: {e}")
            raise

    if last_error:
        raise last_error

def download_file(game_domain, gamefolder, mod_id, file_id, current_counter):
    """Download a specific mod file."""
    logger = get_download_logger()
    download_start = time.time()
    
    logger.info(f"Starting download for mod {mod_id}, file {file_id} (#{current_counter})")

    download_dir = os.path.join(CONFIG.VortexSettings.DownloadsFolderRoot, gamefolder)

    # Ensure download directory exists
    os.makedirs(download_dir, exist_ok=True)
    logger.debug(f"Download directory: {download_dir}")

    # Retry transient network failures (IncompleteRead, dropped connections, timeouts).
    # Nexus download URLs are short-lived signed links, so re-fetch a fresh URL each attempt.
    last_error = None
    for attempt in range(1, MAX_DOWNLOAD_RETRIES + 1):
        # Get a fresh download URL (signed URLs expire and can 403 on retry)
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

        if attempt == 1:
            logger.info(f"Starting download of {filename} (#{current_counter})")
        else:
            logger.info(f"Retry {attempt}/{MAX_DOWNLOAD_RETRIES} for {filename} (#{current_counter})")

        # Lane id = the worker thread name, so the GUI can keep a stable row per thread
        lane = threading.current_thread().name

        try:
            with requests.get(url, stream=True, timeout=30) as r:
                r.raise_for_status()
                file_size = int(r.headers.get('content-length', 0))
                logger.debug(f"File size: {file_size} bytes")

                emit_status("START", lane, file_size, filename)

                downloaded_size = 0
                file_start = time.time()
                last_emit = 0.0
                with open(file_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        # Throttle progress records to ~2/sec per thread to bound stdout volume
                        now = time.time()
                        if now - last_emit >= 0.5:
                            speed_bps = downloaded_size / max(now - file_start, 1e-6)
                            emit_status("PROG", lane, downloaded_size, file_size, int(speed_bps))
                            last_emit = now

            # Guard against truncated downloads that don't raise (server closed cleanly mid-stream)
            if file_size and downloaded_size < file_size:
                raise requests.exceptions.ChunkedEncodingError(
                    f"Incomplete download: got {downloaded_size} of {file_size} bytes")

            file_elapsed = max(time.time() - file_start, 1e-6)
            speed_bps = downloaded_size / file_elapsed
            emit_status("DONE", lane, downloaded_size, int(speed_bps))

            elapsed = timedelta(seconds=time.time() - download_start)
            speed = speed_bps / 1024 / 1024  # MB/s
            logger.info(f"Successfully downloaded {filename} ({downloaded_size} bytes, {speed:.2f} MB/s, Time: {elapsed}) (#{current_counter})")
            return True

        except (requests.exceptions.RequestException, OSError) as e:
            last_error = e
            emit_status("ERR", lane, filename)
            # Clean up partial file before retrying
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.debug(f"Cleaned up partial file: {file_path}")

            if attempt < MAX_DOWNLOAD_RETRIES:
                backoff = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(f"Download failed for {filename} (attempt {attempt}/{MAX_DOWNLOAD_RETRIES}): {e}. Retrying in {backoff}s...")
                time.sleep(backoff)
                continue

            logger.error(f"Download failed for {filename} after {MAX_DOWNLOAD_RETRIES} attempts: {e}")
            raise

    # Should be unreachable, but raise the last error if the loop exits without returning
    if last_error:
        raise last_error