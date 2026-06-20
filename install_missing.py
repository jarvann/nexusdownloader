#!/usr/bin/env python3
"""
Headless installer for the mods in a collection that aren't staged yet.

Mirrors what the GUI's install tab does (parallel FOMOD install with a pre-scan
that SKIPS already-installed mods), but runs entirely from the CLI so the whole
finish flow can stay in WSL. It does NOT download -- archives must already be in
the downloads folder. Manual/external-source mods are reported as skipped.

The scratch/temp dir defaults to the Linux ext4 disk (roomy), so the ramdisk
%TEMP% disk-full failures that killed these installs on Windows can't recur.

Usage (WSL):
    python install_missing.py            # install whatever's missing, 4 workers
    python install_missing.py --workers 6
    python install_missing.py --sequential        # one at a time (easier to debug)

After it finishes, re-run:  python finalize_vortex.py --apply   (then Deploy in Vortex)
"""

import argparse
import glob
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

if os.name == "nt":
    DEFAULT_STAGING = r"L:\VortexMods\skyrimse"
    DEFAULT_DOWNLOADS = r"L:\VortexDownloads\skyrimse"
    DEFAULT_TEMP = ""                       # use the system temp on Windows
else:
    DEFAULT_STAGING = "/mnt/l/vortexmods/skyrimse"
    DEFAULT_DOWNLOADS = "/mnt/l/vortexdownloads/skyrimse"
    DEFAULT_TEMP = "/tmp/nxd_install"        # ext4, roomy -- not the ramdisk

_REV_FOLDER = re.compile(r"-\d+-\d+-\d+$")


def find_collection_json(staging: str) -> str:
    matches = glob.glob(os.path.join(staging, "*", "collection.json"))
    if not matches:
        raise FileNotFoundError(
            f"No */collection.json under {staging}; pass --collection.")
    preferred = [m for m in matches if _REV_FOLDER.search(os.path.basename(os.path.dirname(m)))]
    return max(preferred or matches, key=os.path.getmtime)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--collection", help="path to collection.json (auto-detected if omitted)")
    p.add_argument("--downloads", default=DEFAULT_DOWNLOADS)
    p.add_argument("--staging", default=DEFAULT_STAGING)
    p.add_argument("--temp", default=DEFAULT_TEMP, help=f"scratch dir for extraction (default {DEFAULT_TEMP or '<system>'})")
    p.add_argument("--workers", type=int, default=4, help="parallel install workers (default 4)")
    p.add_argument("--sequential", action="store_true", help="install one mod at a time")
    args = p.parse_args(argv)

    import json
    from utils.fomod_installer import (
        create_fomod_installer, create_parallel_fomod_installer, InstallResult)
    from utils.archive_handler import get_archive_handler

    collection = args.collection or find_collection_json(args.staging)
    with open(collection, "r", encoding="utf-8") as fh:
        collection_data = json.load(fh)
    total = len(collection_data.get("mods", []))

    if args.temp:
        os.makedirs(args.temp, exist_ok=True)

    # Surface which extractors got picked up (helps diagnose .rar/.7z issues).
    try:
        get_archive_handler()
    except Exception:
        pass

    print(f"collection : {collection}")
    print(f"downloads  : {args.downloads}")
    print(f"staging    : {args.staging}")
    print(f"temp       : {args.temp or '<system>'}")
    print(f"mode       : {'sequential' if args.sequential else f'parallel x{args.workers}'}")
    print(f"mods total : {total} (already-installed + manual are skipped automatically)")
    print("-" * 70)

    done = {"n": 0}

    def on_progress(current, total_, name):
        done["n"] = current

    def on_install(name, success, message):
        tag = "OK  " if success else "FAIL"
        line = f"[{tag}] {name}" + (f" -- {message}" if message and not success else "")
        print(line, flush=True)

    if args.sequential:
        cm = create_fomod_installer(args.staging, None, args.temp or None)
    else:
        cm = create_parallel_fomod_installer(
            args.staging, None, args.workers,
            {"installation_timeout_seconds": 600}, args.temp or None)

    with cm as installer:
        if not args.sequential:
            installer.set_progress_callback(on_progress)
            installer.set_installation_callback(on_install)
            results = installer.install_collection_parallel(collection_data, args.downloads)
        else:
            results = installer.install_collection(collection_data, args.downloads)

    ok = [r for r in results if r.status == InstallResult.SUCCESS]
    failed = [r for r in results if r.status == InstallResult.FAILED]
    skipped = [r for r in results if r.status == InstallResult.SKIPPED]

    print("-" * 70)
    print(f"installed (new): {len(ok)}")
    print(f"skipped        : {len(skipped)}  (already-installed + manual)")
    print(f"FAILED         : {len(failed)}")

    if failed:
        with open("INSTALL_FAILURES.txt", "w", encoding="utf-8") as fh:
            for r in failed:
                fh.write(f"{r.mod_name}\t{r.error_message or ''}\n")
        print("  -> failures written to INSTALL_FAILURES.txt")
        for r in failed[:15]:
            print(f"     - {r.mod_name}: {r.error_message or ''}")

    print("-" * 70)
    if not failed:
        print("All available mods installed. Next: python finalize_vortex.py --apply  (then Deploy in Vortex)")
    else:
        print("Some failed (see file). You can re-run this script -- it skips what's already done.")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
