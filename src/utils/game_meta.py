"""
Per-game metadata, keyed by Nexus game domain (collection.json ``info.domainName``).

Vortex is game-agnostic: game-specific facts (which masters are always present,
the Creation-Club listing filename, where plugins.txt lives) come from the active
game extension, not hardcoded in the installer. This module is the equivalent
single source of truth so the rest of the tool stops being Skyrim-only.

Currently populated for the Bethesda games the tool targets; unknown domains fall
back to empty/sensible defaults (so a new game degrades gracefully rather than
mis-applying Skyrim assumptions).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


@dataclass(frozen=True)
class GameMeta:
    domain: str                              # Nexus domain (collection info.domainName)
    base_masters: Set[str] = field(default_factory=frozenset)  # always-present, lowercased
    cc_file: str = ""                        # Creation Club listing (e.g. Skyrim.ccc)
    plugins_subdir: str = ""                 # %LOCALAPPDATA%\<this> holds plugins.txt
    # Loaders/injectors that mark a "root" mod for this game (deploy routing).
    root_loaders: Set[str] = field(default_factory=frozenset)


# Vanilla/DLC master sets (lowercased). These are the plugins always active for a
# vanilla install, so FOMOD fileDependency checks against them succeed.
_SKYRIMSE_MASTERS = {
    "skyrim.esm", "update.esm", "dawnguard.esm", "hearthfires.esm",
    "dragonborn.esm", "ccbgssse001-fish.esm", "_resourcepack.esl",
    "ccbgssse037-curios.esl", "ccqdrsse001-survivalmode.esl",
}
_SKYRIM_LE_MASTERS = {
    "skyrim.esm", "update.esm", "dawnguard.esm", "hearthfires.esm", "dragonborn.esm",
}
_FALLOUT4_MASTERS = {
    "fallout4.esm", "dlcrobot.esm", "dlcworkshop01.esm", "dlccoast.esm",
    "dlcworkshop02.esm", "dlcworkshop03.esm", "dlcnukaworld.esm",
    "dlcultrahighresolution.esm",
}
_FALLOUTNV_MASTERS = {
    "falloutnv.esm", "deadmoney.esm", "honesthearts.esm", "oldworldblues.esm",
    "lonesomeroad.esm", "gunrunnersarsenal.esm", "classicpack.esm",
    "mercenarypack.esm", "tribalpack.esm", "caravanpack.esm",
}
_OBLIVION_MASTERS = {
    "oblivion.esm", "knights.esp", "dlcshiveringisles.esp",
}
_STARFIELD_MASTERS = {
    "starfield.esm", "blueprintships-starfield.esm", "constellation.esm", "oldmars.esm",
}
_MORROWIND_MASTERS = {"morrowind.esm", "tribunal.esm", "bloodmoon.esm"}

_REGISTRY: Dict[str, GameMeta] = {
    "skyrimspecialedition": GameMeta(
        "skyrimspecialedition", _SKYRIMSE_MASTERS, "Skyrim.ccc",
        "Skyrim Special Edition",
        {"skse64_loader.exe", "skse_loader.exe", "dinput8.dll"}),
    "skyrim": GameMeta(
        "skyrim", _SKYRIM_LE_MASTERS, "", "Skyrim",
        {"skse_loader.exe", "dinput8.dll"}),
    "skyrimvr": GameMeta(
        "skyrimvr", _SKYRIMSE_MASTERS, "", "Skyrim VR",
        {"sksevr_loader.exe", "dinput8.dll"}),
    "fallout4": GameMeta(
        "fallout4", _FALLOUT4_MASTERS, "Fallout4.ccc", "Fallout4",
        {"f4se_loader.exe", "dinput8.dll"}),
    "falloutnv": GameMeta(
        "falloutnv", _FALLOUTNV_MASTERS, "", "FalloutNV",
        {"nvse_loader.exe", "dinput8.dll"}),
    "oblivion": GameMeta(
        "oblivion", _OBLIVION_MASTERS, "", "Oblivion",
        {"obse_loader.exe", "dinput8.dll"}),
    "starfield": GameMeta(
        "starfield", _STARFIELD_MASTERS, "Starfield.ccc", "Starfield",
        {"sfse_loader.exe", "dinput8.dll"}),
    "morrowind": GameMeta("morrowind", _MORROWIND_MASTERS, "", "Morrowind", set()),
}

_DEFAULT = GameMeta("", frozenset(), "", "", {"dinput8.dll"})


def get(domain: Optional[str]) -> GameMeta:
    """Metadata for a Nexus game domain; a permissive default for unknown games."""
    return _REGISTRY.get((domain or "").strip().lower(), _DEFAULT)


def base_masters(domain: Optional[str]) -> Set[str]:
    """Lowercased always-active master plugins for the game (vanilla + DLC)."""
    return set(get(domain).base_masters)


def collection_domain(collection: Dict) -> str:
    """Nexus domain a collection targets (collection.json info.domainName)."""
    return ((collection or {}).get("info") or {}).get("domainName", "") or ""


def active_plugin_set(collection: Dict) -> Set[str]:
    """The plugins that will be active for a collection install: the game's base
    masters plus every plugin the collection declares. This is the set FOMOD
    fileDependency conditions are evaluated against (matches Vortex asking the
    host for the active plugin list)."""
    out = base_masters(collection_domain(collection))
    for p in (collection or {}).get("plugins", []):
        n = (p.get("name") or "").strip().lower()
        if n:
            out.add(n)
    return out
