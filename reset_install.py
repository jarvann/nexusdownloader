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

The actual work lives in utils.maintenance (shared with the GUI's Reset button).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

if os.name == "nt":
    DEF_STAGING = r"L:\VortexMods\skyrimse"
    DEF_GAMEDATA = r"L:\SteamLibrary\steamapps\common\Skyrim Special Edition\Data"
else:
    DEF_STAGING = "/mnt/l/VortexMods/skyrimse"
    DEF_GAMEDATA = "/mnt/l/SteamLibrary/steamapps/common/Skyrim Special Edition/Data"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--staging", default=DEF_STAGING)
    ap.add_argument("--game-data", default=DEF_GAMEDATA, help="the game's Data folder")
    ap.add_argument("--purge-deploy", action="store_true",
                    help="un-deploy from the game folder first (remove our hardlinks)")
    ap.add_argument("--keep", nargs="*", default=[],
                    help="extra staging folder names to preserve")
    ap.add_argument("--apply", action="store_true", help="actually do it (default: dry-run)")
    args = ap.parse_args()

    if not os.path.isdir(args.staging):
        print(f"staging not found: {args.staging}")
        return 1

    from utils import maintenance as mnt

    plan = mnt.plan_reset(args.staging, game_data=args.game_data,
                          purge_deploy=args.purge_deploy, extra_keep=args.keep)

    print(f"staging      : {plan.staging}")
    print(f"collection   : {plan.container or '(none found)'} (preserved)")
    print(f"ledger       : {plan.db_path if os.path.exists(plan.db_path) else '(no ledger)'}")
    print(f"mod folders  : {len(plan.folders)} to delete")
    print(f"ledger mods  : {plan.ledger_mods} install rows to reset (downloads PRESERVED)")
    if plan.purge_deploy:
        print(f"purge-deploy : ON -> {plan.game_data}")
    print("-" * 70)

    if not args.apply:
        print("DRY-RUN. Re-run with --apply to execute. Would delete e.g.:")
        for d in plan.folders[:15]:
            print(f"    {d}")
        if len(plan.folders) > 15:
            print(f"    ... and {len(plan.folders) - 15} more")
        return 0

    res = mnt.run_reset(plan, log=print,
                        progress=lambda d, t, n: None)
    print(f"\nDone: purged {res.purged}, deleted {res.deleted} folders, "
          f"ledger reset={res.ledger_reset}. State is now 'downloaded, waiting to install'.")
    for err in res.errors:
        print(f"  ! {err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
