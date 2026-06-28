"""
Vortex deployment writer.

After mods are staged, Vortex "deploys" them by hard-linking each staged file into
the game's Data folder and recording a manifest (``vortex.deployment.json``) so it
knows exactly what it placed (and can purge/redeploy later). This module
reproduces that step so a collection we staged shows up in-game *without* the user
clicking Deploy, and Vortex still recognises the deployment as its own.

Layers (mirrors the rest of the sync code):

* :func:`resolve_deployment` -- **pure**. Given staged files in ascending priority
  order it picks the winning source per relative path (last wins, like Vortex's
  hardlink deploy order) and returns the manifest entries + the link plan.
* :func:`build_manifest` -- **pure**. Wraps entries in the exact manifest shape
  Vortex 2.0.x writes (verified against a live ``vortex.deployment.json``).
* :func:`deploy` -- **I/O**. Walks staging, hard-links winners into the target,
  and writes the manifest atomically.
* :func:`mark_deployed_in_db` -- **I/O**. Bumps ``deploymentCounter`` and clears
  ``needToDeploy`` in state.v2 so Vortex sees the deployment as current.

The manifest's ``instance`` must match the live Vortex instance id; it is read
from an existing manifest, else the ``__vortex_staging_folder`` marker.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from utils.vortex_schema import GAME_ID

MANIFEST_NAME = "vortex.deployment.json"
STAGING_MARKER = "__vortex_staging_folder"
DEPLOY_METHOD = "hardlink_activator"
MANIFEST_VERSION = 1


def _to_backslash(rel: str) -> str:
    """Manifest paths use Windows separators regardless of the host OS."""
    return rel.replace("/", "\\")


# --------------------------------------------------------------------------- #
# Pure planning
# --------------------------------------------------------------------------- #
def resolve_deployment(
    staged: Iterable[Tuple[str, str, int]]
) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]:
    """Resolve file conflicts and produce manifest entries + a link plan.

    Args:
        staged: ``(source_folder, rel_path, mtime_ms)`` tuples in **ascending
            priority** -- later entries override earlier ones on a path collision
            (case-insensitive, matching Vortex/Windows semantics).

    Returns:
        ``(entries, links)`` where ``entries`` are manifest dicts
        ``{relPath, source, target, time}`` (sorted for determinism) and ``links``
        is the winning ``(source_folder, rel_path)`` list to hard-link.
    """
    winners: Dict[str, Tuple[str, str, int]] = {}
    for folder, rel, mtime in staged:
        nrel = _to_backslash(rel)
        winners[nrel.lower()] = (folder, nrel, mtime)

    entries: List[Dict[str, Any]] = []
    links: List[Tuple[str, str]] = []
    for folder, nrel, mtime in winners.values():
        entries.append({"relPath": nrel, "source": folder, "target": "", "time": mtime})
        links.append((folder, nrel))
    entries.sort(key=lambda e: e["relPath"].lower())
    links.sort(key=lambda fl: fl[1].lower())
    return entries, links


_WSL_MOUNT_RE = re.compile(r"^/mnt/([a-zA-Z])(/.*)?$")


def to_windows_path(p: str) -> str:
    """Translate a WSL mount path (``/mnt/<d>/...``) to Windows form (``<D>:\\...``).

    Vortex is a Windows app and compares its configured staging/target folders
    against the manifest's ``stagingPath``/``targetPath`` as plain strings, so the
    manifest MUST hold Windows paths even when the deploy is run from WSL (where
    these are ``/mnt/...`` mounts). Already-Windows or relative paths pass through
    unchanged, so this is a no-op when run natively on Windows.
    """
    m = _WSL_MOUNT_RE.match(p)
    if not m:
        return p
    drive = m.group(1).upper()
    rest = (m.group(2) or "").replace("/", "\\")
    return f"{drive}:{rest}"


def build_manifest(instance_id: str, game_id: str, staging_path: str, target_path: str,
                   entries: List[Dict[str, Any]], *, deployment_time_ms: int = 0) -> Dict[str, Any]:
    """Wrap manifest entries in the exact shape Vortex 2.0.x writes.

    ``stagingPath``/``targetPath`` are always normalized to Windows form (see
    :func:`to_windows_path`) so a WSL-run deploy doesn't leave ``/mnt/...`` paths
    that Vortex can't match against its config.
    """
    return {
        "instance": instance_id,
        "version": MANIFEST_VERSION,
        "deploymentMethod": DEPLOY_METHOD,
        "gameId": game_id,
        "deploymentTime": deployment_time_ms,
        "stagingPath": to_windows_path(staging_path),
        "targetPath": to_windows_path(target_path),
        "files": entries,
    }


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #
def read_instance_id(staging_dir: str, target_data_dir: str) -> Optional[str]:
    """Find the live Vortex instance id (existing manifest first, then marker)."""
    manifest = os.path.join(target_data_dir, MANIFEST_NAME)
    if os.path.isfile(manifest):
        try:
            with open(manifest, "r", encoding="utf-8") as fh:
                inst = json.load(fh).get("instance")
            if inst:
                return inst
        except (OSError, ValueError):
            pass
    marker = os.path.join(staging_dir, STAGING_MARKER)
    if os.path.isfile(marker):
        try:
            with open(marker, "r", encoding="utf-8") as fh:
                return json.load(fh).get("instance")
        except (OSError, ValueError):
            pass
    return None


def write_staging_tag(staging_dir: str, instance_id: str, game_id: str = GAME_ID) -> None:
    """Write Vortex's ``__vortex_staging_folder`` marker into the staging root.

    Vortex creates this to mark a folder as its own; staging we build ourselves
    lacks it, so Vortex warns "Mod Staging Folder invalid" on launch. The format
    mirrors ``writeStagingTag`` in Vortex's stagingDirectory.ts exactly:
    ``{"instance": <app.instanceId>, "game": <gameId>}``. Skip silently if we
    don't have an instance id (writing a bad one is worse than no marker)."""
    if not instance_id or not os.path.isdir(staging_dir):
        return
    marker = os.path.join(staging_dir, STAGING_MARKER)
    try:
        with open(marker, "w", encoding="utf-8") as fh:
            json.dump({"instance": instance_id, "game": game_id}, fh)
    except OSError:
        pass


def _hardlink(src: str, dst: str) -> None:
    """Hard-link ``src`` to ``dst`` (replacing any existing target).

    Falls back to a copy when a hard link isn't possible (e.g. the staging and
    game folders live on different volumes).
    """
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    # If dst is already the SAME physical file (a hard link laid down by a prior
    # deploy), there's nothing to do -- and trying to copy a file onto itself
    # raises shutil.SameFileError. This is the common case on a re-deploy.
    try:
        if os.path.exists(dst) and os.path.samefile(src, dst):
            return
    except OSError:
        pass
    if os.path.lexists(dst):
        _force_remove(dst)
    try:
        os.link(src, dst)
    except OSError:
        try:
            shutil.copy2(src, dst)
        except shutil.SameFileError:
            pass        # already the same content/inode; leave it
        except PermissionError:
            # dst still there and read-only -> clear the attribute and overwrite.
            _force_remove(dst)
            shutil.copy2(src, dst)


def _force_remove(path: str) -> None:
    """Remove ``path``, clearing a read-only attribute first if needed.

    Steam ships some Data files read-only and prior deploys can leave links
    read-only; on Windows ``os.remove`` then raises ``PermissionError`` (Errno
    13). Clearing the write bit and retrying lets a re-deploy replace them
    instead of aborting the whole run on one file."""
    try:
        os.remove(path)
        return
    except FileNotFoundError:
        return
    except PermissionError:
        pass
    except OSError:
        return
    try:
        os.chmod(path, stat.S_IWRITE)
        os.remove(path)
    except OSError:
        pass


def _walk_one(staging_dir: str, folder: str) -> List[Tuple[str, str, int]]:
    """Collect ``(folder, rel_path, mtime_ms)`` for every file under one folder."""
    out: List[Tuple[str, str, int]] = []
    root = os.path.join(staging_dir, folder)
    if not os.path.isdir(root):
        return out
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            try:
                mtime_ms = int(os.path.getmtime(full) * 1000)
            except OSError:
                mtime_ms = 0
            out.append((folder, rel, mtime_ms))
    return out


def _walk_staged(staging_dir: str, folders: Iterable[str],
                 workers: int = 1) -> List[Tuple[str, str, int]]:
    """Collect ``(folder, rel_path, mtime_ms)`` for every file under each folder.

    The per-folder walk is latency-bound over network/9p mounts, so ``workers > 1``
    fans the folders out across a thread pool (the os calls release the GIL). Order
    is preserved -- results come back in the input ``folders`` order, which
    :func:`resolve_deployment` relies on for conflict-winner priority.
    """
    folders = list(folders)
    if workers and workers > 1 and len(folders) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as ex:
            per_folder = list(ex.map(lambda f: _walk_one(staging_dir, f), folders))
        return [t for sub in per_folder for t in sub]
    return [t for folder in folders for t in _walk_one(staging_dir, folder)]


def read_manifest_entries(target_data_dir: str) -> Dict[str, Dict[str, Any]]:
    """Read an existing manifest's file entries, keyed by lower-cased relPath.

    Returns ``{}`` when no (or an unreadable) manifest exists. Used by the
    incremental deploy to keep every already-tracked file instead of rebuilding
    the whole manifest from a full staging walk.
    """
    manifest = os.path.join(target_data_dir, MANIFEST_NAME)
    if not os.path.isfile(manifest):
        return {}
    try:
        with open(manifest, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for e in data.get("files", []):
        rel = e.get("relPath")
        if rel:
            out[rel.lower()] = e
    return out


@dataclass
class DeployResult:
    files: int
    manifest_path: str
    instance_id: str
    linked: int
    skipped_conflicts: int = 0   # changed-folder files left to an existing higher
                                 # -priority winner (incremental mode only)


def deploy(staging_dir: str, target_data_dir: str, enabled_folders: Iterable[str],
           game_id: str = GAME_ID, *, instance_id: Optional[str] = None,
           link: bool = True, deployment_time_ms: int = 0,
           workers: int = 1, only_folders: Optional[Iterable[str]] = None,
           progress: Optional[Callable[[int, int, str], None]] = None) -> DeployResult:
    """Hard-link staged files into the game folder and write the deployment manifest.

    ``enabled_folders`` must be in **load order** (ascending priority) so that, on
    a file conflict, the higher-priority mod wins -- the manifest always matches
    whatever ends up on disk, so Vortex stays consistent either way.

    ``only_folders`` switches to **incremental** mode: only those folders are
    walked + linked, and their files are MERGED into the existing manifest (every
    other already-tracked file is preserved). This turns a one-mod reinstall into
    a delta instead of a full 684k-file redeploy. A changed file wins unless a
    DIFFERENT folder already owns that path in the manifest (a real cross-mod
    conflict) -- those are left to the existing winner and counted, since resolving
    them correctly needs the full load order (run a full deploy for that).

    ``workers > 1`` parallelizes the (latency-bound) file walk and hard-linking
    across a thread pool. Conflict resolution still happens centrally on the
    ordered walk, so each link target is unique -- the links can run in any order.

    ``progress(done, total, rel_path)`` is called (throttled, thread-safe) as files
    are hard-linked, so a GUI can show live deploy progress.
    """
    instance_id = instance_id or read_instance_id(staging_dir, target_data_dir)
    if not instance_id:
        raise RuntimeError(
            "Could not determine the Vortex instance id (no deployment manifest "
            f"or {STAGING_MARKER} marker found). Open Vortex for this game once.")

    skipped_conflicts = 0
    if only_folders is not None:
        # Incremental: walk only the changed folders, merge into the live manifest.
        changed = list(only_folders)
        staged = _walk_staged(staging_dir, changed, workers=workers)
        new_entries, _ = resolve_deployment(staged)
        existing = read_manifest_entries(target_data_dir)
        merged = dict(existing)
        links = []
        for e in new_entries:
            key = e["relPath"].lower()
            cur = merged.get(key)
            if cur is None or cur.get("source") == e["source"]:
                merged[key] = e
                links.append((e["source"], e["relPath"]))
            else:
                skipped_conflicts += 1
        entries = sorted(merged.values(), key=lambda x: x["relPath"].lower())
        links.sort(key=lambda fl: fl[1].lower())
    else:
        folders = list(enabled_folders)
        staged = _walk_staged(staging_dir, folders, workers=workers)
        entries, links = resolve_deployment(staged)

    linked = 0
    if link:
        total = len(links)
        from threading import Lock
        lock, done = Lock(), [0]

        failures: List[Tuple[str, str]] = []

        def _link_one(fl):
            folder, nrel = fl
            sub = nrel.replace("\\", os.sep)
            try:
                _hardlink(os.path.join(staging_dir, folder, sub),
                          os.path.join(target_data_dir, sub))
            except OSError as ex:
                # One unrecoverable file (locked by a running game, ACL-denied)
                # must not abort the whole deploy. Record it and move on.
                with lock:
                    failures.append((nrel, str(ex)))
            if progress is not None:
                with lock:
                    done[0] += 1
                    if done[0] % 200 == 0 or done[0] == total:
                        progress(done[0], total, nrel)

        if workers and workers > 1 and len(links) > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(_link_one, links))
        else:
            for fl in links:
                _link_one(fl)
        linked = total - len(failures)
        if failures:
            shown = "; ".join(f"{r} ({e})" for r, e in failures[:5])
            print(f"WARNING: {len(failures)} of {total} files could not be linked "
                  f"(first few: {shown})")

    manifest = build_manifest(instance_id, game_id, staging_dir, target_data_dir,
                              entries, deployment_time_ms=deployment_time_ms)
    manifest_path = os.path.join(target_data_dir, MANIFEST_NAME)
    tmp = manifest_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    os.replace(tmp, manifest_path)

    return DeployResult(len(entries), manifest_path, instance_id, linked,
                        skipped_conflicts=skipped_conflicts)


def mark_deployed_in_db(db_path: str, game_id: str = GAME_ID, *, node: str = "node"):
    """Bump ``deploymentCounter`` and clear ``needToDeploy`` so Vortex sees the
    deployment as current (requires Vortex closed -- guarded by ``vortex_db``)."""
    from utils import vortex_db

    counter_key = f"persistent###deployment###deploymentCounter###{game_id}"
    need_key = f"persistent###deployment###needToDeploy###{game_id}"
    current = vortex_db.read_prefix(db_path, counter_key, node=node).get(counter_key, 0)
    if not isinstance(current, int):
        current = 0
    records = {
        counter_key: json.dumps(current + 1),
        need_key: json.dumps(False),
    }
    return vortex_db.write_records(db_path, records, backup=True, node=node)


def order_folders_for_deploy(staging_dir: str, collection: Optional[Dict[str, Any]],
                             folder_by_modid: Optional[Dict[str, list]] = None
                             ) -> list:
    """Resolve the staging folders into deploy order via the collection's modRules.

    Falls back to sorted folder order when no collection/rules are available. The
    returned order is ascending priority (later folders win file conflicts), which
    is what the collection author's "after" rules intend.
    """
    folders = sorted(d for d in os.listdir(staging_dir)
                     if os.path.isdir(os.path.join(staging_dir, d)))
    if not collection:
        return folders

    from utils import vortex_loadorder as lo
    from utils.vortex_sync import index_by_modid
    if folder_by_modid is None:
        folder_by_modid = index_by_modid(folders)
    resolve = lo.build_mod_resolver(collection.get("mods", []), folder_by_modid)
    return lo.order_mods(folders, collection.get("modRules", []), resolve)


def deploy_collection(db_path: str, staging_dir: str, target_data_dir: str,
                      enabled_folders: Optional[Iterable[str]] = None,
                      game_id: str = GAME_ID, *, collection: Optional[Dict[str, Any]] = None,
                      node: str = "node", deployment_time_ms: int = 0,
                      workers: int = 1, only_folders: Optional[Iterable[str]] = None,
                      progress: Optional[Callable[[int, int, str], None]] = None
                      ) -> Tuple[DeployResult, Any]:
    """High-level: hard-link + write manifest, then mark the deployment in the DB.

    Deploy order (which decides file-conflict winners) comes from the collection's
    modRules when ``collection`` is supplied; otherwise from an explicit
    ``enabled_folders`` list, else sorted folder order.

    ``only_folders`` runs an incremental deploy (see :func:`deploy`) -- just those
    folders are linked + merged into the live manifest, instead of a full redeploy.
    """
    if enabled_folders is None:
        enabled_folders = order_folders_for_deploy(staging_dir, collection)
    # Use Vortex's authoritative app.instanceId for the manifest so Vortex accepts
    # our deployment as its own instead of purging + re-linking everything.
    from utils import vortex_db
    instance_id = None
    try:
        instance_id = vortex_db.read_app_instance_id(db_path, node=node)
    except Exception:
        instance_id = None
    # Stamp the staging marker so Vortex stops warning "Mod Staging Folder
    # invalid" (our staging is built by us, not Vortex, so it lacks the tag).
    write_staging_tag(staging_dir, instance_id, game_id)
    result = deploy(staging_dir, target_data_dir, enabled_folders, game_id,
                    instance_id=instance_id,
                    deployment_time_ms=deployment_time_ms, workers=workers,
                    only_folders=only_folders, progress=progress)
    db_write = mark_deployed_in_db(db_path, game_id, node=node)
    return result, db_write


@dataclass
class FinalizeResult:
    deploy: DeployResult
    loadorder_path: str
    plugins_path: str
    active_plugins: int
    esl_flagged: int = 0          # plugins auto-flagged light (ESL) this run


def finalize_collection(db_path: str, collection: Dict[str, Any], staging_dir: str,
                        game_data_dir: str, localappdata_dir: str,
                        game_id: str = GAME_ID, *, node: str = "node",
                        deployment_time_ms: int = 0, backup: bool = True,
                        workers: int = 1, eslify: bool = True,
                        only_folders: Optional[Iterable[str]] = None,
                        progress: Optional[Callable[[int, int, str], None]] = None) -> FinalizeResult:
    """Full post-install pipeline: order mods -> deploy -> sort plugins.

    Resolves deploy order from ``collection.modRules``, hard-links + writes the
    deployment manifest, marks the DB deployed, then writes loadorder.txt /
    plugins.txt from ``collection.pluginRules`` (masters-first; existing files
    backed up). ``localappdata_dir`` is the game's ``%LOCALAPPDATA%/<Game>`` dir.

    ``only_folders`` runs an incremental deploy (delta-merge into the live
    manifest) instead of a full redeploy -- for reinstalling a handful of mods.
    ``workers > 1`` parallelizes the deploy's file walk + hard-linking.
    """
    from utils import vortex_loadorder as lo
    from utils import esl_flagging as esl

    result, _ = deploy_collection(db_path, staging_dir, game_data_dir, game_id=game_id,
                                  collection=collection, node=node,
                                  deployment_time_ms=deployment_time_ms, workers=workers,
                                  only_folders=only_folders, progress=progress)

    # Auto-flag eligible plugins as light (ESL) -- our equivalent of Vortex's
    # "mark could-be-light as light". Without it the full-plugin count blows past
    # Skyrim's 254 cap and the game won't launch. Runs before the sort so the
    # load order reflects the newly-light masters.
    esl_flagged = 0
    if eslify:
        enabled = lo.enabled_map(collection)
        active_names = [n for n in lo.scan_plugins(game_data_dir)
                        if n.lower() in enabled] if enabled else lo.scan_plugins(game_data_dir)
        eres = esl.mark_eligible_as_light(game_data_dir, active_names,
                                          workers=max(8, workers))
        esl_flagged = eres.flagged

    lo_path, pl_path, active = lo.sort_plugins(collection, game_data_dir,
                                               localappdata_dir, backup=backup)
    return FinalizeResult(result, lo_path, pl_path, active, esl_flagged=esl_flagged)
