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
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

from utils import local_state as ls


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


def run_reset(plan: ResetPlan, *, log: Callable[[str], None] = lambda _m: None,
              progress: Optional[Callable] = None) -> ResetResult:
    """Execute a previously computed :class:`ResetPlan`.

    Deletes the staging mod folders (downloads live elsewhere and are untouched),
    optionally purges the deployment first, then resets the ledger's install state
    while preserving the downloads + endorsements.
    """
    res = ResetResult()

    if plan.purge_deploy and plan.game_data:
        try:
            log(f"Purging deployment from {plan.game_data} ...")
            pr = purge_deployment(plan.staging, plan.game_data, force=True, progress=progress)
            res.purged = getattr(pr, "removed", 0)
            log(f"  removed {res.purged} deployed file(s).")
        except Exception as e:
            res.errors.append(f"deploy purge failed: {e}")
            log(f"  WARNING: {res.errors[-1]} (continuing)")

    total = len(plan.folders)
    for i, d in enumerate(plan.folders, 1):
        full = os.path.join(plan.staging, d)
        try:
            shutil.rmtree(full)
            res.deleted += 1
        except Exception as e:
            res.errors.append(f"could not delete {d}: {e}")
        if progress:
            progress(i, total, d)
    log(f"Deleted {res.deleted}/{total} staging mod folders.")

    if os.path.exists(plan.db_path):
        ledger = ls.get_ledger(plan.db_path)
        ledger.reset_install_state()
        ledger.flush()
        res.ledger_reset = True
        log("Ledger install state reset (downloads + endorsements preserved).")
    return res
