#!/usr/bin/env python3
"""Bootstrap the local ledger from the current install (one careful pass).

This does, ONCE and carefully, the identity matching we were previously redoing
(badly) on every run -- then persists it so nothing downstream has to guess
again. It is DISK-DRIVEN, which is the key to getting it right:

  * The staging folder name IS the archive's masked stem (deriveModInstallName),
    so for each staging folder we can find its archive by an exact reverse match
    -- no folder guessing at all.
  * The archive name carries the modId; the collection supplies fileId/md5 for
    that file. We match the archive to its collection entry by modId + exact
    fileSize (the reliable key), which fills in the download record.
  * We then hash every staged file to lay down the integrity baseline.

Existing Vortex download ids (archiveIds) are reused when ``state.v2`` is
readable, so the ledger projects back to Vortex faithfully.

Usage:
    python reconcile_state.py                 # build/refresh the ledger
    python reconcile_state.py --no-hash       # skip the (slow) file hashing
    python reconcile_state.py --workers 16
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from utils import local_state as ls          # noqa: E402
from utils.vortex_sync import (               # noqa: E402  (reuse the validated helpers)
    index_by_modid, _gen_id, _match_archive,
)

if os.name == "nt":
    DEF_STAGING, DEF_DL = r"L:\VortexMods\skyrimse", r"L:\VortexDownloads\skyrimse"
else:
    DEF_STAGING, DEF_DL = "/mnt/l/VortexMods/skyrimse", "/mnt/l/VortexDownloads/skyrimse"

ARCHIVE_RE = re.compile(r"\.(7z|zip|rar)$", re.IGNORECASE)
_INVALID = re.compile(r'[<>:"/\\|?*]')


def _folder_for_archive(archive_name: str) -> str:
    """Our installer's folder name for an archive = stem with invalid chars
    masked (mirrors fomod_installer._get_vortex_folder_name, so a reconcile
    matches the folders we actually created)."""
    return _INVALID.sub("_", ARCHIVE_RE.sub("", archive_name))


def _walk_mod_files(folder_abs: str) -> List[Tuple[str, str, int, Optional[str], int, Optional[int]]]:
    """(name, rel_path, size, md5=None, mtime, created) for every file under a mod
    folder. md5 is filled later (optionally) so the walk stays cheap."""
    out = []
    for dirpath, _dirs, files in os.walk(folder_abs):
        for fn in files:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, folder_abs).replace(os.sep, "/")
            try:
                st = os.stat(full)
            except OSError:
                continue
            created = int(getattr(st, "st_birthtime", st.st_ctime))
            out.append((fn.lower(), rel, st.st_size, None, int(st.st_mtime), created))
    return out


def _read_vortex_download_ids(staging_dir: str) -> Dict[str, str]:
    """Map archive localPath -> existing Vortex archiveId, so our ledger ids line
    up with Vortex. Best-effort: empty if Vortex's DB can't be read."""
    try:
        from utils import vortex_db
        db = vortex_db.find_state_db() or os.environ.get("VORTEX_DB")
        if not db or not vortex_db.probe(db):
            return {}
        raw = vortex_db.read_prefix(db, "persistent###downloads###files###")
        return {v: k.split("###")[3] for k, v in raw.items() if k.endswith("###localPath")}
    except Exception:
        return {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--staging", default=DEF_STAGING)
    ap.add_argument("--downloads", default=DEF_DL)
    ap.add_argument("--collection", help="collection.json (auto-detected under staging)")
    ap.add_argument("--no-hash", action="store_true", help="skip per-file md5 hashing")
    ap.add_argument("--resume", action="store_true",
                    help="skip mods already verified with files (resume an interrupted run)")
    ap.add_argument("--workers", type=int, default=16)
    args = ap.parse_args(argv)

    coll_path = args.collection
    if not coll_path:
        import glob
        cands = glob.glob(os.path.join(args.staging, "*", "collection.json"))
        coll_path = max(cands, key=os.path.getmtime) if cands else None
    if not coll_path or not os.path.exists(coll_path):
        print("!! collection.json not found; pass --collection"); return 1
    collection = json.load(open(coll_path, encoding="utf-8"))
    info = collection.get("info", {})
    coll_name = info.get("name", os.path.basename(os.path.dirname(coll_path)))

    print(f"collection : {coll_path}")
    print(f"staging    : {args.staging}")
    print(f"downloads  : {args.downloads}")

    # --- index disk + collection ------------------------------------------- #
    all_archives = [n for n in os.listdir(args.downloads) if ARCHIVE_RE.search(n)]
    archive_sizes = {}
    for n in all_archives:
        try:
            archive_sizes[n] = os.path.getsize(os.path.join(args.downloads, n))
        except OSError:
            pass
    downloads_by_modid = index_by_modid(all_archives, archives_only=True)
    # archive stem(masked) -> archive name, for the folder->archive reverse match
    folder_to_archive = {_folder_for_archive(n): n for n in all_archives}

    mods_by_modid: Dict[str, List[dict]] = {}
    for m in collection.get("mods", []):
        s = m.get("source") or {}
        if s.get("type") == "nexus" and s.get("modId"):
            mods_by_modid.setdefault(str(s["modId"]), []).append(m)

    vortex_dl_ids = _read_vortex_download_ids(args.staging)
    if vortex_dl_ids:
        print(f"reused Vortex archiveIds: {len(vortex_dl_ids)}")

    staging_folders = [d for d in os.listdir(args.staging)
                       if os.path.isdir(os.path.join(args.staging, d)) and not d.startswith(".")]
    print(f"staging folders: {len(staging_folders)}   archives: {len(all_archives)}")
    print("-" * 64)

    # --- build the ledger --------------------------------------------------- #
    db_path = ls.db_path_for(args.staging)
    st = ls.LocalState(db_path)
    rev_id, rev_no = _parse_revision(os.path.basename(os.path.dirname(coll_path)))
    cid = _collection_id(collection)
    st.upsert_collection(cid or 0, info.get("domainName", "") or "", coll_name, rev_id, rev_no)

    already_done = set()
    if args.resume:
        already_done = {m["folder"] for m in st.all_mods()
                        if m["verified"] and (m["file_count"] or 0) > 0}
        if already_done:
            print(f"resume: skipping {len(already_done)} already-verified mods")

    written_downloads = set()
    plans: List[Tuple[str, str, Optional[dict]]] = []   # (folder, folder_abs, mod_data|None)
    matched = orphan = 0

    for folder in staging_folders:
        if folder in already_done:
            continue
        archive = folder_to_archive.get(folder)
        mod_data = None
        dl_id = None
        if archive:
            mid = _modid_of(archive)
            cands = mods_by_modid.get(mid or "", [])
            mod_data = _match_collection_mod(cands, archive, archive_sizes.get(archive))
            dl_id = _ensure_download(st, archive, args.downloads, mod_data, cid,
                                     vortex_dl_ids, archive_sizes, written_downloads)
            matched += 1
        else:
            orphan += 1
        st.upsert_mod(folder, dl_id, variant=coll_name,
                      installer_choices=(mod_data or {}).get("choices") if mod_data else None,
                      file_count=0, verified=False, state="installed")
        plans.append((folder, os.path.join(args.staging, folder), mod_data))

    st.flush()
    print(f"mods recorded: {len(plans)}  (matched to a download: {matched}, orphan: {orphan})")

    # --- per-file rows (+ optional hashing) --------------------------------- #
    t0 = time.time()
    total_files = [0]

    def do_folder(item):
        folder, folder_abs, _ = item
        rows = _walk_mod_files(folder_abs)
        if not args.no_hash:
            rows = [(n, rp, sz,
                     ls.hash_file(os.path.join(folder_abs, rp.replace("/", os.sep))),
                     mt, cr)
                    for (n, rp, sz, _md5, mt, cr) in rows]
        st.replace_mod_files(folder, rows)
        st.set_mod_verified(folder, verified=bool(rows), file_count=len(rows))
        total_files[0] += len(rows)
        return len(rows)

    print(f"walking + {'hashing ' if not args.no_hash else ''}files "
          f"({args.workers} workers)...")
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, _ in enumerate(ex.map(do_folder, plans), 1):
            if i % 500 == 0:
                print(f"   {i}/{len(plans)} mods, {total_files[0]:,} files, "
                      f"{time.time()-t0:.0f}s")
    st.flush()
    st.close()

    print("-" * 64)
    print(f"DONE in {time.time()-t0:.0f}s: {len(plans)} mods, {total_files[0]:,} files")
    print(f"ledger: {db_path}")
    print(f"inspect: python -m utils.local_state --db '{db_path}' --dump")
    return 0


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _modid_of(archive_name: str) -> Optional[str]:
    ids = index_by_modid([archive_name])
    return next(iter(ids), None)


def _match_collection_mod(cands: List[dict], archive: str,
                          archive_size: Optional[int]) -> Optional[dict]:
    """Pick the collection mod whose file is this archive: exact fileSize first,
    then name overlap (the same logic that disambiguates shared-modId files)."""
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    if archive_size:
        exact = [m for m in cands if (m.get("source") or {}).get("fileSize") == archive_size]
        if len(exact) == 1:
            return exact[0]
        if exact:
            cands = exact
    low = archive.lower()
    return max(cands, key=lambda m: sum(
        1 for w in ((m.get("source") or {}).get("logicalFilename") or m.get("name") or "").lower().split()
        if len(w) > 3 and w in low))


def _ensure_download(st, archive, downloads_dir, mod_data, collection_id,
                     vortex_ids, archive_sizes, written) -> str:
    s = (mod_data or {}).get("source") or {}
    dl_id = vortex_ids.get(archive)
    if not dl_id:
        if s.get("modId") and s.get("fileId"):
            dl_id = _gen_id(f"{s['modId']}-{s['fileId']}")
        else:
            dl_id = _gen_id(archive)
    if dl_id not in written:
        size = archive_sizes.get(archive) or 0
        st.upsert_download(
            dl_id, archive, s.get("modId"), s.get("fileId"), s.get("md5", "") or "",
            size, size, s.get("logicalFilename") or (mod_data or {}).get("name", "") or "",
            collection_id)
        written.add(dl_id)
    return dl_id


def _parse_revision(folder: str) -> Tuple[int, int]:
    m = re.search(r"-(\d+)-(\d+)-(\d+)$", folder)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _collection_id(collection: dict) -> Optional[int]:
    for m in collection.get("mods", []):
        cid = ((m.get("source") or {}).get("collectionId"))
        if cid:
            return int(cid)
    return None


if __name__ == "__main__":
    sys.exit(main())
