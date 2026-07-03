import os, sys, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from pathlib import Path
from utils import deploy_engine as de
from utils import vortex_deploy as vd


def _seed(tmp):
    staging = tmp / "staging"; game = tmp / "game"; data = game / "Data"
    data.mkdir(parents=True); staging.mkdir()
    # A normal mod (stripped to Data already at install) -> deploys to Data
    (staging / "RegMod").mkdir()
    (staging / "RegMod" / "MyMod.esp").write_text("esp")
    (staging / "RegMod" / "textures").mkdir()
    (staging / "RegMod" / "textures" / "t.dds").write_text("tex")
    # An SKSE mod staged WHOLE: loader at root + a Data/ subtree -> deploys to game root
    skse = staging / "SKSE"; (skse / "Data" / "SKSE" / "Plugins").mkdir(parents=True)
    (skse / "skse64_loader.exe").write_text("loader")
    (skse / "skse64_1_6_640.dll").write_text("dll")
    (skse / "Data" / "SKSE" / "Plugins" / "p.dll").write_text("plugin")
    return staging, game, data


def test_engine_routes_each_type_to_its_target(tmp_path):
    staging, game, data = _seed(tmp_path)
    res = de.deploy_all(str(staging), str(data), str(game),
                        ["RegMod", "SKSE"], instance_id="inst-1")
    # Regular mod -> Data
    assert (data / "MyMod.esp").exists()
    assert (data / "textures" / "t.dds").exists()
    # SKSE loader -> GAME ROOT (next to where SkyrimSE.exe would be), NOT Data
    assert (game / "skse64_loader.exe").exists()
    assert (game / "skse64_1_6_640.dll").exists()
    assert not (data / "skse64_loader.exe").exists()
    # SKSE's Data/ subtree -> the real Data folder (game_root/Data == Data)
    assert (data / "SKSE" / "Plugins" / "p.dll").exists()
    # Two separate manifests, one per type
    assert (data / "vortex.deployment.json").exists()
    assert (game / "vortex.deployment.skse.json").exists()
    assert "skse" in res and "" in res


def test_engine_skips_root_type_when_game_root_unknown(tmp_path):
    staging, game, data = _seed(tmp_path)
    logs = []
    res = de.deploy_all(str(staging), str(data), None, ["RegMod", "SKSE"],
                        instance_id="inst-1", log=lambda lvl, m: logs.append((lvl, m)))
    assert (data / "MyMod.esp").exists()          # default type still deploys
    assert not (game / "skse64_loader.exe").exists()  # root type skipped, not misplaced
    assert not (data / "skse64_loader.exe").exists()  # crucially NOT dumped into Data
    assert any("game root" in m for _l, m in logs)
    assert "skse" not in res


def test_purge_all_clears_every_type(tmp_path):
    staging, game, data = _seed(tmp_path)
    de.deploy_all(str(staging), str(data), str(game), ["RegMod", "SKSE"], instance_id="i")
    pres = de.purge_all(str(staging), str(data), str(game), force=True, workers=4)
    assert not (game / "skse64_loader.exe").exists()
    assert not (data / "MyMod.esp").exists()
    assert not (data / "SKSE" / "Plugins" / "p.dll").exists()
    assert pres[""].remaining == 0 and pres["skse"].remaining == 0


def test_stale_loader_removed_on_redeploy(tmp_path):
    staging, game, data = _seed(tmp_path)
    de.deploy_all(str(staging), str(data), str(game), ["RegMod", "SKSE"], instance_id="i")
    # drop the extra dll from SKSE, redeploy -> it must be unlinked from game root
    (staging / "SKSE" / "skse64_1_6_640.dll").unlink()
    de.deploy_all(str(staging), str(data), str(game), ["RegMod", "SKSE"], instance_id="i")
    assert not (game / "skse64_1_6_640.dll").exists()
    assert (game / "skse64_loader.exe").exists()


def test_deploy_collection_routes_via_engine(tmp_path, monkeypatch):
    """The high-level deploy_collection must modtype-route (default->Data, skse->root)."""
    import utils.vortex_deploy as vdmod
    import utils.vortex_db as vdb
    staging, game, data = _seed(tmp_path)
    monkeypatch.setattr(vdb, "read_app_instance_id", lambda *a, **k: "inst-1")
    monkeypatch.setattr(vdmod, "mark_deployed_in_db", lambda *a, **k: None)
    result, _ = vdmod.deploy_collection(
        "fakedb", str(staging), str(data),
        enabled_folders=["RegMod", "SKSE"], game_root=str(game), node="node")
    assert (game / "skse64_loader.exe").exists()        # routed to game root
    assert (data / "MyMod.esp").exists()                # routed to Data
    assert (data / "SKSE" / "Plugins" / "p.dll").exists()
    assert result.files > 0                              # aggregated across types
