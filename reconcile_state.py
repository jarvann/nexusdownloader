#!/usr/bin/env python3
"""CLI wrapper: build the local ledger from the current install.

The logic lives in ``utils.state_reconcile.reconcile`` so Link can call the same
code to self-heal an empty ledger. See that module for the design notes.

Usage:
    python reconcile_state.py                 # build/refresh the ledger
    python reconcile_state.py --no-hash       # skip the (slow) file hashing
    python reconcile_state.py --resume        # skip already-verified mods
    python reconcile_state.py --workers 16
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from utils import local_state as ls          # noqa: E402
from utils import state_reconcile             # noqa: E402

if os.name == "nt":
    DEF_STAGING, DEF_DL = r"L:\VortexMods\skyrimse", r"L:\VortexDownloads\skyrimse"
else:
    DEF_STAGING, DEF_DL = "/mnt/l/VortexMods/skyrimse", "/mnt/l/VortexDownloads/skyrimse"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--staging", default=DEF_STAGING)
    ap.add_argument("--downloads", default=DEF_DL)
    ap.add_argument("--collection", help="collection.json (auto-detected under staging)")
    ap.add_argument("--no-hash", action="store_true", help="skip per-file md5 hashing")
    ap.add_argument("--resume", action="store_true", help="skip already-verified mods")
    ap.add_argument("--workers", type=int, default=16)
    args = ap.parse_args(argv)

    coll = args.collection or state_reconcile.find_collection_json(args.staging)
    if not coll or not os.path.exists(coll):
        print("!! collection.json not found; pass --collection"); return 1
    print(f"collection : {coll}")
    print(f"staging    : {args.staging}\ndownloads  : {args.downloads}")
    print("-" * 64)
    state_reconcile.reconcile(args.staging, args.downloads, coll,
                              do_hash=not args.no_hash, workers=args.workers,
                              resume=args.resume)
    db = ls.db_path_for(args.staging)
    print("-" * 64)
    print(f"ledger: {db}")
    print(f"inspect: python -m utils.local_state --db '{db}' --dump")
    return 0


if __name__ == "__main__":
    sys.exit(main())
