#!/usr/bin/env python3
"""
Find (and optionally clear) mods left INCOMPLETE by the old Windows MAX_PATH bug,
so a re-run reinstalls them correctly with the long-path (\\\\?\\) fix.

The old installer SILENTLY skipped any file whose full staging path exceeded 260
chars. A mod with a long folder name + deep paths (OAR animations, deep textures)
that ALSO had short-path files doesn't show up as a failure -- its folder has
content, so the skip-check considers it installed and won't re-attempt it. This
compares each mod's ARCHIVE contents to what's on disk and flags folders that are
missing files which would have been dropped (dest path > ~255). With --apply it
deletes those folders so the next install run rebuilds them cleanly.

Reads archives (header listing only, no extraction). FOMOD archives are reported
separately (their selected file set can't be derived without running the FOMOD),
and only deleted with --include-fomod.

Usage:
    python fix_longpath_mods.py                       # DRY RUN report
    python fix_longpath_mods.py --apply               # delete incomplete simple-mod folders
    python fix_longpath_mods.py --apply --include-fomod
"""

import argparse
import glob
import os
import shutil
import sys

if os.name == "nt":
    DEF_STAGING, DEF_DL = r"L:\VortexMods\skyrimse", r"L:\VortexDownloads"
else:
    DEF_STAGING, DEF_DL = "/mnt/l/VortexMods/skyrimse", "/mnt/l/VortexDownloads"

MAXPATH = 255          # old code dropped files whose dest path exceeded ~260
DATA_DIRS = {  # anchors for matching an archive file to its deployed staging path
    "meshes", "textures", "scripts", "sound", "music", "video", "interface",
    "seq", "grass", "lodsettings", "shadersfx", "source", "skse", "dialogueviews",
    "calientetools", "skyproc patchers", "strings", "facegen", "facegendata",
    "actors", "materials", "particles", "distantlod", "scripts.rar", "tools",
}
SKIP_EXT = (".txt", ".md", ".pdf", ".jpg", ".jpeg", ".png", ".webp", ".gif")
SKIP_NAME = ("readme", "license", "changelog", "meta.ini", "fomod")


def _list_archive(path):
    """Return the archive's member paths (lowercased, '/'-separated) or None."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".zip":
            import zipfile
            with zipfile.ZipFile(path) as z:
                return [n.replace("\\", "/").lower() for n in z.namelist() if not n.endswith("/")]
        if ext == ".7z":
            import py7zr
            with py7zr.SevenZipFile(path, "r") as z:
                return [n.replace("\\", "/").lower() for n in z.getnames()]
    except Exception:
        pass
    # universal fallback: external 7z header listing
    try:
        import subprocess
        out = subprocess.run(["7z", "l", "-ba", "-slt", path], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=120)
        names = []
        for line in out.stdout.splitlines():
            if line.startswith("Path = "):
                names.append(line[7:].replace("\\", "/").lower())
        return names or None
    except Exception:
        return None


def _data_rel(member):
    """The member path from its first data-dir segment (so it matches the deployed
    layout regardless of wrapper folders). None for non-data files."""
    parts = member.split("/")
    for i, seg in enumerate(parts):
        if seg in DATA_DIRS:
            return "/".join(parts[i:])
    return None


def _is_skippable(member):
    base = member.rsplit("/", 1)[-1]
    return base.endswith(SKIP_EXT) or any(s in base for s in SKIP_NAME)


def _folder_for(archive_name):
    stem = os.path.splitext(archive_name)[0]
    import re
    return re.sub(r'[<>:"/\\|?*]', "_", stem)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--staging", default=DEF_STAGING)
    ap.add_argument("--downloads", default=DEF_DL)
    ap.add_argument("--collection", help="collection.json (auto-detected in staging if omitted)")
    ap.add_argument("--include-fomod", action="store_true", help="also delete flagged FOMOD mods")
    ap.add_argument("--apply", action="store_true", help="delete incomplete folders (default: dry run)")
    args = ap.parse_args(argv)

    import json
    coll = args.collection
    if not coll:
        cands = glob.glob(os.path.join(args.staging, "*", "collection.json"))
        coll = max(cands, key=os.path.getmtime) if cands else None
    if not coll or not os.path.exists(coll):
        print("!! collection.json not found; pass --collection."); return 1
    mods = json.load(open(coll, encoding="utf-8")).get("mods", [])
    dl_index = {os.path.basename(p).lower(): p
                for p in glob.glob(os.path.join(args.downloads, "*")) if os.path.isfile(p)}
    print(f"collection : {coll}")
    print(f"staging    : {args.staging}   downloads: {args.downloads} ({len(dl_index)} archives)")
    print("-" * 72)

    incomplete, fomod_suspect, checked = [], [], 0
    import re
    MID = re.compile(r"-(\d{2,7})-")
    for m in mods:
        s = m.get("source") or {}
        mid = str(s.get("modId") or "")
        if not mid:
            continue
        # find this mod's archive in downloads (by modId token + name overlap)
        cands = [p for n, p in dl_index.items() if f"-{mid}-" in n]
        if not cands:
            continue
        archive = cands[0]
        if len(cands) > 1:
            target = (s.get("logicalFilename") or m.get("name") or "").lower().split()
            archive = max(cands, key=lambda p: sum(
                1 for w in target if len(w) > 3 and w in os.path.basename(p).lower()))
        folder = _folder_for(os.path.basename(archive))
        fdir = os.path.join(args.staging, folder)
        if not os.path.isdir(fdir) or not os.listdir(fdir):
            continue   # missing/empty -> the re-run's skip-check already re-attempts it
        members = _list_archive(archive)
        if not members:
            continue
        checked += 1
        is_fomod = any("moduleconfig.xml" in n for n in members)
        # at-risk = data files whose deployed path would exceed MAX_PATH
        at_risk_missing = []
        for n in members:
            if _is_skippable(n):
                continue
            rel = _data_rel(n)
            if not rel:
                continue
            dest = os.path.join(fdir, rel.replace("/", os.sep))
            if len(dest) > MAXPATH and not os.path.exists(dest):
                at_risk_missing.append(rel)
        if at_risk_missing:
            entry = (folder, len(at_risk_missing))
            (fomod_suspect if is_fomod else incomplete).append(entry)

    print(f"checked {checked} installed mods")
    print(f"\nINCOMPLETE (simple-install, missing deep files): {len(incomplete)}")
    for folder, n in sorted(incomplete, key=lambda x: -x[1])[:40]:
        print(f"   [{n:4d} missing]  {folder}")
    print(f"\nFOMOD suspects (review; rebuild only if in-game assets missing): {len(fomod_suspect)}")
    for folder, n in sorted(fomod_suspect, key=lambda x: -x[1])[:20]:
        print(f"   [{n:4d} missing?] {folder}")

    to_delete = incomplete + (fomod_suspect if args.include_fomod else [])
    print("-" * 72)
    if not to_delete:
        print("Nothing to rebuild.")
        return 0
    if not args.apply:
        print(f"DRY RUN. Re-run with --apply to delete {len(to_delete)} folder(s) "
              "(+ --include-fomod for the FOMOD ones), then re-run the install.")
        return 0
    removed = 0
    for folder, _ in to_delete:
        try:
            shutil.rmtree(os.path.join(args.staging, folder)); removed += 1
        except OSError as e:
            print(f"   could not remove {folder}: {e}")
    print(f"Deleted {removed} folder(s). Re-run the install to rebuild them with the long-path fix.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
