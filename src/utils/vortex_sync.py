"""
Vortex sync orchestrator: turn a downloaded + staged collection into the Vortex
state.v2 records that make it show as installed/enabled and linked to the
collection at the correct revision.

Design:
* :func:`build_plan` is **pure** -- given the collection and on-disk facts it
  builds every record (downloads + installed mods + profile enables + the
  collection mod / rules / revision manifest) and validates each against
  :mod:`utils.vortex_schema`. No I/O, so it's fully unit-testable.
* :func:`run` does the I/O: read existing DB state, assess version/schema drift,
  verify Vortex isn't holding the lock, then write atomically (with a backup)
  via :mod:`utils.vortex_db`. It refuses to write on schema drift or schema
  violations unless explicitly forced.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from utils import vortex_records as vr
from utils.vortex_schema import (
    GAME_ID, validate_record, assess_risk, RiskAssessment, RECORD_SCHEMAS,
)

ARCHIVE_RE = re.compile(r"\.(7z|zip|rar)$", re.IGNORECASE)
_MODID_RE = re.compile(r"-(\d{2,7})-")


def _best_match(candidates: List[str], mod: Dict[str, Any]) -> Optional[str]:
    """Pick the staging folder / archive that belongs to *this* mod when several
    share a modId (the DOMAIN patches, LODGen/TexGen, any multi-file mod).

    Without this, ``candidates[0]`` is chosen for every mod sharing a modId, so
    only one variant links and the rest show 'Not Installed' in Vortex. Match by
    word overlap between the candidate's name and the mod's logicalFilename/name
    -- the same signal the installer uses to lay the folders down.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    s = mod.get("source", {})
    target = (s.get("logicalFilename") or mod.get("name") or "").lower()
    words = [w for w in re.split(r"[^a-z0-9]+", target) if len(w) > 3]
    if not words:
        return candidates[0]

    def score(cand: str) -> int:
        n = os.path.basename(cand).lower()
        return sum(1 for w in words if w in n)

    best = max(candidates, key=score)
    return best if score(best) > 0 else candidates[0]


def _match_archive(candidates: List[str], mod: Dict[str, Any],
                   sizes: Dict[str, int]) -> Optional[str]:
    """Pick the archive that belongs to *this* mod, preferring an EXACT fileSize
    match over name overlap.

    Name overlap (``_best_match``) ties when one mod's name is a prefix of
    another's -- e.g. "Convenient Horses" scores equally against both
    ``Convenient Horses.zip`` and ``Convenient Horses ... Patch.zip`` -- so
    ``max()`` hands the SAME archive to both mods, collapsing them onto one
    archiveId (Vortex then flags them as duplicate mods). The collection entry
    carries the exact ``fileSize`` of its file, which is unique per file on a
    Nexus mod page, so we match on that first and only fall back to name overlap
    when size can't decide (missing/identical sizes)."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    target = (mod.get("source", {}) or {}).get("fileSize")
    if target:
        exact = [c for c in candidates if sizes.get(c) == target]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:                    # size tie -> break by name overlap
            return _best_match(exact, mod)
    return _best_match(candidates, mod)


def _gen_id(seed: str) -> str:
    """Deterministic 12-char download id from a seed (stable across re-runs)."""
    h = hashlib.sha1(seed.encode()).hexdigest()
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    out = "NXD"
    for i in range(9):
        out += alphabet[int(h[i * 2:i * 2 + 2], 16) % len(alphabet)]
    return out


def index_by_modid(names: List[str], *, archives_only: bool = False) -> Dict[str, List[str]]:
    """Group file/folder names by the modId embedded in them (``-<modId>-``)."""
    out: Dict[str, List[str]] = {}
    for n in names:
        if archives_only and not ARCHIVE_RE.search(n):
            continue
        m = _MODID_RE.search(n)
        if m:
            out.setdefault(m.group(1), []).append(n)
    return out


def parse_revision_from_folder(folder: str) -> Tuple[int, int]:
    """Extract ``(revisionId, revisionNumber)`` from a collection folder name."""
    m = re.search(r"-(\d+)-(\d+)-(\d+)$", folder)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


@dataclass
class SyncPlan:
    records: Dict[str, str] = field(default_factory=dict)   # abs ### key -> json value
    mod_count: int = 0
    new_downloads: int = 0
    profile_enables: int = 0
    skipped_manual: int = 0
    skipped_no_disk: int = 0
    rule_count: int = 0
    orphan_count: int = 0       # staging folders not in the collection, given bare records
    modrule_count: int = 0      # per-mod before/after conflict rules written
    violations: List[str] = field(default_factory=list)     # schema problems

    @property
    def total_keys(self) -> int:
        return len(self.records)


def build_plan(collection: Dict[str, Any], *, downloads_by_modid: Dict[str, List[str]],
               folders_by_modid: Dict[str, List[str]], existing_dl_by_path: Dict[str, str],
               profile_id: str, collection_folder: str, collection_id: int, slug: str,
               revision_id: int, revision_number: int,
               install_time_iso: str = "", install_ms: int = 0,
               all_folders: Optional[List[str]] = None,
               archive_sizes: Optional[Dict[str, int]] = None) -> SyncPlan:
    """Build + validate every record for the sync (pure, no I/O).

    ``install_time_iso``/``install_ms`` (passed in by :func:`run` so this stays
    deterministic) stamp the install-time / install-completed fields Vortex writes.
    ``all_folders`` (every staging subfolder) lets us write a bare record for any
    folder that isn't a collection member, so Vortex's "Mods changed on disk" scan
    finds full folder<->record parity and never re-stubs our records.
    """
    plan = SyncPlan()
    sizes = archive_sizes or {}
    info = collection.get("info", {})
    coll_name = info.get("name", collection_folder)   # the mods' "variant"
    recorded: set = set()   # staging folders we've written a mod record for
    # Maps for translating collection.modRules into per-mod conflict rules:
    # resolve a rule endpoint (by md5 / logical name) to the folder we actually
    # installed it into, plus that folder's download (archive) id.
    folder_by_md5: Dict[str, str] = {}
    folder_by_logical: Dict[str, str] = {}
    archive_id_by_folder: Dict[str, str] = {}

    def add(record_type: str, base: str, leaves: Dict[str, Any]):
        plan.violations.extend(
            f"{base.split('###')[-1]}: {v}" for v in validate_record(record_type, leaves))
        plan.records.update(vr.to_absolute(base, leaves))

    for mod in collection.get("mods", []):
        s = mod.get("source", {})
        if s.get("type") != "nexus" or not s.get("modId") or not s.get("fileId"):
            plan.skipped_manual += 1
            continue
        folders = folders_by_modid.get(str(s["modId"]), [])
        archives = downloads_by_modid.get(str(s["modId"]), [])
        if not folders or not archives:
            plan.skipped_no_disk += 1
            continue
        # Disambiguate shared-modId variants so each mod links its OWN folder,
        # not folders[0] (else every variant but the first shows 'Not Installed').
        folder = _best_match([f for f in folders if f not in recorded] or folders, mod)
        archive = _match_archive(archives, mod, sizes)

        dl_id = existing_dl_by_path.get(archive)
        if not dl_id:
            dl_id = _gen_id(f"{s['modId']}-{s['fileId']}")
            base, leaves = vr.build_download(s, mod.get("name", ""), archive, dl_id,
                                             folder=folder, collection_id=collection_id)
            add("download", base, leaves)
            plan.new_downloads += 1

        base, leaves = vr.build_mod(s, mod, folder, dl_id, archive,
                                    variant=coll_name, installed_as_dependency=True,
                                    install_time=install_time_iso)
        add("mod", base, leaves)
        base, leaves = vr.build_profile_modstate(profile_id, folder)
        add("profile_modstate", base, leaves)
        recorded.add(folder)
        # Index this mod for modRule resolution (md5 + logical name -> its folder).
        archive_id_by_folder[folder] = dl_id
        if s.get("md5"):
            folder_by_md5[s["md5"].lower()] = folder
        lf = (s.get("logicalFilename") or mod.get("name") or "").lower()
        if lf:
            folder_by_logical[lf] = folder
        plan.mod_count += 1
        plan.profile_enables += 1

    # Phase 1b: translate the collection's modRules into per-mod conflict rules.
    # Vortex resolves file conflicts from each mod's own `rules` array; without
    # them every overlap shows as an unresolved conflict even though our deploy
    # already ordered them. Mirrors collections/postprocessCollection.ts: add a
    # {type, reference:{id,idHint,archiveId}} entry to the SOURCE mod for each
    # before/after rule, with the reference resolved to the DEST mod's folder.
    def _resolve_ref(ref: Dict[str, Any]) -> Optional[str]:
        md5 = (ref.get("fileMD5") or "").lower()
        if md5 and md5 in folder_by_md5:
            return folder_by_md5[md5]
        lf = (ref.get("logicalFileName") or "").lower()
        if lf and lf in folder_by_logical:
            return folder_by_logical[lf]
        idh = ref.get("idHint") or ref.get("id")
        return idh if idh in recorded else None

    rules_by_folder: Dict[str, list] = {}
    for rule in collection.get("modRules", []):
        if rule.get("type") not in ("before", "after"):
            continue   # only load-order rules resolve file conflicts
        src_folder = _resolve_ref(rule.get("source") or {})
        if not src_folder:
            continue
        dst_folder = _resolve_ref(rule.get("reference") or {})
        if dst_folder:
            reference = {"id": dst_folder, "idHint": dst_folder,
                         "archiveId": archive_id_by_folder.get(dst_folder, "")}
        else:
            reference = rule.get("reference") or {}
        rules_by_folder.setdefault(src_folder, []).append(
            {"type": rule["type"], "reference": reference})

    for folder, frules in rules_by_folder.items():
        base = f"persistent###mods###{GAME_ID}###{folder}"
        plan.records.update(vr.to_absolute(base, {"rules": frules}))
    plan.modrule_count = sum(len(v) for v in rules_by_folder.values())

    # Phase 2: collection mod @ revision + rules + manifest
    rules = []
    for mod in collection.get("mods", []):
        s = mod.get("source", {})
        if s.get("type") != "nexus" or not s.get("modId"):
            continue
        archive = _match_archive(downloads_by_modid.get(str(s["modId"]), []), mod, sizes) or ""
        rules.append(vr.build_collection_rule(mod, archive))
    plan.rule_count = len(rules)

    coll_archive = collection_folder + ".7z"
    coll_dl_id = existing_dl_by_path.get(coll_archive) or _gen_id(f"coll-{revision_id}")
    if coll_archive not in existing_dl_by_path:
        # Register the collection's own download so Vortex can read the collection
        # identity from downloads.files[archiveId].modInfo.nexus.ids (the linkup).
        base, leaves = vr.build_collection_download(
            info, coll_archive, coll_dl_id, collection_id=collection_id, slug=slug,
            revision_id=revision_id, revision_number=revision_number,
            folder=collection_folder)
        plan.records.update(vr.to_absolute(base, leaves))   # download record (no member schema)
        plan.new_downloads += 1

    base, leaves = vr.build_collection_mod(info, collection_folder, coll_dl_id, rules,
                                           revision_id, revision_number, collection_id, slug,
                                           install_completed_ms=install_ms,
                                           install_time=install_time_iso)
    add("collection", base, leaves)
    base, leaves = vr.build_profile_modstate(profile_id, collection_folder)
    add("profile_modstate", base, leaves)
    recorded.add(collection_folder)
    base, leaves = vr.build_collection_revision(info, collection.get("mods", []),
                                                revision_id, revision_number, collection_id, slug)
    plan.records.update(vr.to_absolute(base, leaves))   # manifest has no record schema

    # Phase 3: bare records for staging folders that aren't collection members
    # (old versions, manually-added mods, duplicates) so refreshMods sees parity.
    for folder in (all_folders or []):
        if folder in recorded or folder.startswith("__vortex"):
            continue
        # Orphans are intentionally bare (no modId/source) -- identical to the stub
        # Vortex itself writes -- so they're schema-exempt; don't validate as "mod".
        base, leaves = vr.build_orphan_mod(folder, install_time=install_time_iso)
        plan.records.update(vr.to_absolute(base, leaves))
        base, leaves = vr.build_profile_modstate(profile_id, folder)
        add("profile_modstate", base, leaves)
        recorded.add(folder)
        plan.orphan_count += 1
        plan.profile_enables += 1

    return plan


# --------------------------------------------------------------------------- #
# I/O orchestration
# --------------------------------------------------------------------------- #
def find_replaceable_collections(mods_data: Dict[str, object], collection_id: int,
                                 keep_folder: str, profile_id: str,
                                 game: str = GAME_ID) -> List[str]:
    """Prefixes to delete for OLD revisions of the same collection.

    Given ``persistent.mods.<game>.*`` data, find collection-type mod records that
    share ``collection_id`` but aren't the folder we're writing now, and return
    the prefixes (the old collection mod + its profile modState) to remove -- so a
    198 -> 232 update doesn't leave two collection entries behind.
    """
    by_folder: Dict[str, Dict[str, object]] = {}
    for k, v in mods_data.items():
        parts = k.split("###")
        if len(parts) < 5:
            continue
        by_folder.setdefault(parts[3], {})[".".join(parts[4:])] = v

    prefixes: List[str] = []
    for folder, leaves in by_folder.items():
        if folder == keep_folder:
            continue
        if leaves.get("type") == "collection" and leaves.get("attributes.collectionId") == collection_id:
            prefixes.append(f"persistent###mods###{game}###{folder}")
            prefixes.append(f"persistent###profiles###{profile_id}###modState###{folder}")
    return prefixes


@dataclass
class SyncResult:
    applied: bool
    plan: SyncPlan
    risk: RiskAssessment
    keys_written: int = 0
    backup_path: str = ""
    message: str = ""
    replaced_collections: int = 0   # old collection revisions removed


def _sample_live_records(read_prefix, db_path) -> Dict[str, Dict[str, Any]]:
    """Sample one live record of each type to feed drift detection."""
    samples: Dict[str, Dict[str, Any]] = {}
    for rtype, schema in RECORD_SCHEMAS.items():
        try:
            data = read_prefix(db_path, schema.base_prefix + "###")
        except Exception:
            continue
        # group by the record id (4th ### segment) and take the first complete one
        groups: Dict[str, Dict[str, Any]] = {}
        for k, v in data.items():
            parts = k.split("###")
            if len(parts) < 5:
                continue
            rel = ".".join(parts[4:])
            groups.setdefault(parts[3], {})[rel] = v
        if groups:
            samples[rtype] = next(iter(groups.values()))
    return samples


def run(db_path: str, collection_path: str, downloads_dir: str, staging_dir: str,
        profile_id: str, *, collection_id: int, slug: str,
        apply: bool = False, force: bool = False, replace: bool = True,
        node: str = "node") -> SyncResult:
    """Read state, assess drift, check the lock, build+validate, and (optionally) write.

    When ``replace`` is set, old revisions of the same collection are removed in
    the same write so the update doesn't leave a stale collection record behind.
    """
    import json
    import time
    from utils import vortex_db

    install_ms = int(time.time() * 1000)
    install_time_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

    with open(collection_path, "r", encoding="utf-8") as fh:
        collection = json.load(fh)

    downloads = index_by_modid(os.listdir(downloads_dir), archives_only=True)
    # Exact byte size per archive basename, so build_plan can disambiguate
    # shared-modId files by fileSize (the way Vortex matches) instead of name.
    archive_sizes: Dict[str, int] = {}
    for names in downloads.values():
        for name in names:
            try:
                archive_sizes[name] = os.path.getsize(os.path.join(downloads_dir, name))
            except OSError:
                pass
    all_folders = [d for d in os.listdir(staging_dir)
                   if os.path.isdir(os.path.join(staging_dir, d))]
    folders = index_by_modid(all_folders)
    collection_folder = os.path.basename(os.path.dirname(collection_path))
    revision_id, revision_number = parse_revision_from_folder(collection_folder)

    # existing downloads localPath -> id
    raw_dl = vortex_db.read_prefix(db_path, "persistent###downloads###files###", node=node)
    existing_dl_by_path = {v: k.split("###")[3] for k, v in raw_dl.items()
                           if k.endswith("###localPath")}

    # drift assessment against the target Vortex version
    app = vortex_db.read_prefix(db_path, "app###appVersion", node=node)
    target_version = app.get("app###appVersion")
    risk = assess_risk(target_version,
                       _sample_live_records(vortex_db.read_prefix, db_path))

    plan = build_plan(collection, downloads_by_modid=downloads, folders_by_modid=folders,
                      existing_dl_by_path=existing_dl_by_path, profile_id=profile_id,
                      collection_folder=collection_folder, collection_id=collection_id,
                      slug=slug, revision_id=revision_id, revision_number=revision_number,
                      install_time_iso=install_time_iso, install_ms=install_ms,
                      all_folders=all_folders, archive_sizes=archive_sizes)

    if not apply:
        return SyncResult(False, plan, risk, message="dry-run")
    if plan.violations and not force:
        return SyncResult(False, plan, risk,
                          message=f"aborted: {len(plan.violations)} schema violations (use force=True to override)")
    if not risk.safe and not force:
        return SyncResult(False, plan, risk,
                          message=f"aborted: {risk.message} (use force=True to override)")

    # Replace old revisions of the same collection (delete in the same batch).
    delete_prefixes: List[str] = []
    if replace:
        mods_data = vortex_db.read_prefix(db_path, f"persistent###mods###{GAME_ID}###", node=node)
        delete_prefixes = find_replaceable_collections(
            mods_data, collection_id, collection_folder, profile_id)

    res = vortex_db.write_records(db_path, plan.records, backup=True, node=node,
                                  delete_prefixes=delete_prefixes)
    return SyncResult(True, plan, risk, res.keys_written, res.backup_path, "applied",
                      replaced_collections=len(delete_prefixes) // 2)


def sync_collection(collection_path: str, downloads_dir: str, staging_dir: str, *,
                    apply: bool = False, force: bool = False, replace: bool = True,
                    node: str = "node") -> SyncResult:
    """High-level entry for the GUI: auto-discover the Vortex DB, active profile,
    and collection identity, then run the sync.

    Raises a clear error if Vortex's DB can't be found or the collection identity
    can't be determined (e.g. a brand-new collection Vortex has never seen).
    """
    from utils import vortex_db

    db_path = vortex_db.find_state_db()
    if not db_path:
        raise FileNotFoundError(
            "Could not find Vortex's database (state.v2). Is Vortex installed?")

    profile_id = vortex_db.read_active_profile(db_path, node=node)
    if not profile_id:
        raise RuntimeError("Could not determine the active Vortex profile.")

    identity = vortex_db.read_collection_identity(db_path, node=node)
    if not identity:
        diag = vortex_db.collection_diagnostic(db_path, node=node)
        raise RuntimeError(
            "Could not determine the collection's id/slug from Vortex. "
            f"[{diag}] If the collection isn't in Vortex yet, add it once so it "
            "knows it, then re-sync.")
    collection_id, slug = identity

    return run(db_path, collection_path, downloads_dir, staging_dir, profile_id,
               collection_id=collection_id, slug=slug, apply=apply, force=force,
               replace=replace, node=node)


def _main(argv=None):
    """Standalone CLI: dry-run by default; --apply writes (backs up first)."""
    import argparse
    parser = argparse.ArgumentParser(description="Sync a downloaded/staged collection into Vortex")
    parser.add_argument("--db", required=True)
    parser.add_argument("--collection", required=True)
    parser.add_argument("--downloads", required=True)
    parser.add_argument("--staging", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--collection-id", type=int, required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    res = run(args.db, args.collection, args.downloads, args.staging, args.profile,
              collection_id=args.collection_id, slug=args.slug,
              apply=args.apply, force=args.force)
    p = res.plan
    print(f"drift: {res.risk.level} - {res.risk.message}")
    print(f"mods={p.mod_count} new_downloads={p.new_downloads} rules={p.rule_count} "
          f"skipped(manual={p.skipped_manual}, no-disk={p.skipped_no_disk}) keys={p.total_keys}")
    print(f"schema violations: {len(p.violations)}")
    print(res.message + (f" (wrote {res.keys_written} keys, backup {res.backup_path})"
                         if res.applied else ""))
    return 0 if (res.applied or not args.apply) else 1


if __name__ == "__main__":
    import sys
    sys.exit(_main())
