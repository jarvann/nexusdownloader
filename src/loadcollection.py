import json
import argparse
import concurrent.futures
import logging
import sys
import download, endorse  # Import modules
from download import download_file, set_download_logger, CONFIG  # Importing functions from download.py
from endorse import endorse_mod, set_endorse_logger  # Importing functions from endorse.py
import threading
import time
import os
from datetime import datetime, timedelta

# The local-state ledger is optional: a bare CLI run without a staging dir still
# downloads fine, it just doesn't record into the ledger. Import defensively so a
# path/dependency hiccup never breaks downloads.
try:
    from utils import state_reconcile, local_state
    from utils.vortex_sync import _gen_id
    _LEDGER_AVAILABLE = True
except Exception:  # pragma: no cover - ledger is a non-critical add-on here
    _LEDGER_AVAILABLE = False


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

def setup_logger(game_domain, operation_type="download"):
    """Setup logger for collection operations using unified logging system."""
    from utils.unified_logging import create_operation_logger
    
    logger = create_operation_logger(operation_type, game_domain)
    set_download_logger(logger)  # Set the logger for download.py
    return logger
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
            except KeyError:
                if logger:
                    name = entry.get('name', 'A mod') if isinstance(entry, dict) else 'A mod'
                    logger.info(f"{name}: ModId is null, must be an off-site mod — "
                                f"check the Collection page for download instructions.")
        if logger:
            logger.verbose(f"Loaded {len(mods)} mods from the JSON file.")
        return mods
    except Exception as e:
        if logger:
            logger.error(f"Error loading mods from JSON: {e}")
        return []

# Main function to execute concurrent downloads
def _open_ledger(staging, collection_path, download_dir, game, logger):
    """Open the ledger and true-up existing downloads vs the collection.

    Returns ``(ledger, dl_lookup)`` or ``(None, {})`` when the ledger isn't in
    play (no staging dir, missing collection, or import failure). ``dl_lookup``
    maps ``(modId, fileId) -> source dict`` so the live per-file write knows each
    file's md5/size/logical name without re-parsing the collection.
    """
    if not (_LEDGER_AVAILABLE and staging and collection_path and os.path.exists(collection_path)):
        return None, {}
    try:
        # True-up: record what's already on disk + flag off-site mods to fetch.
        state_reconcile.reconcile_downloads(
            download_dir, collection_path, game, staging,
            log=(logger.info if logger else print))
        ledger = local_state.get_ledger(local_state.db_path_for(staging))
        lookup = {}
        coll = json.load(open(collection_path, encoding="utf-8"))
        for m in coll.get("mods", []):
            s = m.get("source") or {}
            if s.get("modId") and s.get("fileId"):
                lookup[(s["modId"], s["fileId"])] = (s, m.get("name", "") or "")
        return ledger, lookup
    except Exception as e:                       # never let the ledger break downloads
        if logger:
            logger.warning(f"Ledger true-up skipped ({e}); downloads continue normally.")
        return None, {}


def _record_download(ledger, lookup, mod_id, file_id, path, game, logger):
    """Upsert one freshly-downloaded file into the ledger (best-effort)."""
    if not (ledger and path):
        return
    try:
        s, name = lookup.get((mod_id, file_id), ({}, ""))
        dl_id = _gen_id(f"{mod_id}-{file_id}")
        size = os.path.getsize(path)
        ledger.upsert_download(
            dl_id, os.path.basename(path), mod_id, file_id, s.get("md5", "") or "",
            size, size, s.get("logicalFilename") or name, None,
            state="downloaded", game=game, source="nexus")
    except Exception as e:
        if logger:
            logger.debug(f"Ledger write skipped for mod {mod_id} file {file_id}: {e}")


def main(mods, gamefolder, max_threads=10, logger=None, staging=None, collection_path=None):
    overall_start = time.time()
    if logger:
        logger.verbose(f"Starting downloads for {len(mods)} mods with {max_threads} threads.")

    # Pre-index what's already on disk so we can skip the Nexus API entirely for
    # mods that are already fully downloaded. A resume otherwise fires one API
    # 'get download url' call per existing file (just to learn the filename, then
    # skip it) -> thousands at once -> Nexus rate-limits / resets connections.
    # Filenames embed the modId (-<modId>-) but not the fileId, so we skip a mod
    # only when the count of files on disk for that modId already meets what the
    # collection wants -- safe for resumes; a fresh revision is a fresh download.
    from collections import Counter
    import re as _re
    download_dir = os.path.join(CONFIG.VortexSettings.DownloadsFolderRoot, gamefolder)
    expected = Counter(str(mid) for mid, _fid in mods)
    existing = Counter()
    _modid_re = _re.compile(r"-(\d{2,7})-")
    if os.path.isdir(download_dir):
        for fn in os.listdir(download_dir):
            m = _modid_re.search(fn)
            if m:
                existing[m.group(1)] += 1
    complete = {mid for mid in expected if existing.get(mid, 0) >= expected[mid]}
    if logger and complete:
        logger.verbose(f"{len(complete)} mod(s) already on disk -- skipping the Nexus "
                       f"API for those (fast, rate-limit-safe resume).")

    # Ledger: true-up what's on disk now (incl. off-site mods), then record each
    # file as it lands. No-op when not launched with a staging dir.
    ledger, dl_lookup = _open_ledger(staging, collection_path, download_dir, GAME_DOMAIN, logger)

    with concurrent.futures.ThreadPoolExecutor(max_workers=int(max_threads)) as executor:
        futures = {}
        for mod_id, file_id in mods:
            current_counter = incrementCOUNTER_ThreadSafe()
            fut = executor.submit(
                download_file, GAME_DOMAIN, gamefolder, mod_id, file_id, current_counter,
                already_have=(str(mod_id) in complete))
            futures[fut] = (mod_id, file_id)

        for future in concurrent.futures.as_completed(futures):
            mod_id, file_id = futures[future]
            try:
                path = future.result()
                _record_download(ledger, dl_lookup, mod_id, file_id, path, GAME_DOMAIN, logger)
                incrementCOMPLETED_COUNTER_ThreadSafe()
                # This print statement is intentionally left as print for GUI progress parsing
                print(f"0000\tCompleted download for file {COMPLETED_COUNTER} of {len(mods)}")
                sys.stdout.flush()  # Ensure immediate output to GUI
                if logger:
                    logger.verbose(f"Completed download for file {COMPLETED_COUNTER} of {len(mods)}")

                print(f"PROGRESS: {COMPLETED_COUNTER}/{len(mods)}")
                sys.stdout.flush()  # Ensure immediate output to GUI
            except Exception as e:
                incrementERROR_COUNTER_ThreadSafe()

                if logger:
                    logger.error(f"Error downloading file: {e}")

    if ledger:
        try:
            ledger.flush()
        except Exception:
            pass

    overall_end = time.time()
    final_message = f"Total Execution Time for download: {timedelta(seconds=(overall_end - overall_start))}. Aren't you glad you decided to download using this instead of Vortex?"

    print(final_message)
    if logger:
        logger.verbose(final_message)

def endorse_mods(mods, max_threads=10, logger=None, staging=None):
    # `mods` only ever contains Nexus files (load_mods_from_json drops entries
    # without a modId), so off-site mods are already excluded from endorsement.
    # If we have a ledger, skip mods already endorsed in a previous run and record
    # newly-endorsed ones with a timestamp.
    ledger = None
    if _LEDGER_AVAILABLE and staging:
        try:
            ledger = local_state.get_ledger(local_state.db_path_for(staging))
            already = ledger.endorsed_ids(GAME_DOMAIN)
            before = len(mods)
            mods = [(mid, fid) for (mid, fid) in mods if (mid, fid) not in already]
            if logger and before != len(mods):
                logger.verbose(f"Skipping {before - len(mods)} mod(s) already endorsed "
                               f"in a previous run.")
        except Exception as e:
            if logger:
                logger.warning(f"Endorsement ledger unavailable ({e}); endorsing all.")
            ledger = None

    if logger:
        logger.verbose(f"Starting endorsement for {len(mods)} mods with {max_threads} threads.")
    with concurrent.futures.ThreadPoolExecutor(max_workers=int(max_threads)) as executor:
        futures = {}
        for mod_id, file_id in mods:
            futures[executor.submit(endorse_mod, GAME_DOMAIN, mod_id, file_id)] = (mod_id, file_id)

        for future in concurrent.futures.as_completed(futures):
            mod_id, file_id = futures[future]
            try:
                result = future.result()
                if ledger and result:                 # only record genuine successes
                    ledger.mark_endorsed(mod_id, file_id)
            except Exception as e:
                if logger:
                    logger.error(f"Error endorsing mod file: {e}")
    if ledger:
        try:
            ledger.flush()
        except Exception:
            pass
    if logger:
        logger.verbose("Finished endorsing all mods.")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Parse JSON and download mods asynchronously")
    parser.add_argument('-f', '--gamefolder', help="The folder name where the downloads will be saved. This needs to match Vortex", required=True, default='', type=str)
    parser.add_argument('-j', '--json', help="Path to the JSON file containing mod data", required=True, default='', type=str)
    parser.add_argument('-t', '--maxthreads', help="The total number of active download threads you want, it's 1:1 for files",
                        required=False, default=10, type=int)
    parser.add_argument('-e', '--endorseonly', action='store_true', help="Endorse mods only without downloading them", default=False)
    parser.add_argument('-s', '--staging', help="Vortex mod staging dir; enables recording downloads into the local ledger (true-up + per-file)", required=False, default='', type=str)
    args = parser.parse_args()

    # Temporary logger for loading JSON to get game domain
    temp_logger = logging.getLogger("temp")
    mods = load_mods_from_json(args.json, temp_logger)
    # Setup logger with game domain and operation type
    operation_type = "endorse" if args.endorseonly else "download"
    logger = setup_logger(GAME_DOMAIN if GAME_DOMAIN else "unknown", operation_type)
    # Reload mods with logger for proper error logging
    mods = load_mods_from_json(args.json, logger)

    if args.endorseonly:
        logger.verbose("Endorsing mods only, no downloads will be performed.")
        endorse_mods(mods, args.maxthreads, logger, staging=(args.staging or None))
        exit(0)
    else:
        main(mods, args.gamefolder, args.maxthreads, logger,
             staging=(args.staging or None), collection_path=args.json)

    exit(0)