#!/usr/bin/env python3
"""Dump duplicate / unmatched mods straight from the ledger.

Run from the repo root on the machine that owns the ledger (Windows):
    python find_dups.py
    python find_dups.py --staging "L:\\VortexMods\\skyrimse"

Reports three things the collection screen can't tell apart:
  1. DUPLICATE DOWNLOAD CLAIM  - one download_id claimed by >1 mod folder
  2. SAME (modId,fileId)        - same Nexus file behind >1 mod folder
  3. NO IDENTITY               - mod row with null modId/fileId (would show
                                 "Unspecified" in Vortex)
"""
import os
import sys
import argparse
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from utils import local_state as ls  # noqa: E402

if os.name == "nt":
    DEF_STAGING = r"L:\VortexMods\skyrimse"
else:
    DEF_STAGING = "/mnt/l/VortexMods/skyrimse"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--staging", default=DEF_STAGING)
    args = ap.parse_args()

    db = ls.db_path_for(args.staging)
    print(f"ledger: {db}")
    if not os.path.exists(db):
        print("  (no ledger at that path)")
        return 1

    st = ls.get_ledger(db)
    rows = st.all_mods_with_download()
    print(f"mods in ledger: {len(rows)}\n")

    by_dlid = defaultdict(list)
    by_modfile = defaultdict(list)
    no_identity = []
    for r in rows:
        folder = r.get("folder")
        dlid = r.get("download_id")
        mid = r.get("dl_mod_id")
        fid = r.get("dl_file_id")
        if dlid:
            by_dlid[dlid].append(folder)
        if mid and fid:
            by_modfile[(mid, fid)].append(folder)
        else:
            no_identity.append((folder, r.get("dl_local_path")))

    dup_dl = {k: v for k, v in by_dlid.items() if len(v) > 1}
    dup_mf = {k: v for k, v in by_modfile.items() if len(v) > 1}

    print(f"== 1. DUPLICATE DOWNLOAD CLAIM ({len(dup_dl)}) ==")
    for dlid, folders in sorted(dup_dl.items()):
        print(f"  download {dlid}:")
        for f in folders:
            print(f"      {f}")

    print(f"\n== 2. SAME (modId,fileId) ACROSS FOLDERS ({len(dup_mf)}) ==")
    for (mid, fid), folders in sorted(dup_mf.items()):
        print(f"  mod {mid} file {fid}:")
        for f in folders:
            print(f"      {f}")

    print(f"\n== 3. NO IDENTITY (would show Unspecified) ({len(no_identity)}) ==")
    for folder, path in sorted(no_identity):
        print(f"  {folder}   <- {path or '(no download)'}")

    print(f"\nsummary: {len(dup_dl)} dup-claims, {len(dup_mf)} dup-files, "
          f"{len(no_identity)} no-identity")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
