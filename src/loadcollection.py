import json
import argparse
import concurrent.futures
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
def main(json_file, max_threads=10):
    overall_start = time.time()

    # Load mods from the provided JSON file
    mods = load_mods_from_json(json_file)
    print(f"Loaded {len(mods)} mods from the JSON file.")

    # Using ThreadPoolExecutor to download files asynchronously, max 10 threads
    with concurrent.futures.ThreadPoolExecutor(max_workers=int(max_threads)) as executor:
        futures = []
        for mod_id, file_id in mods:
            current_counter = increment()
            futures.append(executor.submit(download_file, GAME_DOMAIN, mod_id, file_id, current_counter))

        # Wait for all futures to complete
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()  # If the download fails, it will raise an exception here
            except Exception as e:
                print(f"0000\tError downloading file: {e}")

    overall_end = time.time()

    print(
        f"Total Execution Time for download: {timedelta(seconds=(overall_end - overall_start))}. Aren't you glad you decided to download using this instead of Vortex?")


if __name__ == '__main__':
    # Set up argument parsing
    parser = argparse.ArgumentParser(description="Parse JSON and download mods asynchronously")
    parser.add_argument('json_file', help="Path to the JSON file containing mod data")
    parser.add_argument('max_threads', help="The total number of active download threads you want, it's 1:1 for files",
                        default=10)
    args = parser.parse_args()

    # Run the main function with the provided JSON file
    main(args.json_file, args.max_threads)
