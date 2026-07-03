"""One-shot diagnostic for 'tool can't read the Vortex DB'.

Run from the project root on the machine where Vortex lives:

    python diagnose_vortex_read.py

It walks every step the app uses to read state.v2 and prints exactly which one
fails (DB discovery -> node bridge -> probe -> games -> collection identity),
plus the raw bridge stdout/stderr when a read errors.
"""

from __future__ import annotations

import os
import shutil
import sys
import subprocess

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from utils import vortex_db


def hr(label: str) -> None:
    print(f"\n=== {label} ===")


def main() -> None:
    hr("environment")
    print("APPDATA     :", os.environ.get("APPDATA"))
    print("PROGRAMDATA :", os.environ.get("PROGRAMDATA"))
    print("cwd         :", os.getcwd())

    hr("node + bridge")
    bridge = vortex_db._bridge_path()
    print("bridge path :", bridge, "exists:", os.path.exists(bridge))
    print("PATH `node` :", shutil.which("node"))
    print("candidates  :")
    for c in vortex_db._node_candidates():
        print("   ", c)
    try:
        node = vortex_db.resolve_node()
        v = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=15)
        print("RESOLVED    :", node, "->", (v.stdout or v.stderr).strip())
    except Exception as e:
        print("RESOLVED    : FAILED ->", type(e).__name__, e)
    classic = os.path.join(os.path.dirname(bridge), "node_modules", "classic-level")
    print("classic-level dir exists:", os.path.isdir(classic))

    hr("DB discovery (find_state_db)")
    # Show every candidate, whether it exists, and which one wins.
    appdata = os.environ.get("APPDATA")
    programdata = os.environ.get("PROGRAMDATA")
    cands = []
    if appdata:
        cands.append(os.path.join(appdata, "Vortex", "state.v2"))
    if programdata:
        cands.append(os.path.join(programdata, "vortex", "state.v2"))
    cands.append(os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "Vortex", "state.v2"))
    for c in cands:
        real = os.path.realpath(c)
        exists = os.path.isdir(c)
        n = len(os.listdir(c)) if exists else 0
        print(f"  cand: {c}\n        isdir={exists} files={n} realpath={real}")
    db = vortex_db.find_state_db()
    print("CHOSEN db   :", db)
    if not db:
        print("\n>>> find_state_db() returned None — no state.v2 directory found. "
              "Open Vortex once so it creates one, or the path moved.")
        return

    hr("probe (can we open it exclusively?)")
    try:
        res = vortex_db._run_bridge("probe", db, timeout=30)
        print("rc:", res.returncode)
        print("stdout:", (res.stdout or "").strip()[:500])
        print("stderr:", (res.stderr or "").strip()[:1000])
    except Exception as e:
        print("probe raised:", type(e).__name__, e)

    hr("read_vortex_games")
    try:
        games = vortex_db.read_vortex_games(db)
        print("games:", games)
    except Exception as e:
        print("read_vortex_games raised:", type(e).__name__, e)

    hr("collection_diagnostic / identity")
    try:
        print(vortex_db.collection_diagnostic(db, "skyrimse"))
        print("identity:", vortex_db.read_collection_identity(db, "skyrimse"))
        print("instanceId:", vortex_db.read_app_instance_id(db))
        print("activeProfile:", vortex_db.read_active_profile(db))
    except Exception as e:
        print("identity reads raised:", type(e).__name__, e)


if __name__ == "__main__":
    main()
