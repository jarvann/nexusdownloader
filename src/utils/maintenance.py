"""Shared maintenance operations: purge deployment, and reset the install to a
clean 'downloaded, not installed' baseline.

One implementation used by BOTH the reset_install.py CLI and the GUI's
maintenance buttons (DRY). All operations are explicit -- nothing here runs
unless called with ``apply=True``; the dry-run path just reports what would
happen so the caller can confirm first.
"""

from __future__ import annotations

import glob
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Lock
from typing import Callable, List, Optional, Sequence

from utils import local_state as ls

try:
    from utils.unified_logging import get_logger
    _logger = get_logger("maintenance")
except Exception:  # logging must never block a maintenance op
    import logging as _logging
    _logger = _logging.getLogger("nexusdownloader.maintenance")


def collection_container(staging: str) -> Optional[str]:
    """Name of the staging subfolder that holds collection.json (preserved on reset)."""
    for d in glob.glob(os.path.join(staging, "*", "collection.json")):
        return os.path.basename(os.path.dirname(d))
    return None


def staging_mod_folders(staging: str, keep: set) -> List[str]:
    """Mod folders eligible for deletion (excludes __vortex markers + ``keep``)."""
    out: List[str] = []
    if not os.path.isdir(staging):
        return out
    for d in sorted(os.listdir(staging)):
        full = os.path.join(staging, d)
        if not os.path.isdir(full) or d.startswith("__vortex") or d in keep:
            continue
        out.append(d)
    return out


@dataclass
class ResetPlan:
    staging: str
    container: Optional[str]
    folders: List[str]
    ledger_mods: int
    db_path: str
    purge_deploy: bool
    game_data: str


@dataclass
class ResetResult:
    purged: int = 0
    deleted: int = 0
    ledger_reset: bool = False
    errors: List[str] = field(default_factory=list)


def plan_reset(staging: str, *, game_data: str = "", purge_deploy: bool = False,
               extra_keep: Sequence[str] = ()) -> ResetPlan:
    """Compute what a reset would touch, without changing anything."""
    container = collection_container(staging)
    keep = set(extra_keep) | ({container} if container else set())
    folders = staging_mod_folders(staging, keep)
    db_path = ls.db_path_for(staging)
    ledger_mods = 0
    if os.path.exists(db_path):
        ledger_mods = len(ls.get_ledger(db_path).all_mods())
    return ResetPlan(staging=staging, container=container, folders=folders,
                     ledger_mods=ledger_mods, db_path=db_path,
                     purge_deploy=purge_deploy, game_data=game_data)


def purge_deployment(staging: str, game_data: str, *, force: bool = True,
                     workers: int = 16, progress: Optional[Callable] = None):
    """Remove our deployed hardlinks from the game's Data folder (Vortex parity)."""
    from utils import vortex_deploy as vd
    return vd.purge(staging, game_data, force=force, workers=workers, progress=progress)


def _rm_root(full: str) -> Optional[str]:
    """Remove a now-emptied staging mod folder skeleton; error string else None."""
    try:
        shutil.rmtree(full)
    except FileNotFoundError:
        pass
    except Exception as e:  # collected + reported to the caller, never fatal
        return f"could not remove {os.path.basename(full)}: {e}"
    return None


def run_reset(plan: ResetPlan, *, log: Callable[[str], None] = lambda _m: None,
              progress: Optional[Callable] = None, workers: int = 16) -> ResetResult:
    """Execute a previously computed :class:`ResetPlan`.

    Deletes the staging mod folders (downloads live elsewhere and are untouched),
    optionally purges the deployment first, then resets the ledger's install state
    while preserving the downloads + endorsements.

    The wipe is done at *file* granularity: every file under the target folders is
    enumerated into one flat list and deleted across ``workers`` threads (one task
    per file, mirroring vortex_deploy's linker). The pool auto-balances, so a
    single 10k-file texture mod can't monopolize one worker while the rest idle --
    which is exactly what per-folder parallelism suffered from. The emptied
    directory skeletons are then removed in a cheap parallel pass.
    """
    res = ResetResult()

    if plan.purge_deploy and plan.game_data:
        try:
            log(f"Purging deployment from {plan.game_data} ...")
            pr = purge_deployment(plan.staging, plan.game_data, force=True,
                                  workers=workers, progress=progress)
            res.purged = getattr(pr, "removed", 0)
            log(f"  removed {res.purged} deployed file(s).")
        except Exception as e:
            res.errors.append(f"deploy purge failed: {e}")
            log(f"  WARNING: {res.errors[-1]} (continuing)")

    total_folders = len(plan.folders)
    roots = [os.path.join(plan.staging, d) for d in plan.folders]
    _logger.info(f"run_reset: staging={plan.staging!r} folders={total_folders} "
                 f"purge_deploy={plan.purge_deploy}")

    if roots:
        # Phase 1 -- enumerate every file under the target folders (flat list).
        log(f"Scanning {total_folders} staging folder(s) for files...")
        files: List[str] = []
        for full in roots:
            for dirpath, _dirnames, filenames in os.walk(full):
                files.extend(os.path.join(dirpath, fn) for fn in filenames)
        total = len(files)

        # Phase 2 -- delete files flat + parallel (pool auto-balances the big mods).
        log(f"Deleting {total} file(s) across up to {workers} threads...")
        lock, done = Lock(), [0]

        def _wipe_one(fp):
            try:
                os.remove(fp)
            except FileNotFoundError:
                pass  # already gone -- fine
            except Exception as e:
                with lock:
                    res.errors.append(f"could not delete {fp}: {e}")
            if progress is not None:
                with lock:
                    done[0] += 1
                    if done[0] % 200 == 0 or done[0] == total:
                        progress(done[0], total, "")

        if workers and workers > 1 and total > 1:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(_wipe_one, files))
        else:
            for fp in files:
                _wipe_one(fp)

        # Phase 3 -- remove the now-empty directory skeletons (cheap, parallel).
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(roots)))) as ex:
            for err in ex.map(_rm_root, roots):
                if err:
                    res.errors.append(err)
                else:
                    res.deleted += 1

        log(f"Deleted {res.deleted}/{total_folders} staging mod folder(s) "
            f"({total} files).")

    if os.path.exists(plan.db_path):
        ledger = ls.get_ledger(plan.db_path)
        ledger.reset_install_state()
        ledger.flush()
        res.ledger_reset = True
        log("Ledger install state reset (downloads + endorsements preserved).")

    if res.errors:
        _logger.warning(f"run_reset: {len(res.errors)} error(s); first: {res.errors[0]}")
    _logger.info(f"run_reset done: deleted={res.deleted}/{total_folders} "
                 f"purged={res.purged} ledger_reset={res.ledger_reset}")
    return res
