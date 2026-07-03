#!/usr/bin/env python3
"""Verify a collection install: does what's on disk match what the collection
intended? Ledger-first identity, collection.json as the source of truth.

Run from the repo root on the machine that owns the install (Windows), Vortex
may stay open (read-only):

    python verify_installation.py                       # archive + staging + plugins + cheap content
    python verify_installation.py --md5                 # also checksum every archive (slow)
    python verify_installation.py --deep                # extract + simulate FOMOD, diff vs disk
                                                         #   (auto-scoped to mods already flagged)
    python verify_installation.py --deep --deep-all     # deep-check EVERY mod (very slow, re-extracts all)
    python verify_installation.py --deep --only "Some Folder" "Other Folder"
    python verify_installation.py --json report.json    # machine-readable output

Four components:
  ARCHIVE  -- each download matches the collection's expected size (and --md5).
  STAGING  -- every collection mod has a non-empty folder (ledger-matched); flags
              true orphans, manual mods, and file-count drift.
  PLUGINS  -- plugin files on disk the collection never declared (the wrong-FOMOD-
              choice symptom), with the phantom masters they pull in.
  CONTENT  -- cheap: recorded FOMOD choices vs intended (a hint, not a clean bill).
              deep: resolve collection intent -> expected files -> diff vs disk.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from utils import install_verify as iv  # noqa: E402

if os.name == "nt":
    DEF_STAGING = r"L:\VortexMods\skyrimse"
    DEF_DOWNLOADS = r"L:\VortexDownloads\skyrimse"
else:
    DEF_STAGING = "/mnt/l/VortexMods/skyrimse"
    DEF_DOWNLOADS = "/mnt/l/vortexdownloads/skyrimse"

_ORDER = {iv.ERROR: 0, iv.WARN: 1, iv.INFO: 2}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--staging", default=DEF_STAGING)
    ap.add_argument("--downloads", default=DEF_DOWNLOADS)
    ap.add_argument("--collection", default="", help="path to collection.json (auto-detected if omitted)")
    ap.add_argument("--md5", action="store_true", help="checksum archives (slow)")
    ap.add_argument("--deep", action="store_true", help="extract + simulate FOMOD, diff vs disk")
    ap.add_argument("--deep-all", action="store_true",
                    help="with --deep, check EVERY mod (default: only flagged ones)")
    ap.add_argument("--only", nargs="*", default=None, help="deep-check only these folders")
    ap.add_argument("--deep-limit", type=int, default=None, help="cap deep checks to N mods")
    ap.add_argument("--temp", default=None, help="scratch dir for deep extraction")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--json", default="", help="write the full report to this JSON file")
    args = ap.parse_args(argv)

    def _progress(i, total, folder):
        print(f"\r  deep {i}/{total}: {folder[:60]:<60}", end="", flush=True)

    deep_only = args.only
    if args.deep and args.deep_all:
        deep_only = []  # empty list != None -> 'check all' (verify() treats None as auto-scope)

    report = iv.verify(
        args.staging, downloads=args.downloads, collection_path=args.collection,
        check_md5=args.md5, deep=args.deep, deep_only=deep_only,
        deep_limit=args.deep_limit, temp_root=args.temp, workers=args.workers,
        progress=_progress if args.deep else None,
    )
    if args.deep:
        print()  # close the progress line

    st = report.stats
    print(f"collection : {st.get('collection', '?')}")
    print(f"mods       : {st.get('collection_mods', '?')} in collection, "
          f"{st.get('ledger_mods', '?')} in ledger")
    print(f"deep       : {st.get('deep', False)}")
    print("-" * 78)

    counts = {s: len(report.of(s)) for s in (iv.ERROR, iv.WARN, iv.INFO)}
    for comp, title in ((iv.ARCHIVE, "ARCHIVE"), (iv.STAGING, "STAGING"),
                        (iv.CONTENT, "PLUGINS / CONTENT")):
        items = sorted(report.by_component(comp), key=lambda f: (_ORDER[f.severity], f.folder))
        if not items:
            continue
        print(f"\n== {title} ({len(items)}) ==")
        for f in items:
            tag = {iv.ERROR: "ERR ", iv.WARN: "WARN", iv.INFO: "INFO"}[f.severity]
            print(f"  [{tag}] {f.folder}: {f.detail}")

    print("\n" + "-" * 78)
    print(f"ERRORS {counts[iv.ERROR]}   WARN {counts[iv.WARN]}   INFO {counts[iv.INFO]}   "
          f"=> {'OK' if report.ok else 'PROBLEMS FOUND'}")
    if not args.deep and (counts[iv.ERROR] or counts[iv.WARN]):
        print("Tip: re-run with --deep to confirm content of the flagged mods "
              "(resolve collection intent vs files on disk).")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, indent=2)
        print(f"Wrote {args.json}")

    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
