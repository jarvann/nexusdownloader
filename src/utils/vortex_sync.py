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
# Match a modId token with a LOOKAHEAD delimiter so adjacent ids aren't consumed:
# "SMIM SE 2-08-659-2-08" must yield {08, 659}, not {08, 2} (the real modId 659 is
# otherwise eaten as the boundary between "-08-" and "-2-"). We index a name under
# EVERY such token because Vortex never encodes the modId in a fixed position --
# the folder/archive name is just the masked archive filename (deriveModInstallName)
# -- so the only robust key is "does this name contain the mod's id as a token".
_MODID_RE = re.compile(r"-(\d{2,7})(?=[-.])")


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


def _break_rule_cycles(edges: List[Tuple[str, str, str, dict]],
                       rules_by_folder: Dict[str, list]) -> int:
    """Remove rules whose ordering edge closes a cycle, leaving an acyclic set.

    Vortex topo-sorts mods from their before/after rules; a cycle (A before B and
    B before A) is unsortable and pops the "Cycles" dialog asking the user to
    delete a rule by hand. We do that automatically: a depth-first walk over the
    "u loads before v" graph; any edge pointing back to a node still on the
    recursion stack is a back-edge (the one closing a cycle), so we drop that
    rule. Removing all DFS back-edges is guaranteed to yield a DAG. ``edges`` is
    (u, v, src_folder, rule_obj); the rule lives in ``rules_by_folder[src_folder]``
    and is removed by object identity. Returns the number of rules dropped."""
    import sys
    from collections import defaultdict

    adj: Dict[str, list] = defaultdict(list)
    for u, v, sf, ro in edges:
        adj[u].append((v, sf, ro))

    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[str, int] = defaultdict(int)
    dropped = 0
    sys.setrecursionlimit(max(10000, len(adj) * 4 + 1000))

    def dfs(start: str) -> None:
        nonlocal dropped
        color[start] = GRAY
        for (v, sf, ro) in adj[start]:
            c = color[v]
            if c == GRAY:                       # back-edge -> closes a cycle
                lst = rules_by_folder.get(sf)
                if lst and ro in lst:
                    lst.remove(ro)
                    dropped += 1
            elif c == WHITE:
                dfs(v)
        color[start] = BLACK

    for node in list(adj):
        if color[node] == WHITE:
            dfs(node)
    return dropped


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
        for mid in set(_MODID_RE.findall(n)):
            out.setdefault(mid, []).append(n)
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
    dropped_cycle_rules: int = 0  # rules dropped as self-loops / cycle-breakers
    violations: List[str] = field(default_factory=list)     # schema problems
    # (modId,fileId) -> the folder we installed it under. Lets run() spot Vortex's
    # leftover native stub for the same file under a DIFFERENT folder and delete it.
    member_folders: Dict[Tuple[int, int], str] = field(default_factory=dict)

    @property
    def total_keys(self) -> int:
        return len(self.records)


def build_plan(collection: Dict[str, Any], *, ledger_mods: List[Dict[str, Any]],
               profile_id: str, collection_folder: str, collection_id: int, slug: str,
               revision_id: int, revision_number: int,
               install_time_iso: str = "", install_ms: int = 0) -> SyncPlan:
    """Build + validate every record for the sync (pure; reads only ``ledger_mods``).

    ``ledger_mods`` is the authoritative state from :mod:`utils.local_state`
    (``all_mods_with_download`` rows -- each carries its staging folder plus the
    joined download's modId/fileId/md5/local_path). We PROJECT those into Vortex
    records: no disk globbing, no archive/folder guessing, so the wrong-archive /
    duplicate-mods / "Never Installed" failure modes are structurally impossible.
    ``install_time_iso``/``install_ms`` stamp Vortex's install-time fields.
    """
    plan = SyncPlan()
    info = collection.get("info", {})
    coll_name = info.get("name", collection_folder)   # the mods' "variant"
    recorded: set = set()
    # Maps for translating collection.modRules into per-mod conflict rules: resolve
    # a rule endpoint (by md5 / logical name) to the folder it's installed in.
    folder_by_md5: Dict[str, str] = {}
    folder_by_logical: Dict[str, str] = {}
    archive_id_by_folder: Dict[str, str] = {}
    archive_by_modid: Dict[str, str] = {}
    written_downloads: set = set()

    # Collection entries indexed by (modId,fileId) so a ledger mod -- which holds
    # the authoritative download identity -- can pull its display metadata.
    coll_by_modfile: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for m in collection.get("mods", []):
        s = m.get("source") or {}
        if s.get("type") == "nexus" and s.get("modId") and s.get("fileId"):
            coll_by_modfile[(int(s["modId"]), int(s["fileId"]))] = m

    def add(record_type: str, base: str, leaves: Dict[str, Any]):
        plan.violations.extend(
            f"{base.split('###')[-1]}: {v}" for v in validate_record(record_type, leaves))
        plan.records.update(vr.to_absolute(base, leaves))

    # Phase 1: project each LEDGER mod into Vortex records. The ledger already holds
    # the exact folder<->download<->identity, so there is nothing to match here.
    for row in ledger_mods:
        folder = row["folder"]
        dl_id = row.get("download_id") or ""
        mid = row.get("dl_mod_id")
        fid = row.get("dl_file_id")
        archive = row.get("dl_local_path") or ""
        md5 = row.get("dl_md5") or ""
        mod_data = coll_by_modfile.get((mid, fid)) if (mid and fid) else None

        # download record (once per id) from the ledger's authoritative metadata.
        # Only for real nexus identities -- orphans (user/manual mods) get a bare
        # mod record with no download, so skip building one with a null modId.
        if dl_id and mid and fid and dl_id not in written_downloads:
            # Vortex's "Downloaded Time" reads fileTime (ms). Our ledger stores
            # downloaded_at in seconds; without it Vortex shows epoch 0 ("56 years
            # ago"). Fall back to the install timestamp for pre-column rows.
            dl_at = row.get("dl_downloaded_at")
            file_time_ms = int(dl_at) * 1000 if dl_at else install_ms
            dsrc = {"type": "nexus", "modId": mid, "fileId": fid, "md5": md5,
                    "fileSize": row.get("dl_file_size") or 0,
                    "fileTime": file_time_ms,
                    "logicalFilename": row.get("dl_logical") or ""}
            name = (mod_data or {}).get("name", "") or row.get("dl_logical") or ""
            base, leaves = vr.build_download(dsrc, name, archive, dl_id,
                                             folder=folder, collection_id=collection_id)
            add("download", base, leaves)
            written_downloads.add(dl_id)
            plan.new_downloads += 1
        if mid and archive:
            archive_by_modid.setdefault(str(mid), archive)

        if mod_data is not None:
            s = mod_data.get("source", {})
            base, leaves = vr.build_mod(s, mod_data, folder, dl_id, archive,
                                        variant=coll_name, installed_as_dependency=True,
                                        install_time=install_time_iso)
            add("mod", base, leaves)
            base, leaves = vr.build_profile_modstate(profile_id, folder)
            add("profile_modstate", base, leaves)
            plan.mod_count += 1
            plan.profile_enables += 1
            if mid and fid:
                plan.member_folders[(int(mid), int(fid))] = folder
            archive_id_by_folder[folder] = dl_id
            if md5:
                folder_by_md5[md5.lower()] = folder
            lf = (s.get("logicalFilename") or mod_data.get("name") or "").lower()
            if lf:
                folder_by_logical[lf] = folder
        else:
            # A staging folder with no matching collection nexus entry (the user's
            # own mods, manual installs). Bare record -- identical to Vortex's own
            # stub -- so refreshMods sees parity and never re-stubs it.
            base, leaves = vr.build_orphan_mod(folder, install_time=install_time_iso)
            plan.records.update(vr.to_absolute(base, leaves))
            base, leaves = vr.build_profile_modstate(profile_id, folder)
            add("profile_modstate", base, leaves)
            plan.orphan_count += 1
            plan.profile_enables += 1
        recorded.add(folder)

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
    # Ordering edges (u loads before v) for cycle detection, each tagged with the
    # source folder + rule object so we can drop the offending rule if it closes a
    # cycle. Only rules resolved on BOTH ends participate in the folder graph.
    ordered_edges: List[Tuple[str, str, str, dict]] = []
    for rule in collection.get("modRules", []):
        if rule.get("type") not in ("before", "after"):
            continue   # only load-order rules resolve file conflicts
        src_folder = _resolve_ref(rule.get("source") or {})
        if not src_folder:
            continue
        dst_folder = _resolve_ref(rule.get("reference") or {})
        if dst_folder == src_folder:
            # Both endpoints collapsed to the SAME installed folder (shared-modId
            # variants). A rule pointing a mod at itself is a meaningless self-loop
            # and Vortex flags it as a cycle -- drop it. Keep the folder in the map
            # (empty) so a re-Link OVERWRITES any stale cyclic rule we wrote before.
            rules_by_folder.setdefault(src_folder, [])
            plan.dropped_cycle_rules += 1
            continue
        if dst_folder:
            reference = {"id": dst_folder, "idHint": dst_folder,
                         "archiveId": archive_id_by_folder.get(dst_folder, "")}
        else:
            reference = rule.get("reference") or {}
        rule_obj = {"type": rule["type"], "reference": reference}
        rules_by_folder.setdefault(src_folder, []).append(rule_obj)
        if dst_folder:
            u, v = ((src_folder, dst_folder) if rule["type"] == "before"
                    else (dst_folder, src_folder))
            ordered_edges.append((u, v, src_folder, rule_obj))

    # Break any remaining cycles (the collection author's own cyclic rules, or ones
    # our resolution introduced) by dropping the edge that closes each cycle --
    # exactly the manual fix Vortex's "Cycles" dialog asks the user to do, so the
    # cyclic rules never reach Vortex.
    plan.dropped_cycle_rules += _break_rule_cycles(ordered_edges, rules_by_folder)

    # Write rules for every folder we touched -- INCLUDING the ones left empty by
    # self-loop/cycle dropping. Writing an empty [] overwrites any stale cyclic
    # rules a prior Link left in the DB, so a re-Link truly clears the cycle.
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
        archive = archive_by_modid.get(str(s["modId"]), "")
        rules.append(vr.build_collection_rule(mod, archive))
    plan.rule_count = len(rules)

    # The collection's own download (so Vortex reads collection identity from
    # downloads.files[archiveId].modInfo.nexus.ids). Stable id per revision; always
    # (re)written -- idempotent on re-Link.
    coll_archive = collection_folder + ".7z"
    coll_dl_id = _gen_id(f"coll-{revision_id}")
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


def _folder_is_fileless(staging_dir: str, folder: str) -> bool:
    """True if ``folder`` backs no real files (missing, or an empty dir tree).

    The safety gate for stub deletion: we only ever drop a Vortex mod record whose
    staging folder holds nothing, so a real install is never removed by accident.
    """
    path = os.path.join(staging_dir, folder)
    if not os.path.isdir(path):
        return True
    for _root, _dirs, files in os.walk(path):
        if files:
            return False
    return True


def find_duplicate_stubs(mods_data: Dict[str, object],
                         member_folders: Dict[Tuple[int, int], str],
                         staging_dir: str, profile_id: str,
                         game: str = GAME_ID) -> List[str]:
    """Prefixes to delete for Vortex's leftover "Never Installed" twin stubs.

    Our projection is pure (it never reads Vortex's side), so when Vortex already
    had a native mod entry for a file under a DIFFERENT folder than the one we
    installed it under, both survive -- ours (Enabled, a member) plus Vortex's
    ghost (Unspecified, "Never Installed"). For each native entry whose
    (modId,fileId) matches one we just projected as a member but under a different,
    file-less folder, return the prefixes (mod record + its profile modState) to
    remove. The file-less gate guarantees we never delete a folder with real files.
    """
    by_folder: Dict[str, Dict[str, object]] = {}
    for k, v in mods_data.items():
        parts = k.split("###")
        if len(parts) < 5:
            continue
        by_folder.setdefault(parts[3], {})[".".join(parts[4:])] = v

    prefixes: List[str] = []
    for folder, leaves in by_folder.items():
        mid, fid = leaves.get("attributes.modId"), leaves.get("attributes.fileId")
        if mid is None or fid is None:
            continue
        try:
            key = (int(mid), int(fid))
        except (TypeError, ValueError):
            continue
        ours = member_folders.get(key)
        if not ours or ours == folder:
            continue  # not one of ours, or it IS our folder (a clean overwrite)
        if not _folder_is_fileless(staging_dir, folder):
            continue  # a real install lives here -- leave it alone
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
    removed_stubs: int = 0          # Vortex's leftover "Never Installed" twin stubs


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
    from utils import vortex_db, local_state, state_reconcile

    install_ms = int(time.time() * 1000)
    install_time_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

    with open(collection_path, "r", encoding="utf-8") as fh:
        collection = json.load(fh)

    collection_folder = os.path.basename(os.path.dirname(collection_path))
    revision_id, revision_number = parse_revision_from_folder(collection_folder)

    # Refresh the download+mod identity from disk before projecting. This is the
    # FAST reconcile (no file walk / hashing -- Link only needs identity), cheap
    # enough to run every Link so newly-installed mods are always picked up.
    state_reconcile.reconcile_mods(staging_dir, downloads_dir, collection_path,
                                   log=lambda *_: None)
    state = local_state.LocalState(local_state.db_path_for(staging_dir))
    try:
        # Back-fill the collection id onto downloads recorded at download time
        # (before Vortex knew the collection). Match on the game domain we stored.
        game_domain = (collection.get("info") or {}).get("domainName", "")
        if collection_id and game_domain:
            state.link_downloads_to_collection(int(collection_id), game_domain)
            state.flush()
        ledger_mods = state.all_mods_with_download()
    finally:
        state.close()

    # drift assessment against the target Vortex version
    app = vortex_db.read_prefix(db_path, "app###appVersion", node=node)
    target_version = app.get("app###appVersion")
    risk = assess_risk(target_version,
                       _sample_live_records(vortex_db.read_prefix, db_path))

    plan = build_plan(collection, ledger_mods=ledger_mods, profile_id=profile_id,
                      collection_folder=collection_folder, collection_id=collection_id,
                      slug=slug, revision_id=revision_id, revision_number=revision_number,
                      install_time_iso=install_time_iso, install_ms=install_ms)

    if not apply:
        return SyncResult(False, plan, risk, message="dry-run")
    if plan.violations and not force:
        return SyncResult(False, plan, risk,
                          message=f"aborted: {len(plan.violations)} schema violations (use force=True to override)")
    if not risk.safe and not force:
        return SyncResult(False, plan, risk,
                          message=f"aborted: {risk.message} (use force=True to override)")

    # Read Vortex's current mods once, for both cleanup passes below.
    delete_prefixes: List[str] = []
    removed_stubs = 0
    if replace:
        mods_data = vortex_db.read_prefix(db_path, f"persistent###mods###{GAME_ID}###", node=node)
        # 1. Old revisions of the same collection (a 198 -> 232 update).
        delete_prefixes = find_replaceable_collections(
            mods_data, collection_id, collection_folder, profile_id)
        # 2. Vortex's leftover "Never Installed" twin stubs for files we installed
        #    under a different folder (the <Unspecified> ghosts). File-less only.
        stub_prefixes = find_duplicate_stubs(
            mods_data, plan.member_folders, staging_dir, profile_id)
        delete_prefixes.extend(stub_prefixes)
        removed_stubs = len(stub_prefixes) // 2

    res = vortex_db.write_records(db_path, plan.records, backup=True, node=node,
                                  delete_prefixes=delete_prefixes)
    return SyncResult(True, plan, risk, res.keys_written, res.backup_path, "applied",
                      replaced_collections=len(delete_prefixes) // 2 - removed_stubs,
                      removed_stubs=removed_stubs)


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
