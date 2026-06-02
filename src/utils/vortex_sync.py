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
from typing import Any, Dict, List, Tuple

from utils import vortex_records as vr
from utils.vortex_schema import (
    validate_record, assess_risk, RiskAssessment, RECORD_SCHEMAS,
)

ARCHIVE_RE = re.compile(r"\.(7z|zip|rar)$", re.IGNORECASE)
_MODID_RE = re.compile(r"-(\d{2,7})-")


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
    violations: List[str] = field(default_factory=list)     # schema problems

    @property
    def total_keys(self) -> int:
        return len(self.records)


def build_plan(collection: Dict[str, Any], *, downloads_by_modid: Dict[str, List[str]],
               folders_by_modid: Dict[str, List[str]], existing_dl_by_path: Dict[str, str],
               profile_id: str, collection_folder: str, collection_id: int, slug: str,
               revision_id: int, revision_number: int) -> SyncPlan:
    """Build + validate every record for the sync (pure, no I/O)."""
    plan = SyncPlan()

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
        folder, archive = folders[0], archives[0]

        dl_id = existing_dl_by_path.get(archive)
        if not dl_id:
            dl_id = _gen_id(f"{s['modId']}-{s['fileId']}")
            base, leaves = vr.build_download(s, mod.get("name", ""), archive, dl_id)
            add("download", base, leaves)
            plan.new_downloads += 1

        base, leaves = vr.build_mod(s, mod, folder, dl_id, archive)
        add("mod", base, leaves)
        base, leaves = vr.build_profile_modstate(profile_id, folder)
        add("profile_modstate", base, leaves)
        plan.mod_count += 1
        plan.profile_enables += 1

    # Phase 2: collection mod @ revision + rules + manifest
    info = collection.get("info", {})
    rules = []
    for mod in collection.get("mods", []):
        s = mod.get("source", {})
        if s.get("type") != "nexus" or not s.get("modId"):
            continue
        archive = (downloads_by_modid.get(str(s["modId"])) or [""])[0]
        rules.append(vr.build_collection_rule(mod, archive))
    plan.rule_count = len(rules)

    coll_archive = collection_folder + ".7z"
    coll_dl_id = existing_dl_by_path.get(coll_archive) or _gen_id(f"coll-{revision_id}")
    if coll_archive not in existing_dl_by_path:
        plan.new_downloads += 1

    base, leaves = vr.build_collection_mod(info, collection_folder, coll_dl_id, rules,
                                           revision_id, revision_number, collection_id, slug)
    add("collection", base, leaves)
    base, leaves = vr.build_profile_modstate(profile_id, collection_folder)
    add("profile_modstate", base, leaves)
    base, leaves = vr.build_collection_revision(info, collection.get("mods", []),
                                                revision_id, revision_number, collection_id, slug)
    plan.records.update(vr.to_absolute(base, leaves))   # manifest has no record schema

    return plan


# --------------------------------------------------------------------------- #
# I/O orchestration
# --------------------------------------------------------------------------- #
@dataclass
class SyncResult:
    applied: bool
    plan: SyncPlan
    risk: RiskAssessment
    keys_written: int = 0
    backup_path: str = ""
    message: str = ""


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
        apply: bool = False, force: bool = False, node: str = "node") -> SyncResult:
    """Read state, assess drift, check the lock, build+validate, and (optionally) write."""
    import json
    from utils import vortex_db

    with open(collection_path, "r", encoding="utf-8") as fh:
        collection = json.load(fh)

    downloads = index_by_modid(os.listdir(downloads_dir), archives_only=True)
    folders = index_by_modid([d for d in os.listdir(staging_dir)
                              if os.path.isdir(os.path.join(staging_dir, d))])
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
                      slug=slug, revision_id=revision_id, revision_number=revision_number)

    if not apply:
        return SyncResult(False, plan, risk, message="dry-run")
    if plan.violations and not force:
        return SyncResult(False, plan, risk,
                          message=f"aborted: {len(plan.violations)} schema violations (use force=True to override)")
    if not risk.safe and not force:
        return SyncResult(False, plan, risk,
                          message=f"aborted: {risk.message} (use force=True to override)")

    res = vortex_db.write_records(db_path, plan.records, backup=True, node=node)
    return SyncResult(True, plan, risk, res.keys_written, res.backup_path, "applied")


def sync_collection(collection_path: str, downloads_dir: str, staging_dir: str, *,
                    apply: bool = False, force: bool = False,
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
        raise RuntimeError(
            "Could not determine the collection's id/slug from Vortex. Add the "
            "collection in Vortex once so it knows it, then re-sync.")
    collection_id, slug = identity

    return run(db_path, collection_path, downloads_dir, staging_dir, profile_id,
               collection_id=collection_id, slug=slug, apply=apply, force=force, node=node)


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
