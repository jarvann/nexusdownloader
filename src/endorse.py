import requests
import logging
import time

# Transient-failure retry policy. Mirrors download.get_download_url so a single
# slow/blipping Nexus response doesn't silently drop an endorsement. These should
# collapse into the planned shared nexus client (TECH_DEBT.md D6) -- until then,
# keep them in lockstep with download.MAX_DOWNLOAD_RETRIES / RETRY_BACKOFF_BASE.
MAX_ENDORSE_RETRIES = 3
RETRY_BACKOFF_BASE = 3  # seconds; doubles each attempt (3s, 6s, ...)

# Import unified logging
try:
    from utils.unified_logging import get_logger
    _unified_available = True
except ImportError:
    _unified_available = False

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
        CONFIG = MinimalConfig()

LOGGER = None

# Initialize module logger
_module_logger = logging.getLogger(__name__)

# Add this global variable to hold the logger instance
LOGGER = None

def set_endorse_logger(logger):
    global LOGGER
    LOGGER = logger

def get_endorse_logger():
    """Get the endorse logger, preferring unified logging if available."""
    global LOGGER
    if LOGGER is None:
        if _unified_available:
            LOGGER = get_logger('download')  # Endorsement is part of download operations
        else:
            LOGGER = _module_logger
    return LOGGER

def endorse_mod(game_domain, mod_id, file_id):
    """Endorse a mod on NexusMods."""
    logger = get_endorse_logger()
    logger.info(f"Starting endorsement for mod {mod_id} in game {game_domain}")
    
    header = {
        'apikey': CONFIG.AccessControl.NexusAPIKey,
        'Accept': 'application/json',
    }

    url = f'https://api.nexusmods.com/v1/games/{game_domain}/mods/{mod_id}/endorse.json'
    logger.debug(f"Making endorsement request to: {url}")

    last_error = None
    for attempt in range(1, MAX_ENDORSE_RETRIES + 1):
        try:
            response = requests.post(url, headers=header, timeout=30)
            response.raise_for_status()

            result = response.json()
            logger.info(f"Successfully endorsed mod {mod_id} (file {file_id}) in {game_domain}")
            logger.debug(f"Endorsement response: {result}")
            return result

        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < MAX_ENDORSE_RETRIES:
                backoff = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(f"Endorsement failed for mod {mod_id} "
                               f"(attempt {attempt}/{MAX_ENDORSE_RETRIES}): {e}. "
                               f"Retrying in {backoff}s...")
                time.sleep(backoff)
                continue
            logger.error(f"Failed to endorse mod {mod_id} after "
                         f"{MAX_ENDORSE_RETRIES} attempts: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error endorsing mod {mod_id}: {e}")
            raise

    if last_error:
        raise last_error