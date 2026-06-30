#!/usr/bin/env python3
"""Nuke the install back to a clean 'downloaded, waiting to install' baseline.

Keeps your DOWNLOADS (the expensive part) and the ledger's download/endorsement
history. Wipes the install: deletes the staging mod folders and resets the
ledger's install/deploy state. Optionally purges the deployed files from the
game's Data folder first (so no orphan hardlinks are left behind).

Run from the repo root with Vortex CLOSED:
    python reset_install.py                         # dry-run: show exactly what it will do
    python reset_install.py --apply                 # wipe staging mods + reset ledger
    python reset_install.py --apply --purge-deploy  # also un-deploy from the game folder first

PRESERVED: <staging>/<collection>/collection.json container, __vortex* markers,
the downloads folder (never touched), and the ledger's downloads + endorsements.
REMOVED:   every other staging mod folder, and the ledger's mods/mod_files/
           plugins/mod_rules/root_files rows.
"""
import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

if os.name == "nt":
    DEF_STAGING = r"L:\VortexMods\skyrimse"
    DEF_GAMEDATA = r"L:\SteamLibrary\steamapps\common\Skyrim Special Edition\Data"
else:
    DEF_STAGING = "/mnt/l/VortexMods/skyrimse"
    DEF_GAMEDATA = "/mnt/l/SteamLibrary/steamapps/common/Skyrim Special Edition/Data"


def _collection_container(staging: str):
    import glob
    for d in glob.glob(os.path.join(staging, "*", "collection.json")):
        return os.path.basename(os.path.dirname(d))
    return None


def _staging_mod_folders(staging: str, keep: set):
    """Mod folders eligible for deletion (excludes markers + collection container)."""
    out = []
    for d in sorted(os.listdir(staging)):
        full = os.path.join(staging, d)
        if not os.path.isdir(full):
            continue
        if d.startswith("__vortex") or d in keep:
            continue
        out.append(d)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--staging", default=DEF_STAGING)
    ap.add_argument("--game-data", default=DEF_GAMEDATA, help="the game's Data folder")
    ap.add_argument("--purge-deploy", action="store_true",
                    help="un-deploy from the game folder first (remove our hardlinks)")
    ap.add_argument("--keep", nargs="*", default=[],
                    help="extra staging folder names to preserve")
    ap.add_argument("--apply", action="store_true",
                    help="actually do it (default: dry-run)")
    args = ap.parse_args()

    if not os.path.isdir(args.staging):
        print(f"staging not found: {args.staging}")
        return 1

    from utils import local_state as ls

    container = _collection_container(args.staging)
    keep = set(args.keep) | ({container} if container else set())
    folders = _staging_mod_folders(args.staging, keep)

    db_path = ls.db_path_for(args.staging)
    ledger = ls.get_ledger(db_path) if os.path.exists(db_path) else None
    n_mods = len(ledger.all_mods()) if ledger else 0
    n_dls = 0
    if ledger:
        # count preserved downloads for reassurance
        n_dls = len(ledger.all_mods_with_download())  # rows with a mod; downloads kept regardless

    print(f"staging      : {args.staging}")
    print(f"collection   : {container or '(none found)'} (preserved)")
    print(f"ledger       : {db_path if ledger else '(no ledger)'}")
    print(f"mod folders  : {len(folders)} to delete")
    print(f"ledger mods  : {n_mods} install rows to reset (downloads PRESERVED)")
    if args.purge_deploy:
        print(f"purge-deploy : ON -> {args.game_data}")
    print("-" * 70)

    if not args.apply:
        print("DRY-RUN. Re-run with --apply to execute. Would:")
        if args.purge_deploy:
            print(f"  1. purge deployed files from {args.game_data}")
        print(f"  {'2' if args.purge_deploy else '1'}. delete {len(folders)} staging mod folder(s), e.g.:")
        for d in folders[:15]:
            print(f"        {d}")
        if len(folders) > 15:
            print(f"        ... and {len(folders) - 15} more")
        print(f"  {'3' if args.purge_deploy else '2'}. reset ledger install state (keep downloads + endorsements)")
        return 0

    # 1. Optional deploy purge (remove our hardlinks from the game Data folder).
    if args.purge_deploy:
        try:
            from utils import vortex_deploy as vd
            print(f"purging deployment from {args.game_data} ...")
            res = vd.purge(args.staging, args.game_data, force=True, workers=16)
            print(f"  removed {res.removed}, skipped {res.skipped}.")
        except Exception as e:
            print(f"  WARNING: deploy purge failed ({e}); continuing with staging wipe.")

    # 2. Delete staging mod folders (downloads live elsewhere and are untouched).
    deleted = 0
    for d in folders:
        full = os.path.join(args.staging, d)
        try:
            shutil.rmtree(full, ignore_errors=False)
            deleted += 1
        except Exception as e:
            print(f"  could not delete {d}: {e}")
    print(f"deleted {deleted}/{len(folders)} staging mod folders.")

    # 3. Reset the ledger to a clean 'downloaded, not installed' baseline.
    if ledger:
        ledger.reset_install_state()
        ledger.flush()
        print("ledger install state reset (downloads + endorsements preserved).")
    else:
        print("no ledger to reset.")

    print("\nDone. State is now 'downloaded, waiting to install'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
