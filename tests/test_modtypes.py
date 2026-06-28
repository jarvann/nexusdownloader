import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from utils import modtypes as mt


def test_skse_routes_to_game_root_unstripped():
    t = mt.classify({"skse64_loader.exe", "skse64_1_6_640.dll", "Data"})
    assert t.type_id == "skse"
    assert t.to_game_root and not t.strip_to_data
    assert t.target("/game/Data", "/game") == "/game"
    assert t.manifest_name() == "vortex.deployment.skse.json"


def test_dinput_detected():
    t = mt.classify({"dinput8.dll"})
    assert t.type_id == "dinput" and t.to_game_root


def test_engine_fixes_part2_dlls_route_to_root():
    t = mt.classify({"d3dx9_42.dll", "tbbmalloc.dll"})
    assert t.type_id == "engine-injector" and t.to_game_root


def test_explicit_root_folder():
    t = mt.classify({"Root", "meshes"})
    assert t.type_id == "engine-injector"


def test_default_mod_routes_to_data_and_strips():
    t = mt.classify({"meshes", "textures", "MyMod.esp"})
    assert t.type_id == "" and not t.to_game_root and t.strip_to_data
    assert t.target("/game/Data", "/game") == "/game/Data"
    assert t.manifest_name() == "vortex.deployment.json"


def test_priority_skse_beats_engine_injector():
    # a mod with BOTH an skse loader and an enb dll -> skse wins (higher priority)
    t = mt.classify({"skse64_loader.exe", "d3d11.dll"})
    assert t.type_id == "skse"


def test_case_insensitive():
    assert mt.classify({"SKSE64_Loader.EXE"}).type_id == "skse"
