"""Validate that the record builders produce schema-conformant Vortex records."""
import json

from conftest import SAMPLE_NEXUS_MOD
from utils import vortex_records as vr
from utils.vortex_schema import validate_record, P


FOLDER = "Halffaces - Giant Mortar - 2K-122495-1-1719102412"
ARCHIVE = "Halffaces - Giant Mortar - 2K-122495-1-1719102412.zip"


def test_download_record_is_schema_valid():
    base, leaves = vr.build_download(SAMPLE_NEXUS_MOD["source"],
                                     SAMPLE_NEXUS_MOD["name"], ARCHIVE, "NXDabc123def")
    assert base.startswith(f"persistent{P}downloads{P}files{P}")
    assert validate_record("download", leaves) == []
    assert leaves["state"] == "finished"
    assert leaves["modInfo.nexus.ids.modId"] == 122495
    # received == size so Vortex doesn't run a "Finalizing downloads" pass
    assert leaves["received"] == leaves["size"] == 11309496
    # urls is the real nxm link, not empty
    assert leaves["urls"] and "campaign=collection" in leaves["urls"][0]


def test_download_record_links_to_installed_mod_and_collection():
    _, leaves = vr.build_download(SAMPLE_NEXUS_MOD["source"], SAMPLE_NEXUS_MOD["name"],
                                  ARCHIVE, "NXDabc123def", folder=FOLDER, collection_id=30366)
    assert validate_record("download", leaves) == []
    # the installed-link is what flips it from "Never Installed" to recognized
    assert leaves["installed.modId"] == FOLDER
    assert leaves["installed.gameId"] == "skyrimse"
    assert leaves["modInfo.nexus.parentCollectionId"] == "30366"
    # download's referenceTag matches the mod/rule tag
    assert leaves["modInfo.referenceTag"] == "m1z8V2E5s"


def test_mod_record_is_schema_valid():
    base, leaves = vr.build_mod(SAMPLE_NEXUS_MOD["source"], SAMPLE_NEXUS_MOD,
                                FOLDER, "NXDabc123def", ARCHIVE,
                                variant="Test Collection", installed_as_dependency=True)
    assert validate_record("mod", leaves) == []
    assert leaves["state"] == "installed"
    assert leaves["installationPath"] == FOLDER
    # referenceTag must carry the collection tag so the collection can link it
    assert leaves["attributes.referenceTag"] == "m1z8V2E5s"
    # variant + installedAsDependency nest the mod under its collection
    assert leaves["attributes.variant"] == "Test Collection"
    assert leaves["attributes.installedAsDependency"] is True
    # non-numeric category label is dropped (Vortex stores a numeric id)
    assert "attributes.category" not in leaves


def test_mod_record_keeps_numeric_category():
    mod = dict(SAMPLE_NEXUS_MOD, details={"category": "62"})
    _, leaves = vr.build_mod(mod["source"], mod, FOLDER, "DL", ARCHIVE)
    assert validate_record("mod", leaves) == []
    assert leaves["attributes.category"] == 62


def test_profile_modstate_is_schema_valid():
    base, leaves = vr.build_profile_modstate("tKUx4Zd1N", FOLDER)
    assert validate_record("profile_modstate", leaves) == []
    assert leaves["enabled"] is True


def test_to_absolute_produces_json_encoded_leaf_keys():
    base, leaves = vr.build_profile_modstate("tKUx4Zd1N", FOLDER)
    absolute = vr.to_absolute(base, leaves)
    enabled_key = f"{base}{P}enabled"
    assert enabled_key in absolute
    assert json.loads(absolute[enabled_key]) is True   # values are JSON strings
    assert all(P in k for k in absolute)                 # all keys are ###-joined


def test_mod_record_links_to_its_download():
    _, leaves = vr.build_mod(SAMPLE_NEXUS_MOD["source"], SAMPLE_NEXUS_MOD,
                             FOLDER, "DL_ID_42", ARCHIVE)
    assert leaves["archiveId"] == "DL_ID_42"


def test_normalize_version_coerces_to_semver():
    # zero-padded segment (the bikini-armor 1.01 case) -> 1.1.0
    assert vr.normalize_version("1.01") == "1.1.0"
    # leading v stripped, missing segments padded
    assert vr.normalize_version("v2") == "2.0.0"
    assert vr.normalize_version("1.0") == "1.0.0"
    assert vr.normalize_version("3.4.5") == "3.4.5"
    # empty / None -> default
    assert vr.normalize_version("", default="0.0.0") == "0.0.0"
    assert vr.normalize_version(None) == ""
    # no digits at all -> returned trimmed, unmangled
    assert vr.normalize_version("  unknown ") == "unknown"


def test_record_and_rule_versions_agree_for_a_versioned_mod():
    mod = dict(SAMPLE_NEXUS_MOD, version="1.01")
    _, leaves = vr.build_mod(mod["source"], mod, FOLDER, "DL", ARCHIVE)
    rule = vr.build_collection_rule(mod, ARCHIVE)
    # both sides normalize identically -> no record/rule disagreement
    assert leaves["attributes.version"] == "1.1.0"
    assert ">=1.1.0+" in rule["reference"]["versionMatch"]
