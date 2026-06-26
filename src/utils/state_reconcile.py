"""Build the local ledger from the current install (one careful, disk-driven pass).

This is the importable core behind ``reconcile_state.py`` (CLI) and is also
called by :func:`utils.vortex_sync.run` to self-heal an empty ledger before a
Link. It performs, ONCE, the identity matching we used to redo (badly) every
run, then persists it so nothing downstream guesses again.

Disk-driven, which is what makes it correct:
  * a staging folder's name IS the archive's masked stem, so each folder is
    matched to its archive by an exact reverse -- no folder guessing;
  * the archive's name carries the modId; the collection supplies fileId/md5 for
    that file; we match archive -> collection entry by modId + exact fileSize.
Existing Vortex archiveIds are reused when ``state.v2`` is readable so the ledger
projects back to Vortex faithfully.
"""

from __future__ import annotations

import glob
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Tuple

from utils import local_state as ls
from utils.vortex_sync import index_by_modid, _gen_id

ARCHIVE_RE = re.compile(r"\.(7z|zip|rar)$", re.IGNORECASE)
_INVALID = re.compile(r'[<>:"/\\|?*]')


def derive_install_folder(archive_name: str) -> str:
    """Our installer's staging-folder name for an archive: stem with FS-invalid
    chars masked. Single source of truth -- mirror this in the installer so a
    reconcile matches the folders we actually created."""
    return _INVALID.sub("_", ARCHIVE_RE.sub("", archive_name))


def _walk_mod_files(folder_abs: str, do_hash: bool
                    ) -> List[Tuple[str, str, int, Optional[str], int, Optional[int]]]:
    """(name, rel_path, size, md5, mtime, created) for every file under a mod."""
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
            md5 = ls.hash_file(full) if do_hash else None
            out.append((fn.lower(), rel, st.st_size, md5, int(st.st_mtime), created))
    return out


def _read_vortex_download_ids() -> Dict[str, str]:
    """archive localPath -> existing Vortex archiveId (best-effort; {} if no DB)."""
    try:
        from utils import vortex_db
        db = vortex_db.find_state_db() or os.environ.get("VORTEX_DB")
        if not db or not vortex_db.probe(db):
            return {}
        raw = vortex_db.read_prefix(db, "persistent###downloads###files###")
        return {v: k.split("###")[3] for k, v in raw.items() if k.endswith("###localPath")}
    except Exception:
        return {}


def _modid_of(archive_name: str) -> Optional[str]:
    ids = index_by_modid([archive_name])
    return next(iter(ids), None)


def _match_collection_mod(cands: List[dict], archive: str,
                          archive_size: Optional[int]) -> Optional[dict]:
    """The collection entry whose file is this archive: exact fileSize first, then
    name overlap (same logic that disambiguates shared-modId files)."""
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


def _ensure_download(st: "ls.LocalState", archive: str, mod_data: Optional[dict],
                     collection_id: Optional[int], vortex_ids: Dict[str, str],
                     archive_sizes: Dict[str, int], written: set,
                     existing_ids: Dict[str, str]) -> str:
    s = (mod_data or {}).get("source") or {}
    # Stable id: a previously-recorded id for this file wins (so re-runs don't flip
    # it and trip the local_path UNIQUE), then Vortex's archiveId, then a
    # deterministic gen from modId-fileId.
    dl_id = existing_ids.get(archive) or vortex_ids.get(archive)
    if not dl_id:
        dl_id = (_gen_id(f"{s['modId']}-{s['fileId']}")
                 if s.get("modId") and s.get("fileId") else _gen_id(archive))
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
        cid = (m.get("source") or {}).get("collectionId")
        if cid:
            return int(cid)
    return None


def find_collection_json(staging_dir: str) -> Optional[str]:
    cands = glob.glob(os.path.join(staging_dir, "*", "collection.json"))
    return max(cands, key=os.path.getmtime) if cands else None


def reconcile_mods(staging_dir: str, downloads_dir: str, collection_path: str, *,
                   log: Callable[[str], None] = print) -> Dict[str, int]:
    """FAST pass: refresh the download + mod identity from disk (no file walk, no
    hashing). This is all Link needs, so :func:`utils.vortex_sync.run` calls it on
    every Link to keep the ledger current after new installs -- cheap enough to
    run unconditionally (listdir + matching, no per-file stat)."""
    collection = json.load(open(collection_path, encoding="utf-8"))
    info = collection.get("info", {})
    coll_name = info.get("name", os.path.basename(os.path.dirname(collection_path)))

    all_archives = [n for n in os.listdir(downloads_dir) if ARCHIVE_RE.search(n)]
    archive_sizes = {}
    for n in all_archives:
        try:
            archive_sizes[n] = os.path.getsize(os.path.join(downloads_dir, n))
        except OSError:
            pass
    folder_to_archive = {derive_install_folder(n): n for n in all_archives}

    mods_by_modid: Dict[str, List[dict]] = {}
    for m in collection.get("mods", []):
        s = m.get("source") or {}
        if s.get("type") == "nexus" and s.get("modId"):
            mods_by_modid.setdefault(str(s["modId"]), []).append(m)

    vortex_dl_ids = _read_vortex_download_ids()
    staging_folders = [d for d in os.listdir(staging_dir)
                       if os.path.isdir(os.path.join(staging_dir, d)) and not d.startswith(".")]
    log(f"staging folders: {len(staging_folders)}  archives: {len(all_archives)}  "
        f"reused Vortex ids: {len(vortex_dl_ids)}")

    st = ls.LocalState(ls.db_path_for(staging_dir))
    try:
        rev_id, rev_no = _parse_revision(os.path.basename(os.path.dirname(collection_path)))
        cid = _collection_id(collection)
        st.upsert_collection(cid or 0, info.get("domainName", "") or "", coll_name, rev_id, rev_no)
        existing_ids = st.download_ids_by_path()   # keep ids stable across re-runs
        written, matched, orphan = set(), 0, 0
        for folder in staging_folders:
            archive = folder_to_archive.get(folder)
            mod_data = dl_id = None
            if archive:
                cands = mods_by_modid.get(_modid_of(archive) or "", [])
                mod_data = _match_collection_mod(cands, archive, archive_sizes.get(archive))
                dl_id = _ensure_download(st, archive, mod_data, cid, vortex_dl_ids,
                                         archive_sizes, written, existing_ids)
                matched += 1
            else:
                orphan += 1
            st.upsert_mod(folder, dl_id, variant=coll_name,
                          installer_choices=(mod_data or {}).get("choices"),
                          state="installed")
        st.flush()
        log(f"mods recorded: {len(staging_folders)} (matched: {matched}, orphan: {orphan})")
        return {"mods": len(staging_folders), "matched": matched, "orphan": orphan}
    finally:
        st.close()


def reconcile_files(staging_dir: str, *, do_hash: bool = True, workers: int = 16,
                    resume: bool = False, log: Callable[[str], None] = print) -> Dict[str, int]:
    """SLOW pass: walk (and optionally md5-hash) every staged file to lay/refresh
    the integrity baseline. Operates on the mods already recorded by
    :func:`reconcile_mods`."""
    st = ls.LocalState(ls.db_path_for(staging_dir))
    try:
        folders = [m["folder"] for m in st.all_mods()
                   if not (resume and m["verified"] and (m["file_count"] or 0) > 0)]
        log(f"{'hashing' if do_hash else 'walking'} files for {len(folders)} mods "
            f"({workers} workers)...")
        t0, total = time.time(), [0]

        def do_folder(folder):
            folder_abs = os.path.join(staging_dir, folder)
            rows = _walk_mod_files(folder_abs, do_hash)
            st.replace_mod_files(folder, rows)
            st.set_mod_verified(folder, verified=bool(rows), file_count=len(rows))
            total[0] += len(rows)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for i, _ in enumerate(ex.map(do_folder, folders), 1):
                if i % 500 == 0:
                    log(f"   {i}/{len(folders)} mods, {total[0]:,} files, {time.time()-t0:.0f}s")
        st.flush()
        log(f"done: {len(folders)} mods, {total[0]:,} files in {time.time()-t0:.0f}s")
        return {"mods": len(folders), "files": total[0]}
    finally:
        st.close()


def reconcile(staging_dir: str, downloads_dir: str, collection_path: str, *,
              do_hash: bool = True, workers: int = 16, resume: bool = False,
              log: Callable[[str], None] = print) -> Dict[str, int]:
    """Full bootstrap: identity refresh (fast) + integrity baseline (slow)."""
    m = reconcile_mods(staging_dir, downloads_dir, collection_path, log=log)
    f = reconcile_files(staging_dir, do_hash=do_hash, workers=workers, resume=resume, log=log)
    return {**m, **f}
