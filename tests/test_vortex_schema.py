"""Tests for schema validation and version/schema drift assessment."""
from utils.vortex_schema import (
    validate_record, assess_risk, VALIDATED_VORTEX_VERSION,
)


def _valid_mod_leaves():
    return {
        "id": "X", "installationPath": "X", "state": "installed", "type": "",
        "attributes.source": "nexus", "attributes.modId": 1, "attributes.fileId": 2,
        "attributes.name": "X",
    }


def test_valid_record_has_no_violations():
    assert validate_record("mod", _valid_mod_leaves()) == []


def test_missing_required_is_flagged():
    leaves = _valid_mod_leaves()
    del leaves["attributes.modId"]
    v = validate_record("mod", leaves)
    assert any("attributes.modId" in x for x in v)


def test_unknown_key_is_flagged():
    leaves = _valid_mod_leaves()
    leaves["attributes.bogusField"] = "x"
    assert any("unknown key" in x for x in validate_record("mod", leaves))


def test_bad_enum_is_flagged():
    leaves = _valid_mod_leaves()
    leaves["state"] = "not-a-real-state"
    assert any("not in allowed" in x for x in validate_record("mod", leaves))


def test_bool_where_int_expected_is_flagged():
    leaves = _valid_mod_leaves()
    leaves["attributes.modId"] = True  # bool sneaking in as int
    assert any("expected int, got bool" in x for x in validate_record("mod", leaves))


def test_unknown_record_type():
    assert validate_record("nope", {}) == ["unknown record type 'nope'"]


# --- drift assessment -----------------------------------------------------
def test_matching_version_is_safe():
    r = assess_risk(VALIDATED_VORTEX_VERSION)
    assert r.safe and r.level == "ok"


def test_different_version_no_samples_is_risky():
    r = assess_risk("99.0.0", live_samples=None)
    assert not r.safe and r.level == "schema-drift"


def test_different_version_but_schema_intact_is_version_drift():
    samples = {
        "mod": _valid_mod_leaves(),
        "download": {
            "state": "finished", "fileMD5": "x", "game": ["skyrimse"],
            "localPath": "a.zip", "size": 1,
            "modInfo.nexus.ids.modId": 1, "modInfo.nexus.ids.fileId": 2,
            "modInfo.nexus.ids.gameId": "skyrimspecialedition",
        },
        "profile_modstate": {"enabled": True},
    }
    r = assess_risk("99.0.0", live_samples=samples)
    assert r.safe and r.level == "version-drift"


def test_different_version_with_missing_keys_is_schema_drift():
    # live mod record is missing a key we rely on -> structural drift -> risky
    samples = {"mod": {"id": "X", "installationPath": "X", "state": "installed"}}
    r = assess_risk("99.0.0", live_samples=samples)
    assert not r.safe and r.level == "schema-drift"
    assert any("missing expected keys" in d for d in r.details)
