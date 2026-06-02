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


def test_mod_record_is_schema_valid():
    base, leaves = vr.build_mod(SAMPLE_NEXUS_MOD["source"], SAMPLE_NEXUS_MOD,
                                FOLDER, "NXDabc123def", ARCHIVE)
    assert validate_record("mod", leaves) == []
    assert leaves["state"] == "installed"
    assert leaves["installationPath"] == FOLDER
    # referenceTag must carry the collection tag so the collection can link it
    assert leaves["attributes.referenceTag"] == "m1z8V2E5s"


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
