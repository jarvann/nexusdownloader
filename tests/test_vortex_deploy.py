"""Tests for the deployment writer (conflict resolution, manifest, hard-link, DB mark)."""
import json

import pytest

from utils import vortex_deploy as vd


# --- pure conflict resolution --------------------------------------------
def test_resolve_last_wins_case_insensitive():
    staged = [
        ("ModA", "meshes/x.nif", 100),
        ("ModB", "Meshes/X.nif", 200),   # same path (diff case) -> overrides ModA
        ("ModB", "textures/y.dds", 300),
    ]
    entries, links = vd.resolve_deployment(staged)
    by_path = {e["relPath"].lower(): e for e in entries}
    assert by_path["meshes\\x.nif"]["source"] == "ModB"   # higher priority won
    assert by_path["meshes\\x.nif"]["time"] == 200
    assert len(entries) == 2 and len(links) == 2
    # paths are normalised to backslashes and entries are sorted
    assert all("\\" in e["relPath"] or "/" not in e["relPath"] for e in entries)
    assert [e["relPath"] for e in entries] == sorted(e["relPath"] for e in entries)


def test_build_manifest_shape_matches_vortex():
    entries = [{"relPath": "a\\b.nif", "source": "ModA", "target": "", "time": 1}]
    m = vd.build_manifest("INST-1", "skyrimse", "L:\\stage", "L:\\game\\Data",
                          entries, deployment_time_ms=999)
    assert m == {
        "instance": "INST-1", "version": 1, "deploymentMethod": "hardlink_activator",
        "gameId": "skyrimse", "deploymentTime": 999,
        "stagingPath": "L:\\stage", "targetPath": "L:\\game\\Data", "files": entries,
    }


# --- instance id discovery ------------------------------------------------
def test_read_instance_id_prefers_existing_manifest(tmp_path):
    target = tmp_path / "Data"
    target.mkdir()
    (target / vd.MANIFEST_NAME).write_text(json.dumps({"instance": "FROM-MANIFEST"}))
    staging = tmp_path / "stage"
    staging.mkdir()
    (staging / vd.STAGING_MARKER).write_text(json.dumps({"instance": "FROM-MARKER"}))
    assert vd.read_instance_id(str(staging), str(target)) == "FROM-MANIFEST"


def test_read_instance_id_falls_back_to_marker(tmp_path):
    target = tmp_path / "Data"
    target.mkdir()
    staging = tmp_path / "stage"
    staging.mkdir()
    (staging / vd.STAGING_MARKER).write_text(json.dumps({"instance": "FROM-MARKER"}))
    assert vd.read_instance_id(str(staging), str(target)) == "FROM-MARKER"


def test_read_instance_id_none_when_absent(tmp_path):
    assert vd.read_instance_id(str(tmp_path), str(tmp_path)) is None


# --- full deploy (hard-link + manifest) -----------------------------------
def test_deploy_links_files_and_writes_manifest(tmp_path):
    staging = tmp_path / "stage"
    target = tmp_path / "Data"
    target.mkdir()
    # two mods, ModB overrides ModA on the shared meshes/x.nif
    (staging / "ModA" / "meshes").mkdir(parents=True)
    (staging / "ModA" / "meshes" / "x.nif").write_text("A")
    (staging / "ModB" / "meshes").mkdir(parents=True)
    (staging / "ModB" / "meshes" / "x.nif").write_text("B")
    (staging / "ModB" / "textures").mkdir(parents=True)
    (staging / "ModB" / "textures" / "y.dds").write_text("Y")

    res = vd.deploy(str(staging), str(target), ["ModA", "ModB"], "skyrimse",
                    instance_id="INST-9", deployment_time_ms=123)

    # winner content present in the game folder
    assert (target / "meshes" / "x.nif").read_text() == "B"
    assert (target / "textures" / "y.dds").read_text() == "Y"
    assert res.files == 2 and res.linked == 2 and res.instance_id == "INST-9"

    manifest = json.loads((target / vd.MANIFEST_NAME).read_text())
    assert manifest["instance"] == "INST-9"
    assert manifest["deploymentMethod"] == "hardlink_activator"
    sources = {e["relPath"]: e["source"] for e in manifest["files"]}
    assert sources["meshes\\x.nif"] == "ModB"


def test_deploy_raises_without_instance(tmp_path):
    staging = tmp_path / "stage"
    staging.mkdir()
    target = tmp_path / "Data"
    target.mkdir()
    with pytest.raises(RuntimeError, match="instance id"):
        vd.deploy(str(staging), str(target), [], "skyrimse")


def test_mark_deployed_increments_counter(monkeypatch):
    from utils import vortex_db
    captured = {}

    def fake_read(db, key, node="node"):
        return {key: 7}   # existing counter

    def fake_write(db, records, backup=True, node="node"):
        captured["records"] = records
        return "ok"

    monkeypatch.setattr(vortex_db, "read_prefix", fake_read)
    monkeypatch.setattr(vortex_db, "write_records", fake_write)
    vd.mark_deployed_in_db("/db", "skyrimse")
    assert captured["records"]["persistent###deployment###deploymentCounter###skyrimse"] == "8"
    assert captured["records"]["persistent###deployment###needToDeploy###skyrimse"] == "false"
