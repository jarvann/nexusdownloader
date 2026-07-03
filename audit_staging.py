#!/usr/bin/env python3
"""
Audit the mod staging folder for contamination after install:
  * ORPHANS   -- folders that match no mod in the collection (would deploy as
                 extra/wrong content)
  * EMPTY     -- folders with no files (failed/partial install the skip-check may
                 have passed as "installed")
  * DUPLICATES-- folders with identical content fingerprint (file count + total
                 bytes), the signature of the shared-modId wrong-match bug

Read-only by default. Opt in to pruning with --delete-orphans / --delete-empty.
Duplicate detection (--deep) stats every file, so it's slower over a network mount.

Usage:
    python audit_staging.py                 # report orphans + empty (fast)
    python audit_staging.py --deep          # also find duplicate-content folders
    python audit_staging.py --delete-empty  # remove empty folders
    python audit_staging.py --delete-orphans --deep
"""

import argparse
import json
import os
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

if os.name == "nt":
    DEFAULT_STAGING = r"L:\VortexMods\skyrimse"
else:
    DEFAULT_STAGING = "/mnt/l/VortexMods/skyrimse"

_MODID_RE = re.compile(r"-(\d{2,7})-")


def find_collection_json(staging):
    matches = []
    for d in os.listdir(staging):
        p = os.path.join(staging, d, "collection.json")
        if os.path.isfile(p):
            matches.append(p)
    return max(matches, key=os.path.getmtime) if matches else None


def folder_fingerprint(path):
    """(file_count, total_bytes) -- cheap content signature for dupe detection."""
    n, total = 0, 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            n += 1
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return n, total


def has_any_file(path):
    for _root, _dirs, files in os.walk(path):
        if files:
            return True
    return False


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--staging", default=DEFAULT_STAGING)
    p.add_argument("--collection", help="collection.json (auto-detected if omitted)")
    p.add_argument("--deep", action="store_true", help="also detect duplicate-content folders (slower)")
    p.add_argument("--delete-empty", action="store_true", help="remove empty folders")
    p.add_argument("--delete-orphans", action="store_true", help="remove orphan folders")
    args = p.parse_args(argv)

    coll = args.collection or find_collection_json(args.staging)
    if not coll or not os.path.exists(coll):
        print("!! collection.json not found; pass --collection.")
        return 1
    c = json.load(open(coll, encoding="utf-8"))
    coll_folder = os.path.basename(os.path.dirname(coll))

    # Expected identity sets from the collection.
    expected_modids = set()
    expected_names = set()
    for m in c.get("mods", []):
        s = m.get("source", {})
        if s.get("modId"):
            expected_modids.add(str(s["modId"]))
        for key in (m.get("name"), s.get("logicalFilename")):
            if key:
                expected_names.add(key.lower())

    folders = [d for d in os.listdir(args.staging)
               if os.path.isdir(os.path.join(args.staging, d))
               and not d.startswith("__vortex") and d != coll_folder]

    print(f"collection : {coll}")
    print(f"staging    : {args.staging}")
    print(f"folders    : {len(folders)}  (excluding the collection container + markers)")
    print("-" * 70)

    orphans, empty = [], []
    for d in folders:
        path = os.path.join(args.staging, d)
        mid = _MODID_RE.search(d)
        matched = (mid and mid.group(1) in expected_modids) or \
                  any(nm in d.lower() for nm in expected_names if len(nm) > 4)
        if not matched:
            orphans.append(d)
        if not has_any_file(path):
            empty.append(d)

    print(f"orphan folders (no matching collection mod): {len(orphans)}")
    for d in orphans[:30]:
        print(f"   - {d}")
    if len(orphans) > 30:
        print(f"   ... +{len(orphans) - 30} more")
    print(f"empty folders (failed/partial install)     : {len(empty)}")
    for d in empty[:30]:
        print(f"   - {d}")

    dupes = {}
    if args.deep:
        print("-" * 70)
        print("scanning content fingerprints (--deep)...")
        by_fp = defaultdict(list)
        for i, d in enumerate(folders, 1):
            fp = folder_fingerprint(os.path.join(args.staging, d))
            if fp[0] > 0:
                by_fp[fp].append(d)
            if i % 200 == 0:
                print(f"\r   {i}/{len(folders)} folders", end="", flush=True)
        print()
        dupes = {fp: ds for fp, ds in by_fp.items() if len(ds) > 1}
        print(f"duplicate-content groups (same file count + bytes): {len(dupes)}")
        for (n, total), ds in sorted(dupes.items(), key=lambda kv: -kv[0][0])[:15]:
            print(f"   [{n} files, {total/1e6:.0f} MB] x{len(ds)}: {', '.join(ds[:3])}"
                  + (" ..." if len(ds) > 3 else ""))

    # Optional pruning
    removed = 0
    if args.delete_empty:
        for d in empty:
            try:
                shutil.rmtree(os.path.join(args.staging, d)); removed += 1
            except OSError as e:
                print(f"   could not remove {d}: {e}")
    if args.delete_orphans:
        for d in orphans:
            try:
                shutil.rmtree(os.path.join(args.staging, d)); removed += 1
            except OSError as e:
                print(f"   could not remove {d}: {e}")
    print("-" * 70)
    if removed:
        print(f"removed {removed} folder(s).")
    elif orphans or empty or dupes:
        print("Report only. Re-run with --delete-empty / --delete-orphans to prune "
              "(review the lists above first).")
    else:
        print("Staging looks clean -- no orphans, empties, or duplicates.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
