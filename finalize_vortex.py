#!/usr/bin/env python3
"""
Finalize a downloaded+installed collection into Vortex's database so Vortex
recognizes it as installed/enabled and linked to the collection revision.

This is the "make Vortex think it did the work" step, run on its own. It does
NOT download or install -- it only writes the Vortex state.v2 records for mods
that are already staged on disk.

Usage (run on Windows, with Vortex CLOSED):

    # 1) See what it WOULD do -- safe, writes nothing:
    python finalize_vortex.py

    # 2) Actually write it (backs the DB up first):
    python finalize_vortex.py --apply

Paths default to the standard Skyrim SE Vortex layout but can be overridden.
"""

import argparse
import glob
import os
import re
import sys
from pathlib import Path

# Put src/ on the path the same way run_gui.py does, so `from utils import ...`
sys.path.insert(0, str(Path(__file__).parent / "src"))

# Known identity for the Skyrim SE test collection, used only as a fallback if
# Vortex's DB can't tell us the collection id/slug itself.
FALLBACK_COLLECTION_ID = 26945
FALLBACK_SLUG = "gnfjwh"

# OS-aware defaults: Windows uses drive letters; WSL/Linux uses the /mnt mounts.
# On Windows the DB is auto-discovered (%APPDATA%) so DEFAULT_DB stays "".
if os.name == "nt":
    DEFAULT_STAGING = r"L:\VortexMods\skyrimse"
    DEFAULT_DOWNLOADS = r"L:\VortexDownloads\skyrimse"
    DEFAULT_DB = ""
else:
    DEFAULT_STAGING = "/mnt/l/vortexmods/skyrimse"
    DEFAULT_DOWNLOADS = "/mnt/l/vortexdownloads/skyrimse"
    DEFAULT_DB = "/mnt/x/Nexus_Vortex/Appdata/Vortex/state.v2"

_REV_FOLDER = re.compile(r"-\d+-\d+-\d+$")


def find_collection_json(staging: str) -> str:
    """Find the collection.json inside the staging dir's collection folder.

    Prefers a folder whose name ends in the Nexus ``-<rev>-<num>-<ts>`` pattern;
    falls back to the most-recently-modified collection.json otherwise.
    """
    matches = glob.glob(os.path.join(staging, "*", "collection.json"))
    if not matches:
        raise FileNotFoundError(
            f"No */collection.json found under {staging}. "
            "Pass --collection with the full path.")
    preferred = [m for m in matches if _REV_FOLDER.search(os.path.basename(os.path.dirname(m)))]
    pool = preferred or matches
    return max(pool, key=os.path.getmtime)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--collection", help="path to collection.json (auto-detected if omitted)")
    parser.add_argument("--downloads", default=DEFAULT_DOWNLOADS, help=f"downloads dir (default {DEFAULT_DOWNLOADS})")
    parser.add_argument("--staging", default=DEFAULT_STAGING, help=f"staging/mods dir (default {DEFAULT_STAGING})")
    parser.add_argument("--db", default=DEFAULT_DB,
                        help="path to Vortex's state.v2 (auto-discovered on Windows; "
                             f"default on this OS: {DEFAULT_DB or '<auto>'})")
    parser.add_argument("--profile", help="Vortex profile id (auto-detected if omitted)")
    parser.add_argument("--apply", action="store_true", help="actually write to Vortex's DB (otherwise dry-run)")
    parser.add_argument("--force", action="store_true", help="write even on schema drift / validation warnings")
    args = parser.parse_args(argv)

    from utils import vortex_sync, vortex_db
    from utils.vortex_db import VortexBusyError

    collection = args.collection or find_collection_json(args.staging)

    # Locate the DB: explicit --db, else OS default, else Windows auto-discovery.
    db_path = args.db or vortex_db.find_state_db()
    if not db_path or not os.path.exists(db_path):
        print(f"!! Vortex state.v2 not found (looked at: {db_path or '<auto-discovery>'}).")
        print("   Pass --db with the path to your Vortex state.v2 folder.")
        return 1

    print(f"collection : {collection}")
    print(f"downloads  : {args.downloads}")
    print(f"staging    : {args.staging}")
    print(f"db         : {db_path}")
    print(f"mode       : {'APPLY (will write, backup first)' if args.apply else 'dry-run (no changes)'}")
    print("-" * 70)

    try:
        # Profile: explicit, else read the active one from the DB.
        profile_id = args.profile or vortex_db.read_active_profile(db_path)
        if not profile_id:
            print("!! Could not determine the active Vortex profile. Pass --profile.")
            return 1

        # Identity: ask the DB, fall back to the known Skyrim SE collection.
        try:
            identity = vortex_db.read_collection_identity(db_path)
        except Exception:
            identity = None
        if identity:
            collection_id, slug = identity
        else:
            print(f"note: Vortex's DB didn't yield the collection id/slug; "
                  f"using known identity (id={FALLBACK_COLLECTION_ID}, slug={FALLBACK_SLUG}).")
            collection_id, slug = FALLBACK_COLLECTION_ID, FALLBACK_SLUG

        res = vortex_sync.run(
            db_path, collection, args.downloads, args.staging, profile_id,
            collection_id=collection_id, slug=slug,
            apply=args.apply, force=args.force)
    except VortexBusyError as e:
        print(f"\n!! Vortex is running / the DB is locked: {e}")
        print("   Close Vortex completely (check Task Manager for Vortex.exe) and re-run.")
        return 2
    except Exception as e:
        print(f"\n!! Failed: {type(e).__name__}: {e}")
        return 1

    p = res.plan
    print(f"drift            : {res.risk.level} - {res.risk.message}")
    print(f"mods linked      : {p.mod_count}")
    print(f"new downloads    : {p.new_downloads}")
    print(f"collection rules : {p.rule_count}")
    print(f"orphan records   : {p.orphan_count}  (non-collection staging folders, given bare records)")
    print(f"profile enables  : {p.profile_enables}")
    print(f"skipped          : manual={p.skipped_manual}, not-on-disk={p.skipped_no_disk}")
    print(f"total db keys     : {p.total_keys}")
    print(f"schema violations: {len(p.violations)}")
    for v in p.violations[:10]:
        print(f"   - {v}")
    print("-" * 70)
    if res.applied:
        print(f"APPLIED: wrote {res.keys_written} keys "
              f"(replaced {res.replaced_collections} old revision(s)).")
        print(f"Backup of the DB before write: {res.backup_path}")
        print("Open Vortex -> it should now show the collection installed/enabled. "
              "Deploy + sort from there as needed.")
    else:
        print(res.message)
        if not args.apply:
            print("This was a dry-run. Re-run with --apply to write it (Vortex must be closed).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
