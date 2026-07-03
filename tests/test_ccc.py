import os, sys, struct
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from pathlib import Path
from utils import vortex_loadorder as lo


def _mk(p, master=False):
    with open(p, "wb") as f:
        f.write(b"TES4"); f.write(b"\x00"*4)
        f.write(struct.pack("<I", 0x1 if master else 0)); f.write(b"\x00"*20)


def test_ccc_plugin_excluded_from_plugins_txt(tmp_path):
    game = tmp_path / "Skyrim Special Edition"; data = game / "Data"; lad = tmp_path / "lad"
    data.mkdir(parents=True); lad.mkdir()
    # A CC plugin whose name does NOT match the cc???sse### regex -> only Skyrim.ccc knows it
    _mk(data / "ccCustomContent.esl", master=True)
    _mk(data / "MyMod.esp")
    (game / "Skyrim.ccc").write_text("ccCustomContent.esl\n")
    coll = {"plugins": [{"name": "MyMod.esp", "enabled": True}]}
    _, pl_path, _ = lo.sort_plugins(coll, str(data), str(lad))
    names = [l.lstrip("*").lower() for l in open(pl_path).read().splitlines() if not l.startswith("#")]
    # CC content is native -> NOT listed in plugins.txt; the regex alone would've missed it
    assert "cccustomcontent.esl" not in names
    assert "mymod.esp" in names


def test_no_ccc_file_falls_back_to_regex(tmp_path):
    game = tmp_path / "Skyrim Special Edition"; data = game / "Data"; lad = tmp_path / "lad"
    data.mkdir(parents=True); lad.mkdir()
    _mk(data / "ccBGSSSE001-Fish.esm", master=True)   # matches the regex
    _mk(data / "MyMod.esp")
    coll = {"plugins": [{"name": "MyMod.esp", "enabled": True}]}
    _, pl_path, _ = lo.sort_plugins(coll, str(data), str(lad))
    names = [l.lstrip("*").lower() for l in open(pl_path).read().splitlines() if not l.startswith("#")]
    assert "ccbgssse001-fish.esm" not in names   # regex still excludes standard CC
