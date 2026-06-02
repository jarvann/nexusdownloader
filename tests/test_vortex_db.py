"""Tests for the Vortex DB guard layer (lock/concurrency checks, guarded writes)."""
import subprocess

import pytest

from utils import vortex_db as vdb


def _completed(rc, out="", err=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)


def test_probe_available(monkeypatch):
    monkeypatch.setattr(vdb, "_run_bridge", lambda *a, **k: _completed(0, "AVAILABLE\n"))
    assert vdb.probe("/db") is True


def test_probe_locked(monkeypatch):
    monkeypatch.setattr(vdb, "_run_bridge", lambda *a, **k: _completed(3, "LOCKED\n"))
    assert vdb.probe("/db") is False


def test_probe_handles_missing_node(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("node not found")
    monkeypatch.setattr(vdb, "_run_bridge", boom)
    assert vdb.probe("/db") is False


def test_ensure_available_blocks_when_vortex_running(monkeypatch):
    monkeypatch.setattr(vdb, "is_vortex_running", lambda: True)
    with pytest.raises(vdb.VortexBusyError, match="running"):
        vdb.ensure_available("/db")


def test_ensure_available_blocks_when_locked(monkeypatch):
    monkeypatch.setattr(vdb, "is_vortex_running", lambda: False)
    monkeypatch.setattr(vdb, "probe", lambda *a, **k: False)
    with pytest.raises(vdb.VortexBusyError, match="locked"):
        vdb.ensure_available("/db")


def test_ensure_available_passes_when_free(monkeypatch):
    monkeypatch.setattr(vdb, "is_vortex_running", lambda: False)
    monkeypatch.setattr(vdb, "probe", lambda *a, **k: True)
    vdb.ensure_available("/db")  # should not raise


def test_write_records_guards_and_writes(monkeypatch, tmp_path):
    monkeypatch.setattr(vdb, "ensure_available", lambda *a, **k: None)
    monkeypatch.setattr(vdb, "backup_db", lambda db: str(tmp_path / "bak"))
    monkeypatch.setattr(vdb, "_run_bridge", lambda *a, **k: _completed(0, "2\n"))
    res = vdb.write_records("/db", {"a": "1", "b": "2"})
    assert res.keys_written == 2
    assert res.backup_path.endswith("bak")


def test_write_records_aborts_if_locked_midwrite(monkeypatch, tmp_path):
    monkeypatch.setattr(vdb, "ensure_available", lambda *a, **k: None)
    monkeypatch.setattr(vdb, "backup_db", lambda db: str(tmp_path / "bak"))
    monkeypatch.setattr(vdb, "_run_bridge", lambda *a, **k: _completed(3, "", "LOCKED"))
    with pytest.raises(vdb.VortexBusyError):
        vdb.write_records("/db", {"a": "1"})


def test_read_prefix_decodes_json(monkeypatch):
    payload = '{"persistent###x": "123", "persistent###y": "\\"hi\\""}'
    monkeypatch.setattr(vdb, "_run_bridge", lambda *a, **k: _completed(0, payload))
    out = vdb.read_prefix("/db", "persistent###")
    assert out == {"persistent###x": 123, "persistent###y": "hi"}


def test_is_vortex_running_without_psutil(monkeypatch):
    monkeypatch.setattr(vdb, "psutil", None)
    assert vdb.is_vortex_running() is False


def test_read_active_profile(monkeypatch):
    monkeypatch.setattr(vdb, "read_prefix",
                        lambda *a, **k: {"settings###profiles###activeProfileId": "PROF1"})
    assert vdb.read_active_profile("/db") == "PROF1"


def test_read_collection_identity(monkeypatch):
    monkeypatch.setattr(vdb, "read_prefix", lambda *a, **k: {
        "persistent###mods###skyrimse###Coll###attributes###collectionId": 26945,
        "persistent###mods###skyrimse###Coll###attributes###collectionSlug": "gnfjwh",
    })
    assert vdb.read_collection_identity("/db") == (26945, "gnfjwh")


def test_read_collection_identity_none_when_absent(monkeypatch):
    monkeypatch.setattr(vdb, "read_prefix", lambda *a, **k: {})
    assert vdb.read_collection_identity("/db") is None
