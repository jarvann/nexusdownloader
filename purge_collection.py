#!/usr/bin/env python3
"""
Purge a collection's stale mod records from Vortex's state.v2 for a clean re-Link.

Earlier buggy runs (and Vortex's own "duplicate entries when re-installing updated
mods" bug) leave behind stale/duplicate mod records -- the source of the
"potential duplicate mods" warning and lingering conflicts. This removes EVERY mod
record + the active profile's modState for the game, while PRESERVING the
collection mod record itself (so re-running "Link to Vortex" still finds the
collection's id/slug and rebuilds every member + bare record cleanly).

Vortex MUST be fully closed. state.v2 is backed up automatically before any change.

Usage:
    python purge_collection.py              # DRY RUN -- report what would be removed
    python purge_collection.py --apply      # remove (after backup), then re-run Link
    python purge_collection.py --apply --game skyrimse
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from utils import vortex_db


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--game", default="skyrimse")
    ap.add_argument("--apply", action="store_true",
                    help="actually remove records (default: dry run report only)")
    args = ap.parse_args(argv)

    db = vortex_db.find_state_db()
    if not db:
        print("!! Could not find Vortex's state.v2 database.")
        return 1
    print(f"db      : {db}")

    try:
        vortex_db.ensure_available(db)
    except vortex_db.VortexBusyError as e:
        print(f"!! {e}")
        return 2

    game = args.game
    profile = vortex_db.read_active_profile(db)

    # Every mod record's type, so we can preserve the collection record(s).
    types = vortex_db.read_prefix(db, f"persistent###mods###{game}###", suffix="###type")
    coll_folder_prefixes = []
    for key, mtype in types.items():
        folder_prefix = key.rsplit("###", 1)[0]        # drop trailing "###type"
        if mtype == "collection":
            coll_folder_prefixes.append(folder_prefix)
    mod_count = len(types)

    state = (vortex_db.read_prefix(db, f"persistent###profiles###{profile}###modState###",
                                   suffix="###enabled") if profile else {})

    print(f"game    : {game}")
    print(f"profile : {profile}")
    print(f"mod records       : {mod_count}  "
          f"({len(coll_folder_prefixes)} collection record(s) PRESERVED)")
    print(f"modState entries  : {len(state)}  (removed; re-Link rewrites them)")
    print(f"=> would remove {mod_count - len(coll_folder_prefixes)} mod records "
          f"+ {len(state)} modState entries")

    if not args.apply:
        print("\nDRY RUN. Re-run with --apply to remove (state.v2 is backed up first),")
        print("then re-run Link to Vortex to rebuild a clean record set.")
        return 0

    # Read the collection record(s) so we can re-put them in the SAME batch the
    # wipe runs in (the bridge applies deletes before puts, so they survive).
    preserve = {}
    for cf in coll_folder_prefixes:
        for k, v in vortex_db.read_prefix(db, cf + "###").items():
            preserve[k] = json.dumps(v)

    del_prefixes = [f"persistent###mods###{game}"]
    if profile:
        del_prefixes.append(f"persistent###profiles###{profile}###modState")

    res = vortex_db.write_records(db, preserve, delete_prefixes=del_prefixes, backup=True)
    print(f"\nDone. Preserved {len(preserve)} collection keys; backup: {res.backup_path}")
    print("Now re-run Link to Vortex (Vortex closed) to rebuild clean records.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
