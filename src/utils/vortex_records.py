"""
Builders for Vortex state.v2 records.

Pure functions that turn a collection.json mod entry + on-disk facts into the
leaf-key/value structure Vortex expects. Kept free of any I/O so they can be unit
tested against :mod:`utils.vortex_schema`. The actual LevelDB write is done
separately (see :mod:`utils.vortex_db`).

Each builder returns ``(base_key, relative_leaves)`` where ``relative_leaves`` is
a flat ``{dotted.relative.path: value}`` dict (the same shape
``vortex_schema.validate_record`` checks). Use :func:`to_absolute` to expand into
the ``###``-joined, JSON-encoded key/value pairs the DB stores.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Tuple

from utils.vortex_schema import P, GAME_ID, NEXUS_DOMAIN


def _flatten(tree: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    """Flatten nested dicts to dotted leaf paths; arrays/scalars stay as values."""
    out: Dict[str, Any] = {}
    for k, v in tree.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        else:
            out[key] = v
    return out


def build_download(source: Dict[str, Any], mod_name: str,
                   archive_name: str, dl_id: str) -> Tuple[str, Dict[str, Any]]:
    """Build a ``persistent.downloads.files.<id>`` record (state: finished)."""
    mod_id, file_id, md5 = source["modId"], source["fileId"], source.get("md5", "")
    tree = {
        "chunks": [], "urls": [], "state": "finished", "source": "nexus",
        "fileMD5": md5, "fileTime": source.get("fileTime", 0),
        "game": [GAME_ID], "localPath": archive_name, "size": source.get("fileSize", 0),
        "modInfo": {
            "source": "nexus",
            "meta": {
                "fileName": archive_name, "fileMD5": md5, "gameId": GAME_ID,
                "domainName": NEXUS_DOMAIN, "source": "nexus",
                "logicalFileName": source.get("logicalFilename") or mod_name,
                "fileSizeBytes": source.get("fileSize", 0), "status": "published",
                "sourceURI": f"nxm://{NEXUS_DOMAIN}/mods/{mod_id}/files/{file_id}",
                "details": {"modId": str(mod_id), "fileId": str(file_id)},
            },
            "nexus": {"ids": {"modId": mod_id, "fileId": file_id, "gameId": NEXUS_DOMAIN}},
        },
    }
    base = f"persistent{P}downloads{P}files{P}{dl_id}"
    return base, _flatten(tree)


def build_mod(source: Dict[str, Any], mod: Dict[str, Any], folder: str,
              archive_id: str, archive_name: str) -> Tuple[str, Dict[str, Any]]:
    """Build a ``persistent.mods.skyrimse.<folder>`` installed-mod record."""
    tree = {
        "id": folder, "installationPath": folder, "state": "installed", "type": "",
        "archiveId": archive_id, "fileOverrides": [],
        "attributes": {
            "source": "nexus", "downloadGame": GAME_ID, "endorsed": "Undecided",
            "modId": source["modId"], "fileId": source["fileId"],
            "fileMD5": source.get("md5", ""), "fileName": archive_name,
            "fileSize": source.get("fileSize", 0),
            "logicalFileName": source.get("logicalFilename") or mod.get("name", ""),
            "name": folder, "customFileName": mod.get("name", ""),
            "version": str(mod.get("version", "")),
            "modVersion": str(mod.get("version", "")),
            "referenceTag": source.get("tag", ""),     # links to a collection rule
            "author": mod.get("author", ""),
            "category": (mod.get("details") or {}).get("category", ""),
        },
    }
    base = f"persistent{P}mods{P}{GAME_ID}{P}{folder}"
    return base, _flatten(tree)


def build_profile_modstate(profile_id: str, folder: str) -> Tuple[str, Dict[str, Any]]:
    """Build a ``persistent.profiles.<id>.modState.<folder>`` enable entry."""
    base = f"persistent{P}profiles{P}{profile_id}{P}modState{P}{folder}"
    return base, _flatten({"enabled": True, "enabledTime": 0})


def to_absolute(base: str, relative_leaves: Dict[str, Any]) -> Dict[str, str]:
    """Expand ``(base, {dotted.rel: value})`` into ``{full###key: json_value}``."""
    out: Dict[str, str] = {}
    for rel, val in relative_leaves.items():
        full = base + P + rel.replace(".", P)
        out[full] = json.dumps(val)
    return out
