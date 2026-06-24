#!/usr/bin/env python3
"""Remove the files a mis-targeted deploy hard-linked into Oblivion's Data folder.

The deploy was pointed at the wrong game (Oblivion instead of Skyrim Special
Edition) and laid ~294K hard links into Oblivion\\Data before failing. Those are
hard links, so deleting them only drops the directory entries -- the staging
copies (and the actual data) are untouched. We drive deletion off the deploy's
own manifest (vortex.deployment.json) so we ONLY remove files this deploy
created and never touch a genuine Oblivion file (its assets live in BSAs).

Usage:
    python clean_oblivion_misdeploy.py            # DRY RUN: counts only
    python clean_oblivion_misdeploy.py --apply    # actually delete
"""
import argparse
import json
import os
import sys

DATA = r"L:\SteamLibrary\steamapps\common\Oblivion\Data" if os.name == "nt" \
    else "/mnt/l/SteamLibrary/steamapps/common/Oblivion/Data"
MANIFEST = "vortex.deployment.json"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=DATA)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--workers", type=int, default=32)
    args = ap.parse_args(argv)

    man_path = os.path.join(args.data, MANIFEST)
    if not os.path.exists(man_path):
        print(f"!! no manifest at {man_path}; nothing to do safely."); return 1
    print(f"reading manifest: {man_path} ({os.path.getsize(man_path)/1e6:.0f} MB)")
    with open(man_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    files = manifest.get("files", [])
    print(f"manifest lists {len(files):,} deployed files")

    from concurrent.futures import ThreadPoolExecutor
    from threading import Lock

    rels = [(e.get("relPath") or "").replace("\\", os.sep) for e in files]
    rels = [r for r in rels if r]
    touched_dirs = {os.path.dirname(os.path.join(args.data, r)) for r in rels}

    if not args.apply:
        # Sample-check existence on a small slice (full serial lexists over 9p is
        # too slow to be worth it); the manifest count is the real figure.
        sample = rels[::max(1, len(rels) // 500)]
        present_sample = sum(1 for r in sample
                             if os.path.lexists(os.path.join(args.data, r)))
        print(f"sampled {len(sample)} entries -> {present_sample} present "
              f"({100*present_sample/max(1,len(sample)):.0f}%)")
        print(f"\nDRY RUN. Re-run with --apply to delete the ~{len(rels):,} "
              f"deployed files + manifest (parallel removal).")
        return 0

    # Parallel removal: each unlink is a 9p round-trip, so concurrency wins big.
    removed, missing, errors = [0], [0], [0]
    lk = Lock()

    def _rm(rel):
        full = os.path.join(args.data, rel)
        try:
            os.remove(full)
            with lk: removed[0] += 1
        except FileNotFoundError:
            with lk: missing[0] += 1
        except PermissionError:
            try:
                os.chmod(full, 0o666); os.remove(full)
                with lk: removed[0] += 1
            except OSError:
                with lk: errors[0] += 1
        except OSError:
            with lk: errors[0] += 1

    print(f"removing {len(rels):,} files with {args.workers} workers...")
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, _ in enumerate(ex.map(_rm, rels), 1):
            if i % 25000 == 0:
                print(f"   {i:,}/{len(rels):,} processed "
                      f"(removed {removed[0]:,}, gone {missing[0]:,}, err {errors[0]:,})")

    removed, missing, errors = removed[0], missing[0], errors[0]
    print(f"\nremoved               : {removed:,}")
    print(f"already gone          : {missing:,}")
    print(f"errors                : {errors:,}")
    # prune now-empty directories (deepest first) so Oblivion\Data isn't left
    # with hundreds of empty Skyrim folders. Only prunes empties -- never deletes
    # a dir that still holds a genuine Oblivion file.
    pruned = 0
    for d in sorted(touched_dirs, key=len, reverse=True):
        p = d
        while os.path.commonpath([p, args.data]) == os.path.abspath(args.data) \
                and os.path.abspath(p) != os.path.abspath(args.data):
            try:
                os.rmdir(p); pruned += 1
            except OSError:
                break
            p = os.path.dirname(p)
    print(f"pruned empty dirs     : {pruned:,}")

    try:
        os.remove(man_path)
        print(f"removed manifest      : {man_path}")
    except OSError as ex:
        print(f"could not remove manifest: {ex}")
    print("\nDone. Oblivion's Data folder is cleaned; staging is untouched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
