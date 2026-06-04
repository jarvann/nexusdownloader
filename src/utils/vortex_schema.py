"""
Vortex state.v2 record schemas + version-drift detection.

We write records into Vortex's LevelDB by mirroring the exact shape Vortex itself
produces. That shape is reverse-engineered and **version specific**, so this
module pins the schemas to the Vortex version we validated against and provides:

* :data:`RECORD_SCHEMAS` — required/optional leaf keys (relative to a record's
  base key) and their value types, per record type.
* :func:`validate_record` — assert a record we're about to write conforms.
* :func:`assess_risk` — compare the *target* Vortex install (its version + a live
  sample of each record type) against the validated baseline, and report whether
  writing is safe or "risky" (drift detected) so the GUI can warn the user.

Vortex stores its Redux state flattened into LevelDB keys joined by ``###``, with
leaf values JSON-encoded. Objects are flattened into more keys; arrays/scalars
are stored as a single JSON-encoded leaf. The schemas below describe the *leaf*
keys relative to each record's base (e.g. for a mod, ``attributes.modId``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


# The exact Vortex version whose schema these definitions were captured and
# validated against. Bump this (and re-capture goldens) when we test a new one.
VALIDATED_VORTEX_VERSION = "2.0.2"

GAME_ID = "skyrimse"           # Vortex internal game id for Skyrim SE
NEXUS_DOMAIN = "skyrimspecialedition"


@dataclass(frozen=True)
class RecordSchema:
    """Schema for one Vortex record type, as relative leaf keys."""
    name: str
    base_prefix: str                      # e.g. "persistent###mods###skyrimse"
    required: Dict[str, str]              # leaf path -> type ("str","int","bool","array","enum")
    optional: Dict[str, str] = field(default_factory=dict)
    enums: Dict[str, Set[str]] = field(default_factory=dict)  # leaf path -> allowed values

    def known_keys(self) -> Set[str]:
        return set(self.required) | set(self.optional)


P = "###"

RECORD_SCHEMAS: Dict[str, RecordSchema] = {
    "download": RecordSchema(
        name="download",
        base_prefix=f"persistent{P}downloads{P}files",
        required={
            "state": "enum",
            "fileMD5": "str",
            "game": "array",
            "localPath": "str",
            "size": "int",
            "received": "int",   # 2.0.x: bytes received; == size when finalized
            "modInfo.nexus.ids.modId": "int",
            "modInfo.nexus.ids.fileId": "int",
            "modInfo.nexus.ids.gameId": "str",
        },
        optional={
            "chunks": "array", "urls": "array", "source": "str", "fileTime": "int",
            "modInfo.source": "str",
            "modInfo.meta.fileName": "str", "modInfo.meta.fileMD5": "str",
            "modInfo.meta.gameId": "str", "modInfo.meta.domainName": "str",
            "modInfo.meta.source": "str", "modInfo.meta.logicalFileName": "str",
            "modInfo.meta.fileSizeBytes": "int", "modInfo.meta.status": "str",
            "modInfo.meta.sourceURI": "str",
            "modInfo.meta.details.modId": "str", "modInfo.meta.details.fileId": "str",
        },
        enums={"state": {"finished", "init", "started", "paused", "failed"}},
    ),
    "mod": RecordSchema(
        name="mod",
        base_prefix=f"persistent{P}mods{P}{GAME_ID}",
        required={
            "id": "str",
            "installationPath": "str",
            "state": "enum",
            "type": "str",
            "attributes.source": "str",
            "attributes.modId": "int",
            "attributes.fileId": "int",
            "attributes.name": "str",
        },
        optional={
            "archiveId": "str", "fileOverrides": "array",
            "attributes.fileMD5": "str", "attributes.fileName": "str",
            "attributes.fileSize": "int", "attributes.logicalFileName": "str",
            "attributes.version": "str", "attributes.modVersion": "str",
            "attributes.referenceTag": "str", "attributes.customFileName": "str",
            "attributes.author": "str", "attributes.category": "str",
            "attributes.downloadGame": "str", "attributes.endorsed": "str",
            # 2.0.x additions
            "attributes.installedAsDependency": "bool", "attributes.variant": "str",
            # collection-only attributes (present when type == "collection")
            "attributes.collectionId": "int", "attributes.collectionSlug": "str",
            "attributes.revisionId": "int", "attributes.revisionNumber": "int",
            "attributes.newestVersion": "str", "rules": "array",
        },
        enums={
            "state": {"installed", "installing", "downloaded", "uninstalled"},
            "type": {"", "collection"},
        },
    ),
    "collection": RecordSchema(
        name="collection",
        base_prefix=f"persistent{P}mods{P}{GAME_ID}",
        required={
            "id": "str", "installationPath": "str", "state": "enum", "type": "enum",
            "attributes.source": "str", "attributes.name": "str",
            "attributes.collectionId": "int", "attributes.collectionSlug": "str",
            "attributes.revisionId": "int", "attributes.revisionNumber": "int",
        },
        optional={
            "archiveId": "str", "fileOverrides": "array", "rules": "array",
            "attributes.version": "str", "attributes.newestVersion": "str",
            "attributes.customFileName": "str", "attributes.installInstructions": "str",
            "attributes.author": "str", "attributes.downloadGame": "str",
            "attributes.endorsed": "str",
        },
        enums={"state": {"installed", "installing"}, "type": {"collection"}},
    ),
    "profile_modstate": RecordSchema(
        name="profile_modstate",
        base_prefix=f"persistent{P}profiles",  # full base is profiles###<id>###modState###<mod>
        required={"enabled": "bool"},
        optional={"enabledTime": "int"},
    ),
}


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
_PY_TYPES = {
    "str": str, "int": int, "bool": bool, "array": list,
}


def validate_record(record_type: str, leaves: Dict[str, object]) -> List[str]:
    """Validate a record's *relative* leaf dict against its schema.

    Returns a list of human-readable violations (empty == valid).
    """
    schema = RECORD_SCHEMAS.get(record_type)
    if schema is None:
        return [f"unknown record type {record_type!r}"]

    violations: List[str] = []
    for key, typ in schema.required.items():
        if key not in leaves:
            violations.append(f"missing required '{key}'")
            continue
        violations += _check_type(key, leaves[key], typ, schema)

    known = schema.known_keys()
    for key, val in leaves.items():
        if key in schema.required:
            continue
        if key not in known:
            violations.append(f"unknown key '{key}' (not in {schema.name} schema)")
        else:
            violations += _check_type(key, val, schema.optional.get(key, "str"), schema)
    return violations


def _check_type(key: str, val: object, typ: str, schema: RecordSchema) -> List[str]:
    if typ == "enum":
        allowed = schema.enums.get(key, set())
        if allowed and val not in allowed:
            return [f"'{key}'={val!r} not in allowed {sorted(allowed)}"]
        return []
    py = _PY_TYPES.get(typ)
    if py is None:
        return []
    # bool is a subclass of int in Python; guard against the overlap
    if typ == "int" and isinstance(val, bool):
        return [f"'{key}' expected int, got bool"]
    if not isinstance(val, py):
        return [f"'{key}' expected {typ}, got {type(val).__name__}"]
    return []


# --------------------------------------------------------------------------- #
# Version / schema drift assessment
# --------------------------------------------------------------------------- #
@dataclass
class RiskAssessment:
    safe: bool
    level: str            # "ok" | "version-drift" | "schema-drift"
    message: str
    details: List[str] = field(default_factory=list)


def assess_risk(target_version: Optional[str],
                live_samples: Optional[Dict[str, Dict[str, object]]] = None) -> RiskAssessment:
    """Decide whether writing to a target Vortex install is safe.

    Args:
        target_version: the target install's ``app.appVersion`` (or None).
        live_samples: optional ``{record_type: {relative_leaf: value}}`` sampled
            from the *live* DB, used to detect structural drift even when the
            version differs.

    Logic:
        * version matches validated -> ok.
        * version differs but live samples still contain all our required keys ->
          version-drift (probably fine, warn).
        * required keys missing from the live sample, or we can't tell -> schema
          drift, treat as risky.
    """
    if target_version == VALIDATED_VORTEX_VERSION:
        return RiskAssessment(True, "ok",
                              f"Vortex {target_version} matches the validated schema.")

    details: List[str] = []
    if live_samples:
        for rtype, schema in RECORD_SCHEMAS.items():
            sample = live_samples.get(rtype)
            if not sample:
                continue
            missing = [k for k in schema.required if k not in sample]
            if missing:
                details.append(f"{rtype}: live record missing expected keys {missing}")
        if details:
            return RiskAssessment(
                False, "schema-drift",
                f"Vortex {target_version} differs from the validated "
                f"{VALIDATED_VORTEX_VERSION} and its DB schema has changed. "
                f"Writing is RISKY.",
                details)
        return RiskAssessment(
            True, "version-drift",
            f"Vortex {target_version} differs from the validated "
            f"{VALIDATED_VORTEX_VERSION}, but the sampled DB records still match "
            f"the known schema. Proceed with caution.")

    # No samples to compare and version differs -> can't confirm safety.
    return RiskAssessment(
        False, "schema-drift",
        f"Vortex {target_version!r} is not the validated version "
        f"{VALIDATED_VORTEX_VERSION} and no live records were sampled to verify "
        f"the schema. Writing is RISKY until re-validated.")
