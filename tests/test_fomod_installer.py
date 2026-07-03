"""Regression tests for fomod_installer logic added this cycle (manual-mod handling)."""
import logging
from pathlib import Path

from utils.fomod_installer import FomodInstaller, InstallResult


def _bare_installer():
    """A FomodInstaller without the heavy __init__ (temp dirs / archive handler)."""
    inst = FomodInstaller.__new__(FomodInstaller)
    inst.logger = logging.getLogger("test")
    return inst


def test_is_manual_mod_classification():
    inst = _bare_installer()
    nexus = {"source": {"type": "nexus", "modId": 1, "fileId": 2}}
    browse = {"source": {"type": "browse", "url": "http://x"}}
    bundle = {"source": {"type": "bundle"}}
    direct = {"source": {"type": "direct"}}
    nexus_no_file = {"source": {"type": "nexus", "modId": 1}}
    assert inst._is_manual_mod(nexus) is False
    assert inst._is_manual_mod(browse) is True
    assert inst._is_manual_mod(bundle) is True
    assert inst._is_manual_mod(direct) is True
    assert inst._is_manual_mod(nexus_no_file) is True


def test_partition_manual_mods_splits_and_annotates():
    inst = _bare_installer()
    collection = {
        "mods": [
            {"name": "Keep", "source": {"type": "nexus", "modId": 1, "fileId": 2}},
            {"name": "Manual1", "source": {"type": "browse", "url": "http://drive/x",
                                            "instructions": "install by hand"}},
            {"name": "Manual2", "source": {"type": "bundle"}},
        ]
    }
    filtered, manual_results = inst._partition_manual_mods(collection)
    # auto-installable mods remain
    assert [m["name"] for m in filtered["mods"]] == ["Keep"]
    # manual mods become SKIPPED results carrying their instructions/url
    assert len(manual_results) == 2
    assert all(r.status == InstallResult.SKIPPED for r in manual_results)
    note = manual_results[0].warnings[0]
    assert "Manual install required" in note
    assert "http://drive/x" in note and "install by hand" in note


def test_partition_no_manual_mods_is_noop():
    inst = _bare_installer()
    collection = {"mods": [{"name": "A", "source": {"type": "nexus", "modId": 1, "fileId": 2}}]}
    filtered, manual_results = inst._partition_manual_mods(collection)
    assert manual_results == []
    assert filtered is collection  # unchanged object when nothing to split


def test_vortex_folder_name_uses_sanitized_archive_stem():
    inst = _bare_installer()
    name = inst._get_vortex_folder_name("Mod", Path("/dl/Cool Mod-123-1-0-99999.7z"))
    assert name == "Cool Mod-123-1-0-99999"
    # invalid path chars get replaced
    bad = inst._get_vortex_folder_name("Mod", Path("/dl/a:b*c?-1.zip"))
    assert ":" not in bad and "*" not in bad and "?" not in bad


def _nemesis_tree(base: Path, wrapper=None):
    """Create a Nemesis behaviour-patch layout (payload is ALL .txt), optionally
    inside a single wrapper folder + meta.ini (the TK Dodge shape)."""
    root = base / wrapper if wrapper else base
    beh = root / "Nemesis_Engine" / "mod" / "tdmh" / "horsebehavior"
    beh.mkdir(parents=True)
    (beh / "#0170.txt").write_text("x")
    (beh / "#0222.txt").write_text("x")
    if wrapper:
        (root / "meta.ini").write_text("[General]\n")
    return base


def test_find_mod_root_recognizes_nemesis_at_root(tmp_path):
    # Nemesis_Engine directly at the extraction root -> that IS the root.
    _nemesis_tree(tmp_path)
    inst = _bare_installer()
    assert inst._find_mod_root(tmp_path) == tmp_path


def test_find_mod_root_strips_wrapper_around_nemesis(tmp_path):
    # Nemesis_Engine inside a single wrapper folder (+meta.ini) -> strip to wrapper
    # so it stages as Nemesis_Engine/... (deploys to Data/Nemesis_Engine/...).
    _nemesis_tree(tmp_path, wrapper="TKDodgeFartherSteps")
    inst = _bare_installer()
    root = inst._find_mod_root(tmp_path)
    assert root == tmp_path / "TKDodgeFartherSteps"
    assert (root / "Nemesis_Engine").is_dir()


def test_validate_accepts_txt_only_nemesis_mod(tmp_path):
    # Regression: a mod whose entire payload is .txt (Nemesis) must be VALID.
    _nemesis_tree(tmp_path)
    inst = _bare_installer()
    assert inst._validate_mod_installation(tmp_path) is True


def test_validate_rejects_docs_only_and_empty(tmp_path):
    inst = _bare_installer()
    # truly empty
    assert inst._validate_mod_installation(tmp_path) is False
    # only a readme + meta.ini -> still invalid
    (tmp_path / "README.txt").write_text("hi")
    (tmp_path / "meta.ini").write_text("[General]\n")
    assert inst._validate_mod_installation(tmp_path) is False
