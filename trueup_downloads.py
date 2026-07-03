#!/usr/bin/env python3
"""
True up Vortex's download records so the "Some downloads were not finalized" repair
prompt stops firing.

Vortex runs checkForUnfinalized() on EVERY game activation
(download_management/index.ts:1210). It flags a download as unfinalized when:

    (state == "finalizing")  OR  (state == "finished" AND fileMD5 is absent)
    -- and it has a localPath.

...then nags to "Repair" (which hashes every flagged archive, or deletes the
record if the file is gone). Our current writers always emit fileMD5, so the
offenders are STALE records from earlier runs / the collection .7z from an old
link / records Vortex created itself -- a re-link doesn't touch those.

This sweeps ALL download records and fixes the offenders in one atomic, backed-up
write: forces state -> "finished" and sets fileMD5 (the REAL md5 from
collection.json when the record's modId/fileId match, else "" -- empty still
satisfies Vortex's `=== undefined` test, so the prompt stops). Vortex must be
CLOSED.

Usage:
    python trueup_downloads.py                       # DRY RUN report
    python trueup_downloads.py --apply               # fix in place (backs up first)
    python trueup_downloads.py --apply --collection L:\\VortexMods\\skyrimse\\DOMAIN-...\\collection.json
    python trueup_downloads.py --apply --hash        # compute real md5 from disk for unmatched files (slow)
"""

import argparse
import glob
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from utils import vortex_db

PREFIX = "persistent###downloads###files###"


def _by_id(data):
    """{download_id: value} from a read_prefix result keyed by full ### keys."""
    out = {}
    for k, v in data.items():
        parts = k.split("###")
        if len(parts) > 3:
            out[parts[3]] = v        # persistent/downloads/files/<id>/...
    return out


def _load_collection_md5(path):
    """{(modId, fileId): md5} from collection.json, for setting real hashes."""
    out = {}
    if not path or not os.path.exists(path):
        return out
    try:
        c = json.load(open(path, encoding="utf-8"))
    except Exception:
        return out
    for m in c.get("mods", []):
        s = m.get("source") or {}
        if s.get("md5") and s.get("modId") is not None and s.get("fileId") is not None:
            out[(str(s["modId"]), str(s["fileId"]))] = s["md5"]
    return out


def _auto_collection():
    """Newest collection.json in the active game's staging (best-effort)."""
    try:
        db = vortex_db.find_state_db()
        for g in (vortex_db.read_vortex_games(db) if db else []):
            staging = g.get("staging")
            if staging and os.path.isdir(staging):
                cjs = glob.glob(os.path.join(staging, "*", "collection.json"))
                if cjs:
                    return max(cjs, key=os.path.getmtime)
    except Exception:
        pass
    return None


def _file_md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--collection", help="collection.json for real md5s (auto-detected if omitted)")
    ap.add_argument("--downloads", help="downloads folder (for --hash; read from Vortex if omitted)")
    ap.add_argument("--hash", action="store_true",
                    help="compute real md5 from disk for records not matched in collection.json (slow)")
    ap.add_argument("--apply", action="store_true", help="fix in place (default: dry run)")
    args = ap.parse_args(argv)

    db = vortex_db.find_state_db()
    if not db:
        print("!! Could not find Vortex's state.v2 database.")
        return 1
    print(f"db : {db}")
    try:
        vortex_db.ensure_available(db)
    except vortex_db.VortexBusyError as e:
        print(f"!! {e}")
        return 2

    states = _by_id(vortex_db.read_prefix(db, PREFIX, suffix="###state"))
    md5s = _by_id(vortex_db.read_prefix(db, PREFIX, suffix="###fileMD5"))
    locals_ = _by_id(vortex_db.read_prefix(db, PREFIX, suffix="###localPath"))
    modids = _by_id(vortex_db.read_prefix(db, PREFIX, suffix="###modInfo###nexus###ids###modId"))
    fileids = _by_id(vortex_db.read_prefix(db, PREFIX, suffix="###modInfo###nexus###ids###fileId"))

    coll = args.collection or _auto_collection()
    coll_md5 = _load_collection_md5(coll)
    print(f"collection: {coll or '(none -- unmatched records get empty md5)'}  "
          f"[{len(coll_md5)} md5s]")

    finalizing, no_md5 = [], []
    for did, st in states.items():
        if not locals_.get(did):
            continue   # no localPath -> Vortex silently removes it; leave alone
        if st == "finalizing":
            finalizing.append(did)
        elif st == "finished" and did not in md5s:
            no_md5.append(did)

    def real_md5_for(did):
        return coll_md5.get((str(modids.get(did)), str(fileids.get(did))), "")

    matched = sum(1 for d in no_md5 if real_md5_for(d))
    total = len(finalizing) + len(no_md5)
    print(f"download records       : {len(states)}")
    print(f"  state 'finalizing'   : {len(finalizing)}  -> set state finished")
    print(f"  finished, no fileMD5 : {len(no_md5)}  ({matched} get a real md5 from collection.json)")
    print(f"=> would fix {total} records")

    if total == 0:
        print("\nNothing to fix -- no download record triggers the repair prompt.")
        return 0
    if not args.apply:
        print("\nDRY RUN. Re-run with --apply to fix (state.v2 backed up first).")
        return 0

    dl_dir = args.downloads
    if args.hash and not dl_dir:
        try:
            for g in vortex_db.read_vortex_games(db):
                if g.get("downloads"):
                    dl_dir = g["downloads"]
                    break
        except Exception:
            dl_dir = None

    records = {}
    hashed = 0
    for did in set(finalizing) | set(no_md5):
        base = f"{PREFIX}{did}"
        if did in finalizing:
            records[f"{base}###state"] = json.dumps("finished")
        if did not in md5s:
            md5 = real_md5_for(did)
            if not md5 and args.hash and dl_dir and locals_.get(did):
                fp = os.path.join(dl_dir, locals_[did])
                if os.path.isfile(fp):
                    try:
                        md5 = _file_md5(fp)
                        hashed += 1
                    except OSError:
                        md5 = ""
            records[f"{base}###fileMD5"] = json.dumps(md5)

    res = vortex_db.write_records(db, records, backup=True)
    print(f"\nFixed {total} records ({res.keys_written} keys written"
          + (f", {hashed} hashed from disk" if hashed else "") + ").")
    print(f"backup: {res.backup_path}")
    print("Re-open Vortex -- the 'downloads not finalized' prompt should be gone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
