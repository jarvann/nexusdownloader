import requests
from datetime import timedelta
from config import get_config 

CONFIG = get_config()

LOGGER = None

def set_endorse_logger(logger):
    global LOGGER
    LOGGER = logger

def endorse_mod(game_domain, mod_id, file_id):

    header = {
        'apikey': CONFIG.AccessControl.NexusAPIKey,
        'Accept': 'application/json',
    }


    url = f'https://api.nexusmods.com/v1/games/{game_domain}/mods/{mod_id}/endorse.json'
    response = requests.post(url, headers=header)
    response.raise_for_status()
    if LOGGER:
        LOGGER.verbose(f"Endorsed mod {mod_id} with file {file_id} successfully.")
        
    return response.json()