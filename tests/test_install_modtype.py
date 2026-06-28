import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from pathlib import Path
from utils.fomod_installer import FomodInstaller


def _inst(tmp_path):
    staging = tmp_path / "staging"; staging.mkdir()
    return FomodInstaller(str(staging)), staging


def test_default_mod_strips_to_data(tmp_path):
    inst, _ = _inst(tmp_path)
    ex = tmp_path / "ex"; (ex / "Data" / "meshes").mkdir(parents=True)
    (ex / "Data" / "MyMod.esp").write_text("e")
    (ex / "Data" / "meshes" / "m.nif").write_text("m")
    root = inst._stage_root_for(ex)
    # staged from the Data root -> staging holds MyMod.esp at top (deploys to Data)
    assert (root / "MyMod.esp").exists()
    assert root.name == "Data"


def test_skse_mod_stages_whole(tmp_path):
    inst, _ = _inst(tmp_path)
    ex = tmp_path / "ex"; (ex / "Data" / "SKSE" / "Plugins").mkdir(parents=True)
    (ex / "skse64_loader.exe").write_text("L")
    (ex / "skse64_1_6_640.dll").write_text("D")
    (ex / "Data" / "SKSE" / "Plugins" / "p.dll").write_text("p")
    root = inst._stage_root_for(ex)
    # whole tree kept: loader at top AND the Data/ subfolder (deploys to game root)
    assert (root / "skse64_loader.exe").exists()
    assert (root / "Data" / "SKSE" / "Plugins" / "p.dll").exists()


def test_engine_fixes_part2_stages_whole(tmp_path):
    inst, _ = _inst(tmp_path)
    ex = tmp_path / "ex"; ex.mkdir()
    (ex / "d3dx9_42.dll").write_text("x"); (ex / "tbbmalloc.dll").write_text("t")
    root = inst._stage_root_for(ex)
    assert (root / "d3dx9_42.dll").exists()   # kept loose, deploys to game root


def test_wrapped_skse_peeled_then_whole(tmp_path):
    inst, _ = _inst(tmp_path)
    ex = tmp_path / "ex"; inner = ex / "SKSE_2_0_20"
    (inner / "Data" / "SKSE").mkdir(parents=True)
    (inner / "skse64_loader.exe").write_text("L")
    (inner / "Data" / "SKSE" / "x.dll").write_text("p")
    root = inst._stage_root_for(ex)
    assert (root / "skse64_loader.exe").exists() and (root / "Data" / "SKSE" / "x.dll").exists()
