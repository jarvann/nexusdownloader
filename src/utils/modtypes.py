"""Mod-type registry -- the abstraction that decides WHERE a mod deploys.

Mirrors Vortex's ``IModType`` (gamemode_management/types/IModType.ts): each type
has a ``typeId``, a ``priority``, a ``test`` over the mod's top-level files, and a
deploy ``target``. The engine groups staged mods by type and deploys each group to
its target with its own manifest -- exactly how Vortex handles root files instead
of the brittle "copy loose dll to game root" heuristic we had.

The decisive trick from Vortex: a root mod (script-extender / dinput / engine
injector) is staged WHOLE -- its loose binaries AND its ``Data/`` subfolder -- and
the whole tree deploys to the GAME ROOT. Because ``<game_root>/Data`` *is* the Data
folder, ``skse64_loader.exe`` lands at the root and ``Data/SKSE/Plugins/*`` lands in
Data, with no per-file special-casing. So a root type sets ``strip_to_data=False``
(keep the full archive structure in staging) and ``target=game_root``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, FrozenSet, List, Optional, Set


@dataclass(frozen=True)
class ModType:
    type_id: str                    # "" = default (Data); matches Vortex's typeId
    priority: int                   # higher wins when multiple tests match
    to_game_root: bool              # True -> deploy to game root, else Data
    strip_to_data: bool             # whether install strips to the Data subfolder
    _test: Callable[[Set[str]], bool]

    def matches(self, top_level_lower: Set[str]) -> bool:
        return self._test(top_level_lower)

    def target(self, data_dir: str, game_root: Optional[str]) -> Optional[str]:
        """Deploy target for this type. None if a game-root type has no known root
        (caller should then skip/keep-in-staging rather than misplace into Data)."""
        if self.to_game_root:
            return game_root
        return data_dir

    def manifest_name(self) -> str:
        """Per-type manifest file, matching Vortex's ``vortex.deployment.<tag>json``."""
        return ("vortex.deployment.json" if not self.type_id
                else f"vortex.deployment.{self.type_id}.json")


# Trigger filenames (lower-case), checked against a mod's top-level entries.
_SKSE_LOADERS: FrozenSet[str] = frozenset(
    {"skse64_loader.exe", "skse_loader.exe", "sksevr_loader.exe"})
_DINPUT: FrozenSet[str] = frozenset({"dinput8.dll"})
# ENB / SSE Engine Fixes Part 2 / common dll wrappers -- loose binaries that load
# beside SkyrimSE.exe. (Vortex leaves several of these to a manual install; we
# route them via a modtype so a collection install just works.)
_ENGINE_INJECTORS: FrozenSet[str] = frozenset({
    "d3dx9_42.dll", "tbbmalloc.dll", "tbbmalloc_proxy.dll",
    "d3d11.dll", "d3d9.dll", "d3dcompiler_46e.dll", "d3dcompiler_42.dll",
    "dxgi.dll", "enbhost.exe", "version.dll", "winmm.dll",
    "binkw64.dll", "xinput1_3.dll",
})
_ROOT_FOLDER = "root"   # an explicit Root/ folder maps onto the game root


def _has_any(names: Set[str], triggers: FrozenSet[str]) -> bool:
    return not names.isdisjoint(triggers)


# Ordered high -> low priority. First match wins; DEFAULT is the catch-all.
SKSE = ModType("skse", 100, to_game_root=True, strip_to_data=False,
               _test=lambda n: _has_any(n, _SKSE_LOADERS))
DINPUT = ModType("dinput", 90, to_game_root=True, strip_to_data=False,
                 _test=lambda n: _has_any(n, _DINPUT))
ENGINE_INJECTOR = ModType("engine-injector", 80, to_game_root=True, strip_to_data=False,
                          _test=lambda n: _has_any(n, _ENGINE_INJECTORS) or _ROOT_FOLDER in n)
DEFAULT = ModType("", 0, to_game_root=False, strip_to_data=True,
                  _test=lambda n: True)

REGISTRY: List[ModType] = sorted(
    [SKSE, DINPUT, ENGINE_INJECTOR, DEFAULT], key=lambda t: -t.priority)


def classify(top_level_names) -> ModType:
    """Pick the mod type from a mod's TOP-LEVEL entry names (any case).

    ``top_level_names`` are the basenames directly under the mod root (the loose
    files + first-level folders). Returns the highest-priority matching type, or
    DEFAULT. Pure -- no I/O; the caller supplies the names from disk or an archive.
    """
    lower = {str(n).lower() for n in top_level_names}
    for mt in REGISTRY:
        if mt.matches(lower):
            return mt
    return DEFAULT


def is_root_type(type_id: str) -> bool:
    for mt in REGISTRY:
        if mt.type_id == type_id:
            return mt.to_game_root
    return False
