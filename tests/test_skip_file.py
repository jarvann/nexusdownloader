import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from pathlib import Path
from utils.fomod_installer import FomodInstaller


def _i(tmp_path):
    s = tmp_path / "s"; s.mkdir(); return FomodInstaller(str(s))


def test_keeps_real_assets_that_contain_skip_words(tmp_path):
    i = _i(tmp_path)
    # the old substring bug skipped these; Vortex keeps them
    assert not i._should_skip_file(Path("textures/license_plate.dds"))
    assert not i._should_skip_file(Path("meshes/readme_sign.nif"))
    assert not i._should_skip_file(Path("scripts/changelog_board.pex"))


def test_keeps_docs_like_vortex(tmp_path):
    i = _i(tmp_path)
    assert not i._should_skip_file(Path("readme.txt"))
    assert not i._should_skip_file(Path("docs/manual.pdf"))


def test_skips_fomod_config_and_junk(tmp_path):
    i = _i(tmp_path)
    assert i._should_skip_file(Path("fomod/ModuleConfig.xml"))
    assert i._should_skip_file(Path("Thumbs.db"))
    assert i._should_skip_file(Path("sub/.DS_Store"))
    # but a folder literally containing assets named 'fomodish' is fine
    assert not i._should_skip_file(Path("fomodextras/x.dds"))
