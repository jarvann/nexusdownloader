import json
import argparse
import concurrent.futures
from endorse import endorse_mod  # Importing the endorse function from endorse.py
from download import download_file  # Importing the download function from download.py
import threading
import time
from datetime import timedelta

lock = threading.Lock()
COUNTER = 0
LICENSE_KEY = ""
NEXUS_API_KEY = ""
GAME_DOMAIN = ""


def increment():
    global COUNTER
    with lock:  # Automatically acquires and releases the lock
        COUNTER += 1

    return COUNTER

# Function to load mods from a JSON file
def load_mods_from_json(file_path):
    global GAME_DOMAIN 

    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    GAME_DOMAIN= data["info"]["domainName"]
    mods = []
    for entry in data['mods']:
        try:
            mod_id = entry['source']['modId']
            file_id = entry['source']['fileId']
            mods.append((mod_id, file_id))
        except KeyError as e:
            print(f"Skipping entry due to missing key: {e}")
    return mods


# Main function to execute concurrent downloads
def main(mods, gamefolder, max_threads=10):
    overall_start = time.time()

    # Load mods from the provided JSON file


    # Using ThreadPoolExecutor to download files asynchronously, max 10 threads
    with concurrent.futures.ThreadPoolExecutor(max_workers=int(max_threads)) as executor:
        futures = []
        for mod_id, file_id in mods:
            current_counter = increment()
            futures.append(executor.submit(download_file, GAME_DOMAIN, gamefolder, mod_id, file_id, current_counter))

        # Wait for all futures to complete
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()  # If the download fails, it will raise an exception here
            except Exception as e:
                print(f"0000\tError downloading file: {e}")

    overall_end = time.time()

    print(
        f"Total Execution Time for download: {timedelta(seconds=(overall_end - overall_start))}. Aren't you glad you decided to download using this instead of Vortex?")

def endorse_mods(mods, max_threads=10):
    with concurrent.futures.ThreadPoolExecutor(max_workers=int(max_threads)) as executor:
        futures = []
        for mod_id, file_id in mods:
            futures.append(executor.submit(endorse_mod, GAME_DOMAIN, mod_id, file_id))

        # Wait for all futures to complete
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()  # If the download fails, it will raise an exception here
            except Exception as e:
                print(f"Error endorsing mod file: {e}")
    
    print("Finished endorsing all mods.")

if __name__ == '__main__':
    # Set up argument parsing
    parser = argparse.ArgumentParser(description="Parse JSON and download mods asynchronously")
    parser.add_argument('-f', '--gamefolder', help="The folder name where the downloads will be saved. This needs to match Vortex", required=True, default='', type=str)
    parser.add_argument('-j', '--json', help="Path to the JSON file containing mod data", required=True, default='', type=str)
    parser.add_argument('-t', '--maxthreads', help="The total number of active download threads you want, it's 1:1 for files",
                        required=False, default=10, type=int)
    parser.add_argument('-e', '--endorseonly', action='store_true', help="Endorse mods only without downloading them", default=False)
    args = parser.parse_args()

    mods = load_mods_from_json(args.json) 
    print(f"Loaded {len(mods)} mods from the JSON file.")
    
    if args.endorseonly:
        print("Endorsing mods only, no downloads will be performed.")
        endorse_mods(mods, args.maxthreads)
        exit(0)
    else:
        # Run the main function with the provided JSON file
        main(mods, args.gamefolder, args.maxthreads)

        print("Finished processing all mods. Do you want to endorse them? (y/n)")
        endorse = input().strip().lower()

        if endorse == 'y':
            endorse_mods(mods, args.maxthreads) 
                      
    exit(0)  # Exit the script after processing
    # This ensures that the script exits cleanly after all operations are done.