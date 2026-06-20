#!/usr/bin/env python3
"""
Headless "the rest of the way" finalize: apply collection rules -> deploy
(hard-link staged mods into the game) -> sort plugins (load order). This is the
part you'd normally open Vortex to do.

It wraps ``utils.vortex_deploy.finalize_collection`` (order mods by
``collection.modRules`` -> deploy + manifest + mark DB deployed -> write
loadorder.txt/plugins.txt from ``collection.pluginRules``).

Modes (safe by default):
    python finalize_full.py                # PREVIEW: compute order + counts, write NOTHING
    python finalize_full.py --sort-only    # only (re)write plugins.txt/loadorder.txt (no deploy) -- low risk
    python finalize_full.py --apply        # full deploy (hard-links ~6k files) + sort   <-- Vortex must be CLOSED

Run AFTER install_missing.py + finalize_vortex.py --apply, so the new mods are
staged and DB-linked first.
"""

import argparse
import glob
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

if os.name == "nt":
    DEFAULT_STAGING = r"L:\VortexMods\skyrimse"
    DEFAULT_DATA = r"L:\SteamLibrary\steamapps\common\Skyrim Special Edition\Data"
    DEFAULT_LOCALAPPDATA = os.path.expandvars(r"%LOCALAPPDATA%\Skyrim Special Edition")
    DEFAULT_DB = ""
else:
    DEFAULT_STAGING = "/mnt/l/vortexmods/skyrimse"
    DEFAULT_DATA = "/mnt/l/SteamLibrary/steamapps/common/Skyrim Special Edition/Data"
    DEFAULT_LOCALAPPDATA = "/mnt/c/Users/cory/AppData/Local/Skyrim Special Edition"
    DEFAULT_DB = "/mnt/x/Nexus_Vortex/Appdata/Vortex/state.v2"

_REV_FOLDER = re.compile(r"-\d+-\d+-\d+$")


def find_collection_json(staging: str) -> str:
    matches = glob.glob(os.path.join(staging, "*", "collection.json"))
    if not matches:
        raise FileNotFoundError(f"No */collection.json under {staging}; pass --collection.")
    preferred = [m for m in matches if _REV_FOLDER.search(os.path.basename(os.path.dirname(m)))]
    return max(preferred or matches, key=os.path.getmtime)


def _read_current_active(localappdata_dir):
    """Current plugins.txt active entries (lowercased, no '*'), for diffing."""
    p = os.path.join(localappdata_dir, "plugins.txt")
    if not os.path.exists(p):
        return []
    out = []
    with open(p, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line.lstrip("*").lower())
    return out


def _bar(done, total, files, width=32):
    pct = (done / total) if total else 1.0
    filled = int(width * pct)
    return (f"\r  [{'#' * filled}{'-' * (width - filled)}] "
            f"{done}/{total} folders ({pct * 100:4.1f}%)  {files:,} files")


def _walk_with_progress(dep, staging, folders, workers):
    """Walk staged folders with a live progress bar (the silent part that felt hung)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results, total, done, files = {}, len(folders), 0, 0

    def draw():
        # redraw at most every 10 folders (plus the final one) to limit flicker
        if done % 10 == 0 or done == total:
            print(_bar(done, total, files), end="", flush=True)

    if workers and workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(dep._walk_one, staging, f): f for f in folders}
            for fut in as_completed(futs):
                res = fut.result()
                results[futs[fut]] = res
                done += 1
                files += len(res)
                draw()
    else:
        for f in folders:
            res = dep._walk_one(staging, f)
            results[f] = res
            done += 1
            files += len(res)
            draw()
    print()
    return [t for f in folders for t in results.get(f, [])]


def preview(collection, staging, data_dir, localappdata_dir, workers=1):
    from utils import vortex_deploy as dep
    from utils import vortex_loadorder as lo

    print("== DEPLOY (rule-ordered hard-link plan) ==")
    folders = dep.order_folders_for_deploy(staging, collection)
    print(f"  staged folders to deploy : {len(folders)}")
    inst = dep.read_instance_id(staging, data_dir)
    print(f"  vortex instance id        : {inst or '<none found — open Vortex once>'}")
    staged = _walk_with_progress(dep, staging, folders, workers)
    entries, links = dep.resolve_deployment(staged)
    print(f"  files in manifest         : {len(entries)}")
    print(f"  files to hard-link        : {len(links)}")
    print(f"  deploy order (first 3)    : {[os.path.basename(f) for f in folders[:3]]}")
    print(f"  deploy order (last 3)     : {[os.path.basename(f) for f in folders[-3:]]}")

    print("\n== PLUGIN SORT (load order plan) ==")
    scanned = lo.scan_plugins(data_dir)
    full, active = lo.compose_load_order(scanned, data_dir, lo.after_map(collection))
    cur = _read_current_active(localappdata_dir)
    new_set, cur_set = {p.lower() for p in active}, set(cur)
    print(f"  plugins scanned in Data   : {len(scanned)}")
    print(f"  loadorder.txt entries     : {len(full)}")
    print(f"  plugins.txt active entries: {len(active)}  (currently {len(cur)})")
    print(f"  would ADD to active       : {len(new_set - cur_set)}")
    print(f"  would REMOVE from active  : {len(cur_set - new_set)}")
    added = sorted(new_set - cur_set)
    for n in added[:10]:
        print(f"      + {n}")
    if len(added) > 10:
        print(f"      ... +{len(added) - 10} more")
    print("\n(preview only — nothing written. Use --sort-only or --apply to act.)")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--collection", help="path to collection.json (auto-detected if omitted)")
    p.add_argument("--staging", default=DEFAULT_STAGING)
    p.add_argument("--data", default=DEFAULT_DATA, help="game Data folder")
    p.add_argument("--localappdata", default=DEFAULT_LOCALAPPDATA, help="game %%LOCALAPPDATA%% dir (for plugins.txt)")
    p.add_argument("--db", default=DEFAULT_DB, help="Vortex state.v2 (for marking deployed)")
    p.add_argument("--sort-only", action="store_true", help="only write plugins.txt/loadorder.txt; no deploy")
    p.add_argument("--apply", action="store_true", help="full deploy (hard-links) + sort. Vortex MUST be closed.")
    p.add_argument("--no-backup", action="store_true", help="don't back up existing plugins.txt/loadorder.txt")
    p.add_argument("--workers", type=int, default=16,
                   help="threads for the file-walk + hard-linking (default 16; 1 = sequential)")
    args = p.parse_args(argv)

    import json
    collection_path = args.collection or find_collection_json(args.staging)
    with open(collection_path, "r", encoding="utf-8") as fh:
        collection = json.load(fh)

    for label, path in (("staging", args.staging), ("data", args.data),
                        ("localappdata", args.localappdata)):
        if not os.path.isdir(path):
            print(f"!! {label} dir not found: {path}")
            return 1

    print(f"collection   : {collection_path}")
    print(f"staging      : {args.staging}")
    print(f"game data    : {args.data}")
    print(f"localappdata : {args.localappdata}")
    mode = "APPLY (deploy + sort)" if args.apply else ("SORT-ONLY" if args.sort_only else "preview")
    print(f"mode         : {mode}")
    print("-" * 70)

    from utils import vortex_loadorder as lo

    if not args.apply and not args.sort_only:
        preview(collection, args.staging, args.data, args.localappdata, workers=args.workers)
        return 0

    if args.sort_only:
        lo_path, pl_path, active = lo.sort_plugins(
            collection, args.data, args.localappdata, backup=not args.no_backup)
        print(f"wrote loadorder.txt : {lo_path}")
        print(f"wrote plugins.txt   : {pl_path}  ({active} active plugins)")
        print("Done (plugins only — no deploy). Launch the game or open Vortex as you like.")
        return 0

    # full apply
    from utils import vortex_deploy as dep
    from utils import vortex_db
    from utils.vortex_db import VortexBusyError
    # Locate the DB: explicit --db, else OS default, else Windows auto-discovery.
    db = args.db or vortex_db.find_state_db()
    if not db or not os.path.exists(db):
        print(f"!! Vortex state.v2 not found (looked at: {db or '<auto-discovery>'}). "
              "Pass --db with the path to your state.v2 folder.")
        return 1
    try:
        res = dep.finalize_collection(
            db, collection, args.staging, args.data, args.localappdata,
            deployment_time_ms=int(time.time() * 1000), backup=not args.no_backup,
            workers=args.workers)
    except VortexBusyError as e:
        print(f"\n!! Vortex is running / DB locked: {e}\n   Close Vortex and re-run.")
        return 2
    except Exception as e:
        print(f"\n!! Failed: {type(e).__name__}: {e}")
        return 1

    print(f"deployed files     : {res.deploy.files} (hard-linked {res.deploy.linked})")
    print(f"manifest           : {res.deploy.manifest_path}")
    print(f"instance id        : {res.deploy.instance_id}")
    print(f"loadorder.txt      : {res.loadorder_path}")
    print(f"plugins.txt        : {res.plugins_path}  ({res.active_plugins} active)")
    print("-" * 70)
    print("Done. Game is deployed + sorted. Launch skse64_loader.exe and play.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
