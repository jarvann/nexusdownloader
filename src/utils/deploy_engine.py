"""Mod-type-aware deployment engine.

Vortex deploys each mod TYPE to its own target path with its own manifest. We had a
flat "everything -> Data" model plus a root-file heuristic bolted on; this is the
proper structure. The engine classifies each staged mod (:mod:`utils.modtypes`),
groups by type, and runs the existing activator (:func:`utils.vortex_deploy.deploy`)
once per type against that type's target + manifest:

* default type ('')   -> the game Data folder, ``vortex.deployment.json``
* skse / dinput / ... -> the GAME ROOT,        ``vortex.deployment.<type>.json``

A root mod is staged WHOLE (loader/dll + its ``Data/`` subfolder) and deployed to
the game root; since ``<game_root>/Data`` is the Data folder, every file lands where
it belongs with no per-file special-casing. Purge walks every type's manifest.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from utils import modtypes
from utils import vortex_deploy as vd
from utils.vortex_schema import GAME_ID


def classify_folder(staging_dir: str, folder: str) -> modtypes.ModType:
    """Classify a staged mod by its top-level entry names."""
    try:
        names = os.listdir(os.path.join(staging_dir, folder))
    except OSError:
        names = []
    return modtypes.classify(names)


def group_by_type(staging_dir: str, folders: Iterable[str]
                  ) -> "OrderedDict[str, Tuple[modtypes.ModType, List[str]]]":
    """Group folders by modtype id, preserving the input (load) order within each."""
    groups: "OrderedDict[str, Tuple[modtypes.ModType, List[str]]]" = OrderedDict()
    for folder in folders:
        mt = classify_folder(staging_dir, folder)
        groups.setdefault(mt.type_id, (mt, []))[1].append(folder)
    return groups


def _type_order(groups) -> List[str]:
    """Deploy default first, then game-root types (so a root mod's Data/ files win
    over a regular mod's same path -- e.g. SKSE's own scripts are authoritative)."""
    return sorted(groups.keys(), key=lambda tid: modtypes.is_root_type(tid))


def deploy_all(staging_dir: str, data_dir: str, game_root: Optional[str],
               enabled_folders: Iterable[str], *, instance_id: Optional[str] = None,
               game_id: str = GAME_ID, workers: int = 1, deployment_time_ms: int = 0,
               only_folders: Optional[Iterable[str]] = None,
               log: Optional[Callable[[str, str], None]] = None,
               progress: Optional[Callable[[int, int, str], None]] = None,
               activity: Optional[Callable[[int, str], None]] = None
               ) -> Dict[str, vd.DeployResult]:
    """Deploy every modtype to its target. Returns ``{type_id: DeployResult}``.

    A game-root type with no known ``game_root`` is skipped (its files stay in
    staging) and logged, rather than misplaced into Data.
    """
    folders = list(enabled_folders)
    instance_id = instance_id or vd.read_instance_id(staging_dir, data_dir)
    groups = group_by_type(staging_dir, folders)
    only = set(only_folders) if only_folders is not None else None
    results: Dict[str, vd.DeployResult] = {}
    for type_id in _type_order(groups):
        mt, type_folders = groups[type_id]
        target = mt.target(data_dir, game_root)
        if target is None:
            if log:
                log("WARNING", f"modtype '{type_id}': {len(type_folders)} mod(s) need "
                               f"the game root but it's unknown -- left in staging")
            continue
        sub_only = ([f for f in type_folders if f in only]
                    if only is not None else None)
        if only is not None and not sub_only:
            continue
        os.makedirs(target, exist_ok=True)
        res = vd.deploy(staging_dir, target, type_folders, game_id,
                        instance_id=instance_id, workers=workers,
                        deployment_time_ms=deployment_time_ms,
                        manifest_name=mt.manifest_name(),
                        only_folders=sub_only, progress=progress, activity=activity)
        results[type_id] = res
        if log:
            log("INFO", f"modtype '{type_id or 'default'}' -> {target}: "
                        f"{res.files} files, {res.removed_stale} stale removed")
    return results


def purge_all(staging_dir: str, data_dir: str, game_root: Optional[str], *,
              game_id: str = GAME_ID, force: bool = False, workers: int = 1,
              only_folders: Optional[Iterable[str]] = None,
              progress: Optional[Callable[[int, int, str], None]] = None
              ) -> Dict[str, vd.PurgeResult]:
    """Purge every modtype's manifest from its target. Returns ``{type_id: PurgeResult}``."""
    results: Dict[str, vd.PurgeResult] = {}
    for mt in modtypes.REGISTRY:
        target = mt.target(data_dir, game_root)
        if target is None or not os.path.isdir(target):
            continue
        manifest_path = os.path.join(target, mt.manifest_name())
        if not os.path.isfile(manifest_path):
            continue
        res = vd.purge(staging_dir, target, game_id, force=force, workers=workers,
                       only_folders=only_folders, manifest_name=mt.manifest_name(),
                       progress=progress)
        results[mt.type_id] = res
    return results
