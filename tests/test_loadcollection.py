"""Regression tests for loadcollection.load_mods_from_json (the import-fix module)."""
import json

import loadcollection


def _write(tmp_path, data):
    p = tmp_path / "collection.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def test_extracts_nexus_modid_fileid(tmp_path):
    data = {
        "info": {"domainName": "skyrimspecialedition"},
        "mods": [
            {"name": "A", "source": {"type": "nexus", "modId": 1, "fileId": 11}},
            {"name": "B", "source": {"type": "nexus", "modId": 2, "fileId": 22}},
        ],
    }
    mods = loadcollection.load_mods_from_json(_write(tmp_path, data))
    assert mods == [(1, 11), (2, 22)]
    assert loadcollection.GAME_DOMAIN == "skyrimspecialedition"


def test_skips_non_nexus_and_missing_source_keys(tmp_path):
    data = {
        "info": {"domainName": "skyrimspecialedition"},
        "mods": [
            {"name": "nexus", "source": {"type": "nexus", "modId": 5, "fileId": 55}},
            {"name": "browse", "source": {"type": "browse", "url": "http://x"}},
            {"name": "bundle", "source": {"type": "bundle"}},
            {"name": "broken", "source": {"type": "nexus", "modId": 9}},  # no fileId
        ],
    }
    mods = loadcollection.load_mods_from_json(_write(tmp_path, data))
    # only the well-formed nexus mod survives
    assert mods == [(5, 55)]


def test_returns_empty_on_bad_file(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ not json", encoding="utf-8")
    assert loadcollection.load_mods_from_json(str(p)) == []
