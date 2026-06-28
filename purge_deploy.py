#!/usr/bin/env python3
"""Un-deploy (purge) a collection's files from the game folder, like Vortex's
"Purge Mods", using our deployment manifest.

Run from the repo root with Vortex CLOSED:
    python purge_deploy.py                      # dry-run: show what would be removed
    python purge_deploy.py --apply              # purge everything (safe mode)
    python purge_deploy.py --apply --only "SomeMod-folder" "OtherMod-folder"
    python purge_deploy.py --apply --force      # remove manifest targets even if edited

SAFE by default: only files that are still OUR hardlink (same inode as staging)
are removed; anything you replaced by hand is left in place. --force overrides.
Paths auto-resolve from Vortex discovery; override with --staging/--game-data.
"""
import os
import sys
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

if os.name == "nt":
    DEF_STAGING = r"L:\VortexMods\skyrimse"
    DEF_GAMEDATA = r"L:\SteamLibrary\steamapps\common\Skyrim Special Edition\Data"
else:
    DEF_STAGING = "/mnt/l/VortexMods/skyrimse"
    DEF_GAMEDATA = "/mnt/l/SteamLibrary/steamapps/common/Skyrim Special Edition/Data"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--staging", default=DEF_STAGING)
    ap.add_argument("--game-data", default=DEF_GAMEDATA, help="the game's Data folder")
    ap.add_argument("--only", nargs="*", help="purge only these staging folders")
    ap.add_argument("--force", action="store_true",
                    help="remove manifest targets even if no longer our hardlink")
    ap.add_argument("--apply", action="store_true", help="actually remove (default: dry-run)")
    ap.add_argument("--no-db", action="store_true",
                    help="skip flagging needToDeploy in Vortex's DB")
    args = ap.parse_args()

    from utils import vortex_deploy as vd

    entries = vd.read_manifest_entries(args.game_data)
    only = set(args.only) if args.only else None
    targets = [e for e in entries.values()
               if only is None or e.get("source") in only]
    print(f"manifest:   {os.path.join(args.game_data, vd.MANIFEST_NAME)}")
    print(f"tracked:    {len(entries)} files")
    print(f"to purge:   {len(targets)}" + (f" (only {sorted(only)})" if only else ""))

    if not args.apply:
        print("\n(dry-run -- re-run with --apply to actually purge)")
        for e in targets[:20]:
            print(f"  would remove  {e['relPath']}  (<- {e.get('source')})")
        if len(targets) > 20:
            print(f"  ... and {len(targets) - 20} more")
        return 0

    def _prog(done, total, rel):
        print(f"  [{done}/{total}] {rel}")

    if args.no_db:
        res = vd.purge(args.staging, args.game_data, only_folders=only,
                       force=args.force, progress=_prog)
    else:
        db = None
        try:
            from utils import vortex_db
            db = vortex_db.find_state_db()
        except Exception as e:
            print(f"  (could not find Vortex DB: {e}; skipping DB flag)")
        if not db:
            res = vd.purge(args.staging, args.game_data, only_folders=only,
                           force=args.force, progress=_prog)
        else:
            res, _ = vd.purge_collection(db, args.staging, args.game_data,
                                         only_folders=only, force=args.force,
                                         progress=_prog)
    print(f"\nremoved {res.removed}, skipped {res.skipped} (kept user-modified), "
          f"{res.remaining} still tracked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
