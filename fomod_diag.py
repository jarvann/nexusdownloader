#!/usr/bin/env python3
"""Diagnose / audit FOMOD resolution — what files would the engine install?

Runs the REAL resolver (utils.fomod_engine.resolve_install) against a mod's
archive + the collection's recorded choices, using the collection's declared
plugin list as the active set (so fileDependency conditions are evaluated the way
the installer now evaluates them). No staging needed.

Run from the repo root:
    # Diagnose one mod (substring of its name) -- prints resolved files + report:
    python fomod_diag.py --mod "Heavy Armory"

    # Audit ALL FOMOD mods for stray plugins (a plugin the collection never
    # declared) -- the over-install pre-flight. --limit to sample:
    python fomod_diag.py --audit
    python fomod_diag.py --audit --limit 50 --json L:\\audit.json

A stray plugin in the audit means the resolver would install a .esp/.esm/.esl the
collection's `plugins` list doesn't include -- the phantom-master signature.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from utils import install_verify as iv  # noqa: E402

if os.name == "nt":
    DEF_STAGING = r"L:\VortexMods\skyrimse"
    DEF_DOWNLOADS = r"L:\VortexDownloads\skyrimse"
else:
    DEF_STAGING = "/mnt/l/VortexMods/skyrimse"
    DEF_DOWNLOADS = "/mnt/l/vortexdownloads/skyrimse"

_PLUGIN_EXTS = (".esp", ".esm", ".esl")


def _resolve_mod(mod, downloads, active_plugins, temp_root=None):
    """Return (file_dests, report) for a mod, or (None, reason)."""
    from utils.archive_handler import get_archive_handler
    from utils.fomod_engine import find_moduleconfig, parse_moduleconfig, resolve_install

    src = mod.get("source") or {}
    mid = src.get("modId")
    archive = iv._glob_archive(downloads, int(mid)) if mid is not None else None
    if not archive or not os.path.exists(archive):
        return None, "archive not found in downloads"
    choices = mod.get("choices") or {}
    if choices.get("type") != "fomod":
        return None, "not a FOMOD (simple install)"

    handler = get_archive_handler()
    tmp = tempfile.mkdtemp(prefix="diag_", dir=temp_root or None)
    try:
        handler.extract_archive(archive, tmp)
        mc = find_moduleconfig(tmp)
        if not mc:
            return None, "no ModuleConfig.xml in archive"
        cfg = parse_moduleconfig(mc)
        ops, report = resolve_install(cfg, choices, mc.parent.parent,
                                      active_plugins=active_plugins)
        dests = [op.destination.replace("\\", "/") for op in ops
                 if os.path.isfile(op.abs_source)]
        return dests, report
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--staging", default=DEF_STAGING)
    ap.add_argument("--downloads", default=DEF_DOWNLOADS)
    ap.add_argument("--collection", default="")
    ap.add_argument("--mod", default="", help="diagnose one mod (name substring)")
    ap.add_argument("--audit", action="store_true", help="audit every FOMOD mod for stray plugins")
    ap.add_argument("--limit", type=int, default=None, help="cap --audit to N mods")
    ap.add_argument("--temp", default=None)
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)

    coll_path = args.collection or iv.find_collection_json(args.staging, args.downloads)
    if not coll_path or not os.path.exists(coll_path):
        print("could not locate collection.json")
        return 1
    collection = iv.load_collection(coll_path)
    active = {p.get("name", "").lower() for p in collection.get("plugins", []) if p.get("name")}
    print(f"collection : {os.path.basename(os.path.dirname(coll_path))}")
    print(f"plugins    : {len(active)} declared (active set for fileDependency)")
    print("-" * 70)

    mods = [mod for _mid, _fid, _s, mod in iv.iter_nexus_mods(collection)]

    if args.mod:
        frag = args.mod.lower()
        hits = [m for m in mods if frag in (m.get("name", "").lower())]
        if not hits:
            print(f"no mod matching '{args.mod}'")
            return 1
        for m in hits[:10]:
            dests, report = _resolve_mod(m, args.downloads, active, args.temp)
            print(f"\n=== {m.get('name')} ===")
            if dests is None:
                print(f"  ({report})")
                continue
            plugins = [d for d in dests if d.lower().endswith(_PLUGIN_EXTS)]
            stray = [p for p in plugins if os.path.basename(p).lower() not in active]
            print(f"  chosen plugins : {report.chosen_plugins}")
            print(f"  flags set      : {report.flags_set}")
            print(f"  conditionals   : {report.conditional_patterns_applied} applied")
            if report.unmatched_selections:
                print(f"  UNMATCHED      : {report.unmatched_selections}")
            print(f"  files          : {len(dests)} ({len(plugins)} plugins)")
            if stray:
                print(f"  STRAY PLUGINS  : {stray}  <-- not in the collection")
            else:
                print(f"  no stray plugins ✓")
        return 0

    if args.audit:
        fomods = [m for m in mods if (m.get("choices") or {}).get("type") == "fomod"]
        if args.limit:
            fomods = fomods[:args.limit]
        print(f"auditing {len(fomods)} FOMOD mods...")
        flagged = []
        for i, m in enumerate(fomods, 1):
            print(f"\r  [{i}/{len(fomods)}] {m.get('name','')[:50]:<50}", end="", flush=True)
            dests, report = _resolve_mod(m, args.downloads, active, args.temp)
            if dests is None:
                continue
            plugins = [d for d in dests if d.lower().endswith(_PLUGIN_EXTS)]
            stray = sorted({os.path.basename(p) for p in plugins
                            if os.path.basename(p).lower() not in active})
            if stray:
                flagged.append({"mod": m.get("name"), "stray": stray})
        print(f"\n\n{'=' * 70}")
        print(f"FOMOD mods that would install stray plugins: {len(flagged)}")
        for f in flagged:
            print(f"  {f['mod']}: {f['stray']}")
        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump({"flagged": flagged}, fh, indent=2)
            print(f"\nWrote {args.json}")
        return 0 if not flagged else 2

    print("Pass --mod NAME to diagnose one, or --audit to scan all FOMOD mods.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
