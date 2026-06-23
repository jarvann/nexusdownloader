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
                   archive_name: str, dl_id: str, *,
                   folder: str = "", collection_id: Any = None) -> Tuple[str, Dict[str, Any]]:
    """Build a ``persistent.downloads.files.<id>`` record (state: finished).

    ``folder`` (the installed mod's staging folder) adds the ``installed`` link
    that flips the download from "Never Installed" to recognized in Vortex's UI.
    ``collection_id`` records the parent collection so the download groups under it.
    """
    mod_id, file_id, md5 = source["modId"], source["fileId"], source.get("md5", "")
    size = source.get("fileSize", 0)
    logical = source.get("logicalFilename") or mod_name
    nexus: Dict[str, Any] = {
        "ids": {"modId": mod_id, "fileId": file_id, "gameId": NEXUS_DOMAIN},
    }
    if collection_id is not None:
        # Vortex stores this as a string directly under modInfo.nexus -- it's how a
        # collection-sourced download is grouped under its collection.
        nexus["parentCollectionId"] = str(collection_id)
    tree = {
        "id": dl_id, "chunks": [], "pausable": True,
        # The nxm URL (with ?campaign=collection) is what Vortex records; an empty
        # urls array makes it think the file can't be re-fetched.
        "urls": [f"nxm://{NEXUS_DOMAIN}/mods/{mod_id}/files/{file_id}?campaign=collection"],
        "state": "finished", "source": "nexus",
        "fileMD5": md5, "fileTime": source.get("fileTime", 0),
        # Vortex lists every compatible game domain, not just the internal id.
        "game": [GAME_ID, NEXUS_DOMAIN], "localPath": archive_name, "size": size,
        # received == size marks the download fully received/finalized. Without it
        # Vortex 2.0.x treats the record as unfinalized and runs a "Finalizing
        # downloads" pass on startup.
        "received": size,
        "modInfo": {
            "source": "nexus", "game": GAME_ID, "name": logical,
            "referenceTag": source.get("tag", ""),
            "meta": {
                "fileName": archive_name, "fileMD5": md5, "gameId": GAME_ID,
                "domainName": NEXUS_DOMAIN, "source": "nexus",
                "logicalFileName": logical,
                "fileSizeBytes": source.get("fileSize", 0), "status": "published",
                "sourceURI": f"nxm://{NEXUS_DOMAIN}/mods/{mod_id}/files/{file_id}",
                "details": {"modId": str(mod_id), "fileId": str(file_id)},
            },
            "nexus": nexus,
        },
    }
    if folder:
        tree["installed"] = {"gameId": GAME_ID, "modId": folder}
    base = f"persistent{P}downloads{P}files{P}{dl_id}"
    return base, _flatten(tree)


def build_mod(source: Dict[str, Any], mod: Dict[str, Any], folder: str,
              archive_id: str, archive_name: str, *,
              variant: str = "", installed_as_dependency: bool = False,
              install_time: str = "") -> Tuple[str, Dict[str, Any]]:
    """Build a ``persistent.mods.skyrimse.<folder>`` installed-mod record.

    ``variant`` (the collection's display name) and ``installed_as_dependency``
    are what nest a member mod under its collection in the Vortex UI.
    """
    attributes: Dict[str, Any] = {
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
        "installedAsDependency": installed_as_dependency,
        "isPrimary": False,
    }
    # Vortex stores category as a numeric Nexus category id; only emit it when we
    # actually have a number (collection.json often carries a label or nothing).
    cat = (mod.get("details") or {}).get("category")
    if isinstance(cat, bool):
        pass
    elif isinstance(cat, int):
        attributes["category"] = cat
    elif isinstance(cat, str) and cat.isdigit():
        attributes["category"] = int(cat)
    if variant:
        attributes["variant"] = variant
    if install_time:
        attributes["installTime"] = install_time
    # Vortex's InstallDriver stamps installerChoices onto the installed mod
    # (mirrors the collection entry's `choices`). Its keys ("options"/"type") are
    # dot-free so they flatten cleanly. `patches`/`fileList` are intentionally NOT
    # set here: their keys are filenames containing dots, which would corrupt the
    # ###-flattening -- they live faithfully in the rule's JSON blob instead, and
    # association doesn't need them on the mod (reference.tag is the match key).
    if mod.get("choices"):
        attributes["installerChoices"] = mod["choices"]
    tree = {
        "id": folder, "installationPath": folder, "state": "installed", "type": "",
        "archiveId": archive_id, "fileOverrides": [], "attributes": attributes,
    }
    base = f"persistent{P}mods{P}{GAME_ID}{P}{folder}"
    return base, _flatten(tree)


def build_profile_modstate(profile_id: str, folder: str) -> Tuple[str, Dict[str, Any]]:
    """Build a ``persistent.profiles.<id>.modState.<folder>`` enable entry."""
    base = f"persistent{P}profiles{P}{profile_id}{P}modState{P}{folder}"
    return base, _flatten({"enabled": True, "enabledTime": 0})


def build_collection_rule(mod: Dict[str, Any], archive_name: str = "") -> Dict[str, Any]:
    """Build one rule for the collection mod's ``rules`` array.

    Matches Vortex's own ``collectionModToRule`` shape so the rules array is
    indistinguishable from a native install:
    * ``type`` is ``recommends`` for optional mods, ``requires`` otherwise --
      only ``requires`` rules count toward collection completeness, so optionals
      neither block completion nor get treated as mandatory.
    * ``reference.tag`` matches the member mod's ``attributes.referenceTag`` --
      this alone is how Vortex links an installed mod to its collection slot
      (``installerChoices``/``fileList`` live at the rule top level, NOT in
      ``reference``, so they don't affect matching).
    * ``installerChoices``/``fileList``/``extra.patches``/``extra.fileOverrides``
      mirror the collection entry so update/patch behavior matches native. These
      ride inside the JSON-serialized rules array, so dotted filename keys (e.g.
      patch keys like ``foo.esp``) are preserved verbatim.
    """
    s = mod.get("source", {})
    rule: Dict[str, Any] = {
        "type": "recommends" if mod.get("optional") else "requires",
        "reference": {
            "description": mod.get("name", ""), "fileMD5": s.get("md5", ""),
            "gameId": GAME_ID, "fileSize": s.get("fileSize", 0),
            "versionMatch": f">={mod.get('version', '0')}+{s.get('updatePolicy', 'prefer')}",
            "logicalFileName": s.get("logicalFilename") or mod.get("name", ""),
            "tag": s.get("tag", ""), "md5Hint": s.get("md5", ""),
            "repo": {
                "repository": "nexus", "gameId": NEXUS_DOMAIN,
                "modId": str(s.get("modId", "")), "fileId": str(s.get("fileId", "")),
                "campaign": "collection",
            },
        },
        "extra": {
            "author": mod.get("author", ""),
            "type": (mod.get("details") or {}).get("type", ""),
            "category": (mod.get("details") or {}).get("category", ""),
            "version": str(mod.get("version", "")), "name": mod.get("name", ""),
            "phase": mod.get("phase", 0), "fileName": archive_name,
        },
    }
    # Only emit when present -- JSON.stringify drops undefined, so a native rule
    # omits these keys when the mod has no choices/hashes/patches; match that.
    if mod.get("choices"):
        rule["installerChoices"] = mod["choices"]
    if mod.get("hashes"):
        rule["fileList"] = mod["hashes"]
    if mod.get("patches"):
        rule["extra"]["patches"] = mod["patches"]
    if mod.get("fileOverrides"):
        rule["extra"]["fileOverrides"] = mod["fileOverrides"]
    if mod.get("instructions"):
        rule["extra"]["instructions"] = mod["instructions"]
    return rule


def build_collection_mod(info: Dict[str, Any], coll_folder: str, archive_id: str,
                         rules: list, revision_id: int, revision_number: int,
                         collection_id: int, slug: str, *,
                         install_completed_ms: int = 0,
                         install_time: str = "") -> Tuple[str, Dict[str, Any]]:
    """Build the ``type:collection`` mod record at a given revision.

    ``install_completed_ms`` marks the collection install as finished; without it
    Vortex can treat the collection as incomplete and re-prompt to install it.
    """
    attributes: Dict[str, Any] = {
        "source": "nexus", "downloadGame": GAME_ID, "endorsed": "Undecided",
        "name": coll_folder, "customFileName": info.get("name", coll_folder),
        "collectionId": collection_id, "collectionSlug": slug,
        "revisionId": revision_id, "revisionNumber": revision_number,
        "version": str(revision_number), "newestVersion": str(revision_number),
        "installInstructions": info.get("installInstructions", ""),
        "author": info.get("author", ""),
        "excludePluginRules": False, "recommendNewProfile": False,
    }
    if install_completed_ms:
        attributes["installCompleted"] = install_completed_ms
    if install_time:
        attributes["installTime"] = install_time
    tree = {
        "id": coll_folder, "installationPath": coll_folder, "state": "installed",
        "type": "collection", "archiveId": archive_id, "fileOverrides": [], "rules": rules,
        "attributes": attributes,
    }
    base = f"persistent{P}mods{P}{GAME_ID}{P}{coll_folder}"
    return base, _flatten(tree)


def build_collection_revision(info: Dict[str, Any], mods: list, revision_id: int,
                              revision_number: int, collection_id: int,
                              slug: str) -> Tuple[str, Dict[str, Any]]:
    """Build the cached ``persistent.collections.revisions.<id>`` manifest."""
    mod_files = [
        {"file": {"modId": m["source"]["modId"], "fileId": m["source"]["fileId"],
                  "size": m["source"].get("fileSize", 0), "name": m.get("name", "")}}
        for m in mods if (m.get("source") or {}).get("type") == "nexus"
    ]
    tree = {
        "timestamp": 0,
        "info": {
            "id": revision_id, "revisionNumber": revision_number,
            "adultContent": True, "status": "published",
            "collection": {"id": collection_id, "slug": slug},
            "modFiles": mod_files,
        },
    }
    base = f"persistent{P}collections{P}revisions{P}{revision_id}"
    return base, _flatten(tree)


def to_absolute(base: str, relative_leaves: Dict[str, Any]) -> Dict[str, str]:
    """Expand ``(base, {dotted.rel: value})`` into ``{full###key: json_value}``."""
    out: Dict[str, str] = {}
    for rel, val in relative_leaves.items():
        full = base + P + rel.replace(".", P)
        out[full] = json.dumps(val)
    return out
