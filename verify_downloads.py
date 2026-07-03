#!/usr/bin/env python3
"""
Verify downloaded collection archives against the collection's expected sizes /
MD5s, and (optionally) delete the bad ones so a resumed download re-fetches only
those.

Why: the downloader skips files that already EXIST by name -- it does not
checksum them. So if the app is killed mid-download (e.g. 45 threads in flight),
up to ~45 truncated files are left at their final names and a resume wrongly
treats them as complete. This pass catches them.

Modes:
  * fast (default): compare each archive's byte size to the collection's
    source.fileSize. Catches every truncated/partial file in seconds.
  * thorough (--md5, or config downloads.verify_md5 = true): verify the actual
    MD5. Catches silent corruption too; reads every file, so it's slower.

Usage (Vortex closed not required -- this only touches the downloads folder):
    python verify_downloads.py                 # report only (dry-run)
    python verify_downloads.py --delete        # delete suspect files
    python verify_downloads.py --md5 --delete  # thorough + delete
"""

import argparse
import glob
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

if os.name == "nt":
    DEFAULT_DOWNLOADS = r"L:\VortexDownloads\skyrimse"
    DEFAULT_STAGING = r"L:\VortexMods\skyrimse"
else:
    DEFAULT_DOWNLOADS = "/mnt/l/vortexdownloads/skyrimse"
    DEFAULT_STAGING = "/mnt/l/VortexMods/skyrimse"

_MODID_RE = re.compile(r"-(\d{2,7})-")
_ARCHIVE_RE = re.compile(r"\.(7z|zip|rar)$", re.IGNORECASE)
_REV_FOLDER = re.compile(r"-\d+-\d+-\d+$")


def _modid_of(name: str):
    m = _MODID_RE.search(name)
    return m.group(1) if m else None


def find_collection_json(staging: str, downloads: str):
    for base in (staging, os.path.dirname(downloads)):
        for m in glob.glob(os.path.join(base, "*", "collection.json")):
            return m
    # also accept one sitting next to downloads
    direct = glob.glob(os.path.join(downloads, "collection.json"))
    return direct[0] if direct else None


def _config_md5_default() -> bool:
    try:
        cfg = json.load(open(Path(__file__).parent / "src" / "config.json", encoding="utf-8"))
        return bool((cfg.get("downloads") or {}).get("verify_md5", False))
    except Exception:
        return False


def _md5(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--downloads", default=DEFAULT_DOWNLOADS)
    p.add_argument("--staging", default=DEFAULT_STAGING)
    p.add_argument("--collection", help="collection.json (auto-detected if omitted)")
    p.add_argument("--md5", action="store_true",
                   help="thorough MD5 verify (default from config downloads.verify_md5)")
    p.add_argument("--delete", action="store_true",
                   help="delete suspect files so a resumed download re-fetches them")
    p.add_argument("--workers", type=int, default=8, help="threads for MD5 hashing")
    args = p.parse_args(argv)

    use_md5 = args.md5 or _config_md5_default()

    collection = args.collection or find_collection_json(args.staging, args.downloads)
    if not collection or not os.path.exists(collection):
        print("!! collection.json not found; pass --collection.")
        return 1
    with open(collection, "r", encoding="utf-8") as fh:
        cdata = json.load(fh)

    # modId -> set of expected sizes / md5s (a modId may have several files/variants)
    sizes_by_mod = defaultdict(set)
    md5s_by_mod = defaultdict(set)
    for mod in cdata.get("mods", []):
        s = mod.get("source", {})
        mid = s.get("modId")
        if mid is None:
            continue
        if s.get("fileSize"):
            sizes_by_mod[str(mid)].add(int(s["fileSize"]))
        if s.get("md5"):
            md5s_by_mod[str(mid)].add(s["md5"].lower())

    files = [f for f in os.listdir(args.downloads)
             if _ARCHIVE_RE.search(f) and os.path.isfile(os.path.join(args.downloads, f))]

    print(f"collection : {collection}")
    print(f"downloads  : {args.downloads}")
    print(f"mode       : {'MD5 (thorough)' if use_md5 else 'size (fast)'}")
    print(f"archives   : {len(files)}")
    print("-" * 70)

    valid, suspect, unverifiable = [], [], []

    def check(fn):
        path = os.path.join(args.downloads, fn)
        mid = _modid_of(fn)
        if mid is None or (mid not in sizes_by_mod and mid not in md5s_by_mod):
            return ("unverifiable", fn, "no collection entry (manual/off-site?)")
        if use_md5:
            expected = md5s_by_mod.get(mid)
            if not expected:
                # fall back to size if the collection lacks an md5 for this mod
                return _size_check(path, fn, sizes_by_mod.get(mid))
            actual = _md5(path)
            return ("valid", fn, "") if actual in expected else \
                   ("suspect", fn, f"md5 {actual[:8]}… not expected")
        return _size_check(path, fn, sizes_by_mod.get(mid))

    def _size_check(path, fn, expected_sizes):
        if not expected_sizes:
            return ("unverifiable", fn, "no expected size in collection")
        actual = os.path.getsize(path)
        return ("valid", fn, "") if actual in expected_sizes else \
               ("suspect", fn, f"size {actual:,} != expected {sorted(expected_sizes)}")

    if use_md5:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            results = list(ex.map(check, files))
    else:
        results = [check(f) for f in files]

    for kind, fn, why in results:
        (valid if kind == "valid" else suspect if kind == "suspect" else unverifiable).append((fn, why))

    print(f"valid        : {len(valid)}")
    print(f"unverifiable : {len(unverifiable)}  (manual/off-site mods or no expected size)")
    print(f"SUSPECT      : {len(suspect)}  (truncated/corrupt — re-download these)")
    for fn, why in suspect[:40]:
        print(f"   - {fn}  [{why}]")
    if len(suspect) > 40:
        print(f"   ... +{len(suspect) - 40} more")
    print("-" * 70)

    if suspect and args.delete:
        removed = 0
        for fn, _ in suspect:
            try:
                os.remove(os.path.join(args.downloads, fn))
                removed += 1
            except OSError as e:
                print(f"   could not delete {fn}: {e}")
        print(f"DELETED {removed} suspect file(s). Restart the download to re-fetch them.")
    elif suspect:
        print("Dry-run: re-run with --delete to remove the suspect files, then resume the download.")
    else:
        print("All archives verify clean. Safe to resume / install.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
