import json
import argparse
import concurrent.futures
import logging
from endorse import endorse_mod  # Importing the endorse function from endorse.py
from download import download_file  # Importing the download function from download.py
from download import set_download_logger  # Importing the set_download_logger function from download.py
from endorse import set_endorse_logger  # Importing the endorse function from endorse.py
import threading
import time
import os
from datetime import datetime, timedelta


lock = threading.Lock()
COUNTER = 0
COMPLETED_COUNTER = 0
ERROR_COUNTER = 0
LICENSE_KEY = ""
NEXUS_API_KEY = ""
GAME_DOMAIN = ""

# Custom VERBOSE log level
VERBOSE_LEVEL_NUM = 15
logging.addLevelName(VERBOSE_LEVEL_NUM, "VERBOSE")
def verbose(self, message, *args, **kws):
    if self.isEnabledFor(VERBOSE_LEVEL_NUM):
        self._log(VERBOSE_LEVEL_NUM, message, args, **kws)
logging.Logger.verbose = verbose

def setup_logger(game_domain):
    # Ensure logs directory exists at project root
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    logs_dir = os.path.join(root_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    log_filename = os.path.join(logs_dir, f"log_{game_domain}_{timestamp}.log")
    logger = logging.getLogger("nexusdownloader")
    logger.setLevel(VERBOSE_LEVEL_NUM)
    fh = logging.FileHandler(log_filename, mode='a', encoding='utf-8')
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(threadName)s: %(message)s')
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    # Ensure all threads flush after each log
    # for handler in logger.handlers:
    #     handler.flush = True

    set_download_logger(logger)  # Set the logger for download.py
    set_endorse_logger(logger) # Set the logger for endorse.py
    logger.verbose(f"Logger initialized for game domain: {game_domain}")    
    
    return logger

def incrementCOUNTER_ThreadSafe():
    global COUNTER
    with lock:
        COUNTER += 1
    return COUNTER

def incrementCOMPLETED_COUNTER_ThreadSafe():
    global COMPLETED_COUNTER
    with lock:
        COMPLETED_COUNTER += 1
    return COMPLETED_COUNTER

def incrementERROR_COUNTER_ThreadSafe():
    global ERROR_COUNTER
    with lock:
        ERROR_COUNTER += 1
        print(f"ERRORS: {ERROR_COUNTER}", flush=True)
    return ERROR_COUNTER

# Function to load mods from a JSON file
def load_mods_from_json(file_path, logger=None):
    global GAME_DOMAIN
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        GAME_DOMAIN = data["info"]["domainName"]
        mods = []
        for entry in data['mods']:
            try:
                mod_id = entry['source']['modId']
                file_id = entry['source']['fileId']
                mods.append((mod_id, file_id))
            except KeyError as e:
                if logger:
                    logger.error(f"Skipping entry due to missing key: {e}")
        if logger:
            logger.verbose(f"Loaded {len(mods)} mods from the JSON file.")
        return mods
    except Exception as e:
        if logger:
            logger.error(f"Error loading mods from JSON: {e}")
        return []

# Main function to execute concurrent downloads
def main(mods, gamefolder, max_threads=10, logger=None):
    overall_start = time.time()
    if logger:
        logger.verbose(f"Starting downloads for {len(mods)} mods with {max_threads} threads.")

    with concurrent.futures.ThreadPoolExecutor(max_workers=int(max_threads)) as executor:
        futures = []
        for mod_id, file_id in mods:
            current_counter = incrementCOUNTER_ThreadSafe()
            futures.append(executor.submit(download_file, GAME_DOMAIN, gamefolder, mod_id, file_id, current_counter))

        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
                incrementCOMPLETED_COUNTER_ThreadSafe()
                # This print statement is intentionally left as print for GUI progress parsing
                print(f"0000\tCompleted download for file {COMPLETED_COUNTER} of {len(mods)}")
                if logger:
                    logger.verbose(f"Completed download for file {COMPLETED_COUNTER} of {len(mods)}")
                
                print(f"PROGRESS: {COMPLETED_COUNTER}/{len(mods)}")
            except Exception as e:
                incrementERROR_COUNTER_ThreadSafe()

                if logger:
                    logger.error(f"Error downloading file: {e}")

    overall_end = time.time()
    final_message = f"Total Execution Time for download: {timedelta(seconds=(overall_end - overall_start))}. Aren't you glad you decided to download using this instead of Vortex?"

    print(final_message)
    if logger:
        logger.verbose(final_message)

def endorse_mods(mods, max_threads=10, logger=None):
    if logger:
        logger.verbose(f"Starting endorsement for {len(mods)} mods with {max_threads} threads.")
    with concurrent.futures.ThreadPoolExecutor(max_workers=int(max_threads)) as executor:
        futures = []
        for mod_id, file_id in mods:
            futures.append(executor.submit(endorse_mod, GAME_DOMAIN, mod_id, file_id))

        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except Exception as e:
                if logger:
                    logger.error(f"Error endorsing mod file: {e}")
    if logger:
        logger.verbose("Finished endorsing all mods.")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Parse JSON and download mods asynchronously")
    parser.add_argument('-f', '--gamefolder', help="The folder name where the downloads will be saved. This needs to match Vortex", required=True, default='', type=str)
    parser.add_argument('-j', '--json', help="Path to the JSON file containing mod data", required=True, default='', type=str)
    parser.add_argument('-t', '--maxthreads', help="The total number of active download threads you want, it's 1:1 for files",
                        required=False, default=10, type=int)
    parser.add_argument('-e', '--endorseonly', action='store_true', help="Endorse mods only without downloading them", default=False)
    args = parser.parse_args()

    # Temporary logger for loading JSON to get game domain
    temp_logger = logging.getLogger("temp")
    mods = load_mods_from_json(args.json, temp_logger)
    # Now setup logger with game domain
    logger = setup_logger(GAME_DOMAIN if GAME_DOMAIN else "unknown")
    # Reload mods with logger for proper error logging
    mods = load_mods_from_json(args.json, logger)

    if args.endorseonly:
        logger.verbose("Endorsing mods only, no downloads will be performed.")
        endorse_mods(mods, args.maxthreads, logger)
        exit(0)
    else:
        main(mods, args.gamefolder, args.maxthreads, logger)

    exit(0)