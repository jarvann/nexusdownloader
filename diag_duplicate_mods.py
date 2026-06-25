#!/usr/bin/env python3
"""READ-ONLY diagnosis of Vortex's "potential duplicate mods" warning.

Vortex flags two mod records as duplicates when they have different ids but the
SAME archiveId and the same variant (mod_management/index.ts getDuplicateMods).
This reads every persistent.mods.skyrimse.<folder> record, groups by archiveId,
and reports collisions -- showing for each which records are CURRENT (staging
folder still exists + enabled in the active profile) vs STALE (folder gone /
not enabled), so we know exactly which to remove for a clean state.

Does NOT write anything. Requires Vortex CLOSED (LevelDB single-writer lock).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from utils import vortex_db  # noqa: E402

GAME = "skyrimse"
STAGING = r"L:\VortexMods\skyrimse" if os.name == "nt" else "/mnt/l/VortexMods/skyrimse"


def main():
    db = os.environ.get("VORTEX_DB") or vortex_db.find_state_db()
    print(f"state.v2: {db}")
    if not vortex_db.probe(db):
        print("!! DB not readable -- is Vortex still open? Close it and retry.")
        return 1

    base = f"persistent###mods###{GAME}###"
    arc = vortex_db.read_prefix(db, base, suffix="###archiveId")
    var = vortex_db.read_prefix(db, base, suffix="###attributes###variant")

    # map folder -> archiveId
    def folder_of(key, suffix):
        return key[len(base):-len(suffix)]
    by_folder_arc = {folder_of(k, "###archiveId"): v for k, v in arc.items()}
    by_folder_var = {folder_of(k, "###attributes###variant"): v for k, v in var.items()}
    print(f"mod records: {len(by_folder_arc)}")

    # active profile modState (enabled folders)
    prof = vortex_db.read_active_profile(db) if hasattr(vortex_db, "read_active_profile") else None
    enabled = set()
    if prof:
        ms = vortex_db.read_prefix(db, f"persistent###profiles###{prof}###modState###",
                                   suffix="###enabled")
        pre = f"persistent###profiles###{prof}###modState###"
        enabled = {k[len(pre):-len("###enabled")] for k, v in ms.items() if v in (True, "true")}
    print(f"active profile: {prof}  enabled mods: {len(enabled)}")

    staging_dirs = set(os.listdir(STAGING)) if os.path.isdir(STAGING) else set()

    # group folders by (archiveId, variant)
    import collections
    groups = collections.defaultdict(list)
    for folder, aid in by_folder_arc.items():
        if not aid:
            continue
        groups[(aid, by_folder_var.get(folder, ""))].append(folder)

    collisions = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"\narchiveId+variant COLLISIONS (Vortex duplicates): {len(collisions)}")
    stale_total, current_total = [], []
    for (aid, variant), folders in sorted(collisions.items(), key=lambda x: -len(x[1]))[:40]:
        print(f"\n  archiveId={aid}  variant={variant!r}  ({len(folders)} records)")
        for f in folders:
            on_disk = f in staging_dirs
            en = f in enabled
            tag = "CURRENT" if (on_disk and en) else "STALE  "
            (current_total if tag.strip() == "CURRENT" else stale_total).append(f)
            print(f"      [{tag}] disk={on_disk!s:5} enabled={en!s:5}  {f}")

    # full totals (not just shown 40)
    sd, cd = 0, 0
    for folders in collisions.values():
        for f in folders:
            if f in staging_dirs and f in enabled:
                cd += 1
            else:
                sd += 1
    print(f"\n=== SUMMARY ===")
    print(f"collision groups : {len(collisions)}")
    print(f"records in them  : {sum(len(v) for v in collisions.values())}")
    print(f"  CURRENT (keep) : {cd}")
    print(f"  STALE (remove) : {sd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
