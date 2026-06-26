import requests
import logging
from datetime import timedelta

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
    
    try:
        response = requests.post(url, headers=header, timeout=30)
        response.raise_for_status()
        
        result = response.json()
        logger.info(f"Successfully endorsed mod {mod_id} (file {file_id}) in {game_domain}")
        logger.debug(f"Endorsement response: {result}")
        
        return result
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to endorse mod {mod_id}: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error endorsing mod {mod_id}: {e}")
        raise