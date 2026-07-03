import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from utils import install_verify as iv
from utils import local_state as ls


def _mk_plugin(path, masters=()):
    """Minimal TES4 plugin with optional MAST master subrecords."""
    body = b""
    for m in masters:
        name = m.encode("cp1252") + b"\x00"
        body += b"MAST" + struct.pack("<H", len(name)) + name
        body += b"DATA" + struct.pack("<H", 8) + b"\x00" * 8
    header = (b"TES4" + struct.pack("<I", len(body)) + struct.pack("<I", 0)
              + b"\x00" * 12)
    with open(path, "wb") as f:
        f.write(header + body)


def _ledger(tmp_path):
    return ls.LocalState(str(tmp_path / "ledger.db"))


def _add(st, *, folder, dl_id, mod_id, file_id, size=100, enabled=True,
         file_count=1, choices=None, local_path="x.7z"):
    st.upsert_download(dl_id, local_path, mod_id, file_id, "", size, 1, "", None)
    st.upsert_mod(folder, dl_id, enabled=enabled, file_count=file_count,
                  installer_choices=choices)


def test_staging_buckets_orphan_manual_and_missing(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    coll = {"mods": [
        {"name": "Required A", "source": {"modId": 11, "fileId": 1, "fileSize": 100}},
        {"name": "Optional B", "optional": True, "source": {"modId": 22, "fileId": 2, "fileSize": 100}},
    ]}
    with _ledger(tmp_path) as st:
        # Required A installed with content
        (staging / "ReqA").mkdir()
        (staging / "ReqA" / "f.txt").write_text("hi")
        _add(st, folder="ReqA", dl_id="d1", mod_id=11, file_id=1, file_count=1)
        # A manual mod (no identity) present in ledger + on disk
        (staging / "ManualMod").mkdir()
        (staging / "ManualMod" / "g.txt").write_text("hi")
        st.upsert_download("d9", str(staging / "m.7z"), None, None, "", 1, 1, "", None)
        st.upsert_mod("ManualMod", "d9", file_count=1)
        # A true orphan: folder on disk, no ledger row
        (staging / "OrphanFolder").mkdir()
        (staging / "OrphanFolder" / "h.txt").write_text("hi")
        st.flush()

        findings = iv.verify_staging(coll, str(staging), st)

    folders = {f.folder: f for f in findings}
    # Optional B was never installed -> INFO, not error
    assert any(f.severity == iv.INFO and "optional" in f.detail.lower()
               and f.data.get("modId") == 22 for f in findings)
    # Manual mod -> INFO manual, not orphan
    assert folders["ManualMod"].severity == iv.INFO
    assert "manual" in folders["ManualMod"].detail.lower()
    # True orphan -> WARN
    assert folders["OrphanFolder"].severity == iv.WARN
    assert "orphan" in folders["OrphanFolder"].detail.lower()
    # Required A is fine -> no finding for it
    assert "ReqA" not in folders


def test_empty_required_folder_is_error(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    coll = {"mods": [{"name": "Req", "source": {"modId": 11, "fileId": 1, "fileSize": 100}}]}
    with _ledger(tmp_path) as st:
        (staging / "ReqEmpty").mkdir()  # empty folder == failed install
        _add(st, folder="ReqEmpty", dl_id="d1", mod_id=11, file_id=1, file_count=5)
        st.flush()
        findings = iv.verify_staging(coll, str(staging), st)
    err = [f for f in findings if f.severity == iv.ERROR]
    assert err and "empty" in err[0].detail.lower()


def test_archive_size_mismatch_is_error(tmp_path):
    dl = tmp_path / "dl"
    dl.mkdir()
    archive = dl / "mod-11-1.7z"
    archive.write_bytes(b"x" * 90)  # 90 bytes, collection expects 100
    coll = {"mods": [{"name": "Req", "source": {"modId": 11, "fileId": 1, "fileSize": 100}}]}
    with _ledger(tmp_path) as st:
        _add(st, folder="ReqA", dl_id="d1", mod_id=11, file_id=1,
             local_path=str(archive))
        st.flush()
        findings = iv.verify_archives(coll, st)
    assert any(f.severity == iv.ERROR and "size" in f.detail.lower() for f in findings)


def test_stray_plugin_with_phantom_master_is_flagged(tmp_path):
    """The WACCF case: an enabled mod folder has a plugin the collection never
    declared, and it depends on a master that's neither vanilla nor declared."""
    staging = tmp_path / "staging"
    (staging / "BadFomodMod").mkdir(parents=True)
    _mk_plugin(staging / "BadFomodMod" / "UnintendedPatch.esp",
               masters=["Skyrim.esm", "WACCF_BashedPatchLvlListFix.esp"])
    coll = {
        "mods": [],
        # collection declares only its real plugins; the stray patch is NOT here
        "plugins": [{"name": "SomeIntended.esp", "enabled": True}],
    }
    with _ledger(tmp_path) as st:
        st.upsert_mod("BadFomodMod", None, enabled=True, file_count=1)
        st.flush()
        findings = iv.verify_plugins(coll, str(staging), st)
    hit = [f for f in findings if f.data.get("plugin") == "UnintendedPatch.esp"]
    assert hit, "stray plugin not detected"
    f = hit[0]
    assert f.severity == iv.WARN
    # Skyrim.esm is vanilla and must NOT be a phantom; WACCF must be flagged
    assert "WACCF_BashedPatchLvlListFix.esp" in f.data["phantom_masters"]
    assert "Skyrim.esm" not in f.data["phantom_masters"]


def test_cheap_content_flags_recorded_choice_drift(tmp_path):
    fomod = {"type": "fomod", "options": [
        {"name": "Main", "groups": [
            {"name": "Body", "choices": [{"name": "CBBE", "idx": 0}]}]}]}
    other = {"type": "fomod", "options": [
        {"name": "Main", "groups": [
            {"name": "Body", "choices": [{"name": "UNP", "idx": 1}]}]}]}
    coll = {"mods": [{"name": "BodyMod", "choices": fomod,
                     "source": {"modId": 11, "fileId": 1, "fileSize": 100}}]}
    with _ledger(tmp_path) as st:
        _add(st, folder="BodyMod", dl_id="d1", mod_id=11, file_id=1, choices=other)
        st.flush()
        findings = iv.verify_content_cheap(coll, st)
    assert any(f.severity == iv.WARN and "differ" in f.detail.lower() for f in findings)
