"""
Unified installation verifier — does what's on disk match what the collection
intended?

Ledger-FIRST by design. The old standalone scripts (verify_downloads.py,
audit_staging.py) re-derive mod identity from filenames with a crude regex and
have no view of the local ledger, which produces false "orphans" (e.g. a mod the
ledger positively identifies but the regex can't) and can't tell a manual/off-site
mod from a genuinely missing one. This engine uses the SQLite ledger
(:mod:`utils.local_state`) as the authoritative ``intended -> installed`` map and
only falls back to disk facts, so the answers are trustworthy at collection scale.

Three components:

1. ARCHIVE INTEGRITY  -- each download's on-disk archive matches the collection's
                         expected ``source.fileSize`` (and md5 when asked). The
                         archive path comes from the ledger download record, not a
                         glob, so we check the exact file we installed from.
2. STAGING CORRECTNESS-- every collection mod we should have installed has a
                         non-empty staging folder, matched via the ledger's
                         ``(modId, fileId)`` identity. Disk folders with no ledger
                         record are real ORPHANS; ledger mods with no Nexus
                         identity are MANUAL (info, not error). Also flags
                         file-count drift (ledger N vs disk M) -- a cheap
                         partial-install / post-install-deletion signal.
3. CONTENT FIDELITY   -- do the files in each mod's folder match what its installer
                         choices should have produced?
                         * cheap tier: compare the ledger's recorded
                           ``installer_choices`` to the collection's ``choices``
                           (a wrong-FOMOD-choice install is caught with NO disk read).
                         * deep tier (opt-in, scoped): re-extract the archive, run
                           the real FOMOD/simple resolver, and diff the expected
                           file set against what's on disk.

The engine returns a :class:`VerifyReport` and is import-friendly so a GUI
"Verify Installation" button can call :func:`verify` and render the findings.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from utils import local_state as ls

# Components and severities (kept as plain strings so the GUI/JSON stays simple).
ARCHIVE = "archive"
STAGING = "staging"
CONTENT = "content"
ERROR = "error"
WARN = "warn"
INFO = "info"

_ARCHIVE_EXTS = (".7z", ".zip", ".rar")


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #
@dataclass
class Finding:
    component: str          # ARCHIVE | STAGING | CONTENT
    severity: str           # ERROR | WARN | INFO
    folder: str             # mod folder / archive name the finding is about
    detail: str             # human-readable explanation
    data: Dict[str, Any] = field(default_factory=dict)  # machine-readable extras

    def __str__(self) -> str:
        return f"[{self.severity.upper()}] {self.component}: {self.folder} -- {self.detail}"


@dataclass
class VerifyReport:
    findings: List[Finding] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    def add(self, *args, **kw) -> None:
        self.findings.append(Finding(*args, **kw))

    def of(self, severity: str) -> List[Finding]:
        return [f for f in self.findings if f.severity == severity]

    def by_component(self, component: str) -> List[Finding]:
        return [f for f in self.findings if f.component == component]

    @property
    def ok(self) -> bool:
        """True when nothing rises to ERROR (warnings/info are non-fatal)."""
        return not self.of(ERROR)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "stats": self.stats,
            "counts": {s: len(self.of(s)) for s in (ERROR, WARN, INFO)},
            "findings": [vars(f) for f in self.findings],
        }


# --------------------------------------------------------------------------- #
# Path / collection / ledger resolution
# --------------------------------------------------------------------------- #
def find_collection_json(staging: str, downloads: str = "") -> Optional[str]:
    """Locate a collection.json under staging (or alongside downloads)."""
    cands: List[str] = []
    cands += glob.glob(os.path.join(staging, "*", "collection.json"))
    if downloads:
        cands += glob.glob(os.path.join(os.path.dirname(downloads), "*", "collection.json"))
        cands.append(os.path.join(downloads, "collection.json"))
    cands = [c for c in cands if os.path.exists(c)]
    if not cands:
        return None
    return max(cands, key=os.path.getmtime)


def load_collection(collection_path: str) -> Dict[str, Any]:
    with open(collection_path, "r", encoding="utf-8") as f:
        return json.load(f)


def iter_nexus_mods(collection: Dict[str, Any]):
    """Yield ``(modId, fileId, source, mod)`` for every Nexus-identified mod.

    Off-site/manual entries (no modId/fileId) are skipped -- they have no archive
    identity to verify against. ``optional`` lives on the mod entry.
    """
    for mod in collection.get("mods", []):
        src = mod.get("source") or {}
        mid, fid = src.get("modId"), src.get("fileId")
        if mid is None or fid is None:
            continue
        yield int(mid), int(fid), src, mod


# --------------------------------------------------------------------------- #
# Disk helpers
# --------------------------------------------------------------------------- #
def _count_files(path: str) -> int:
    n = 0
    for _root, _dirs, files in os.walk(path):
        n += len(files)
    return n


def _rel_files(path: str) -> Set[str]:
    """Lowercased, forward-slash relative paths of every file under ``path``."""
    base = Path(path)
    out: Set[str] = set()
    for p in base.rglob("*"):
        if p.is_file():
            out.add(p.relative_to(base).as_posix().lower())
    return out


def _md5(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Component 1 — archive integrity
# --------------------------------------------------------------------------- #
def verify_archives(collection: Dict[str, Any], ledger: ls.LocalState, *,
                    downloads: str = "", check_md5: bool = False,
                    workers: int = 8) -> List[Finding]:
    """Check each download's on-disk archive against the collection's expected
    size/md5. Uses the ledger's recorded ``local_path`` when present (exact file),
    falling back to a downloads-folder glob by modId only if the ledger has none."""
    findings: List[Finding] = []
    # ledger download path per (modId,fileId)
    dl_by_ids: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for r in ledger.all_mods_with_download():
        mid, fid = r.get("dl_mod_id"), r.get("dl_file_id")
        if mid is not None and fid is not None:
            dl_by_ids[(int(mid), int(fid))] = r

    jobs: List[Tuple[str, str, Set[int], Set[str]]] = []  # (label, path, sizes, md5s)
    for mid, fid, src, mod in iter_nexus_mods(collection):
        name = mod.get("name", f"{mid}-{fid}")
        exp_size = src.get("fileSize")
        exp_md5 = (src.get("md5") or "").lower()
        row = dl_by_ids.get((mid, fid))
        path = (row or {}).get("dl_local_path") if row else None
        if not path:
            # No ledger path -> glob the downloads dir by modId token as a fallback.
            path = _glob_archive(downloads, mid) if downloads else None
        if not path or not os.path.exists(path):
            findings.append(Finding(ARCHIVE, ERROR, name,
                            "archive missing on disk (no ledger path and none found)",
                            {"modId": mid, "fileId": fid}))
            continue
        sizes = {int(exp_size)} if isinstance(exp_size, int) else set()
        md5s = {exp_md5} if exp_md5 else set()
        jobs.append((name, path, sizes, md5s))

    def _check(job) -> Optional[Finding]:
        name, path, sizes, md5s = job
        try:
            actual = os.path.getsize(path)
        except OSError as e:
            return Finding(ARCHIVE, ERROR, name, f"cannot stat archive: {e}", {"path": path})
        if sizes and actual not in sizes:
            return Finding(ARCHIVE, ERROR, name,
                           f"size {actual:,} != expected {sorted(sizes)} (truncated/corrupt)",
                           {"path": path, "actual": actual, "expected": sorted(sizes)})
        if check_md5 and md5s:
            if _md5(path) not in md5s:
                return Finding(ARCHIVE, ERROR, name, "md5 mismatch (corrupt)", {"path": path})
        return None

    if check_md5:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(_check, jobs))
    else:
        results = [_check(j) for j in jobs]
    findings += [r for r in results if r is not None]
    return findings


def _glob_archive(downloads: str, mod_id: int) -> Optional[str]:
    for ext in _ARCHIVE_EXTS:
        hits = glob.glob(os.path.join(downloads, f"*-{mod_id}-*{ext}"))
        if hits:
            return max(hits, key=os.path.getsize)
    return None


# --------------------------------------------------------------------------- #
# Component 2 — staging correctness (ledger-first)
# --------------------------------------------------------------------------- #
def verify_staging(collection: Dict[str, Any], staging: str,
                   ledger: ls.LocalState) -> List[Finding]:
    findings: List[Finding] = []
    rows = ledger.all_mods_with_download()
    by_ids: Dict[Tuple[int, int], Dict[str, Any]] = {}
    ledger_folders: Set[str] = set()
    coll_folder = _collection_container(staging)
    for r in rows:
        ledger_folders.add(r.get("folder"))
        mid, fid = r.get("dl_mod_id"), r.get("dl_file_id")
        if mid is not None and fid is not None:
            by_ids[(int(mid), int(fid))] = r

    matched_folders: Set[str] = set()

    # 2a. Every collection mod should map to an installed, non-empty folder.
    for mid, fid, src, mod in iter_nexus_mods(collection):
        name = mod.get("name", f"{mid}-{fid}")
        required = not mod.get("optional")
        row = by_ids.get((mid, fid))
        if not row:
            sev = ERROR if required else INFO
            findings.append(Finding(STAGING, sev, name,
                            ("required mod not installed (no ledger record)" if required
                             else "optional mod not installed (skipped by choice)"),
                            {"modId": mid, "fileId": fid}))
            continue
        folder = row.get("folder")
        matched_folders.add(folder)
        fpath = os.path.join(staging, folder)
        if not os.path.isdir(fpath):
            findings.append(Finding(STAGING, ERROR, folder,
                            "ledger says installed but staging folder is missing",
                            {"modId": mid, "fileId": fid}))
            continue
        disk_n = _count_files(fpath)
        if disk_n == 0:
            findings.append(Finding(STAGING, ERROR, folder,
                            "staging folder is empty (failed/partial install)",
                            {"modId": mid, "fileId": fid}))
            continue
        led_n = row.get("file_count") or 0
        if led_n and disk_n != led_n:
            findings.append(Finding(STAGING, WARN, folder,
                            f"file-count drift: ledger {led_n} vs disk {disk_n}",
                            {"ledger": led_n, "disk": disk_n}))

    # 2b. Ledger mods that aren't part of the collection: extra or manual.
    for r in rows:
        folder = r.get("folder")
        if folder in matched_folders or folder == coll_folder:
            continue
        mid, fid = r.get("dl_mod_id"), r.get("dl_file_id")
        if mid is None or fid is None:
            findings.append(Finding(STAGING, INFO, folder,
                            "manual/off-site mod (no Nexus identity) — not from the collection",
                            {"path": r.get("dl_local_path")}))
        else:
            findings.append(Finding(STAGING, WARN, folder,
                            f"installed mod (modId {mid}, file {fid}) is not in the collection",
                            {"modId": mid, "fileId": fid}))

    # 2c. Disk folders with NO ledger record at all = true orphans.
    if os.path.isdir(staging):
        for d in os.listdir(staging):
            full = os.path.join(staging, d)
            if not os.path.isdir(full) or d.startswith("__vortex") or d == coll_folder:
                continue
            if d not in ledger_folders:
                findings.append(Finding(STAGING, WARN, d,
                                "staging folder with no ledger record (true orphan)"))
    return findings


def _collection_container(staging: str) -> Optional[str]:
    for d in glob.glob(os.path.join(staging, "*", "collection.json")):
        return os.path.basename(os.path.dirname(d))
    return None


# --------------------------------------------------------------------------- #
# Component 3 — content fidelity
# --------------------------------------------------------------------------- #
def _choice_set(choices: Optional[Dict[str, Any]]) -> Set[Tuple[str, str, str]]:
    """Normalized {(step, group, choiceName)} for a collection/ledger choices dict."""
    if not choices or choices.get("type") != "fomod":
        return set()
    from utils.fomod_engine import selections_from_collection
    out: Set[Tuple[str, str, str]] = set()
    for step, group, _idx, name in selections_from_collection(choices):
        out.add(((step or "").lower(), (group or "").lower(), (name or "").lower()))
    return out


def verify_content_cheap(collection: Dict[str, Any],
                         ledger: ls.LocalState) -> List[Finding]:
    """FAST HINT, NOT a clean bill of health. Compares the FOMOD choices we
    RECORDED installing against the collection's intended choices — no disk read.

    A difference here means the install is definitely wrong, so it's worth
    surfacing. But agreement does NOT certify the install: the recorded choices
    can match while the FOMOD engine still placed unintended files (a bug in
    resolution). Only :func:`verify_content_deep` (resolve collection intent ->
    diff vs disk) and :func:`verify_plugins` are authoritative. The collection is
    the source of truth; the ledger is used here only for the folder mapping."""
    findings: List[Finding] = []
    by_ids: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for r in ledger.all_mods_with_download():
        mid, fid = r.get("dl_mod_id"), r.get("dl_file_id")
        if mid is not None and fid is not None:
            by_ids[(int(mid), int(fid))] = r

    for mid, fid, src, mod in iter_nexus_mods(collection):
        coll_choices = mod.get("choices")
        if not coll_choices or coll_choices.get("type") != "fomod":
            continue  # nothing to compare for simple mods
        row = by_ids.get((mid, fid))
        if not row:
            continue  # missing install already reported by staging
        raw = row.get("installer_choices")
        led_choices = json.loads(raw) if raw else None
        want = _choice_set(coll_choices)
        got = _choice_set(led_choices)
        if want and got and want != got:
            findings.append(Finding(CONTENT, WARN, row.get("folder", mod.get("name", "")),
                            "installed FOMOD choices differ from the collection's",
                            {"missing": sorted(want - got), "extra": sorted(got - want)}))
        elif want and not got:
            findings.append(Finding(CONTENT, WARN, row.get("folder", mod.get("name", "")),
                            "collection specifies FOMOD choices but none were recorded at install",
                            {"expected": sorted(want)}))
    return findings


def verify_content_deep(collection: Dict[str, Any], staging: str, ledger: ls.LocalState, *,
                        only: Optional[Sequence[str]] = None, limit: Optional[int] = None,
                        temp_root: Optional[str] = None,
                        progress: Optional[Callable[[int, int, str], None]] = None
                        ) -> List[Finding]:
    """Re-extract each mod's archive, run the REAL installer resolver, and diff the
    expected file set against what's on disk. Expensive (extracts archives), so it
    is opt-in and scoped: pass ``only`` (folder names) or ``limit`` to bound it.

    ``only`` defaults to nothing -> every installed collection mod is checked; pass
    the folders flagged by the cheap tiers to verify just those.
    """
    import shutil
    import tempfile
    from utils.fomod_installer import FomodInstaller
    from utils.fomod_engine import find_moduleconfig, parse_moduleconfig, resolve_install

    installer = FomodInstaller(staging, temp_root=temp_root)
    handler = installer.archive_handler

    rows = {r.get("folder"): r for r in ledger.all_mods_with_download()}
    coll_by_ids = {(mid, fid): mod for mid, fid, _s, mod in iter_nexus_mods(collection)}

    # Build the work list: installed collection mods (optionally filtered).
    work: List[Tuple[str, Dict[str, Any], Dict[str, Any]]] = []
    for folder, row in rows.items():
        if only is not None and folder not in only:
            continue
        mid, fid = row.get("dl_mod_id"), row.get("dl_file_id")
        if mid is None or fid is None:
            continue
        mod = coll_by_ids.get((int(mid), int(fid)))
        if mod is None:
            continue
        archive = row.get("dl_local_path")
        if not archive or not os.path.exists(archive):
            continue
        work.append((folder, row, mod))
    if limit is not None:
        work = work[:limit]

    findings: List[Finding] = []
    total = len(work)
    for i, (folder, row, mod) in enumerate(work, 1):
        if progress:
            progress(i, total, folder)
        archive = row.get("dl_local_path")
        tmp = tempfile.mkdtemp(prefix="verify_", dir=temp_root or None)
        try:
            expected = _simulate_expected(installer, handler, Path(archive), mod, Path(tmp),
                                          find_moduleconfig, parse_moduleconfig, resolve_install)
            if expected is None:
                findings.append(Finding(CONTENT, INFO, folder,
                                "could not simulate (no ModuleConfig found in archive)"))
                continue
            on_disk = _rel_files(os.path.join(staging, folder))
            missing = expected - on_disk
            extra = on_disk - expected
            if missing or extra:
                sev = ERROR if missing else WARN
                findings.append(Finding(CONTENT, sev, folder,
                                f"content mismatch: {len(missing)} expected file(s) missing, "
                                f"{len(extra)} unexpected on disk",
                                {"missing": sorted(missing)[:50], "extra": sorted(extra)[:50],
                                 "missing_count": len(missing), "extra_count": len(extra)}))
        except Exception as e:  # extraction/resolve failure shouldn't abort the run
            findings.append(Finding(CONTENT, WARN, folder, f"deep check failed: {e}"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return findings


def _simulate_expected(installer, handler, archive: Path, mod: Dict[str, Any], tmp: Path,
                       find_moduleconfig, parse_moduleconfig, resolve_install) -> Optional[Set[str]]:
    """Expected relative file set (lowercased, posix) the installer would produce,
    computed WITHOUT copying into staging. Mirrors _install_simple/_install_fomod."""
    handler.extract_archive(archive, tmp)
    choices = (mod.get("choices") or {})
    if choices.get("type") == "fomod":
        mc = find_moduleconfig(tmp)
        if not mc:
            return None
        config = parse_moduleconfig(mc)
        ops, _report = resolve_install(config, choices, mc.parent.parent)
        return {op.destination.replace("\\", "/").lower()
                for op in ops if Path(op.abs_source).is_file()}
    # simple mod: copy the stage-root subtree minus skipped files
    root = installer._stage_root_for(tmp)
    out: Set[str] = set()
    for f in root.rglob("*"):
        if f.is_file():
            rel = f.relative_to(root)
            if not installer._should_skip_file(rel):
                out.add(rel.as_posix().lower())
    return out


# --------------------------------------------------------------------------- #
# Component 4 — plugins on disk vs the collection's intended plugin set
# --------------------------------------------------------------------------- #
_PLUGIN_EXTS = (".esp", ".esm", ".esl")


def verify_plugins(collection: Dict[str, Any], staging: str,
                   ledger: ls.LocalState) -> List[Finding]:
    """Find plugin files physically present in ENABLED mod folders that the
    collection never declared. A stray plugin is the classic wrong-FOMOD-choice
    symptom: it drags in masters (e.g. WACCF) the build doesn't include, producing
    Vortex's "dependencies don't exist" / missing-master crashes. The collection's
    ``plugins`` list is the authority; the ledger only tells us which folder is
    which and whether it's enabled.
    """
    from utils.vortex_loadorder import read_masters, is_vanilla_master

    intended = {p.get("name", "").lower() for p in collection.get("plugins", []) if p.get("name")}
    findings: List[Finding] = []
    if not intended:
        findings.append(Finding(CONTENT, INFO, "(plugins)",
                        "collection declares no plugins list — skipping plugin check"))
        return findings

    enabled_folders = {r.get("folder") for r in ledger.all_mods()
                       if r.get("enabled") and r.get("folder")}

    for folder in sorted(enabled_folders):
        base = os.path.join(staging, folder)
        if not os.path.isdir(base):
            continue
        for p in Path(base).rglob("*"):
            if not (p.is_file() and p.suffix.lower() in _PLUGIN_EXTS):
                continue
            pname = p.name.lower()
            if pname in intended:
                continue  # declared by the collection -> fine
            # Stray plugin. Report it and the non-vanilla masters it pulls in,
            # since those are the phantom dependencies.
            masters = read_masters(str(p))
            phantom = [m for m in masters
                       if m.lower() not in intended and not is_vanilla_master(m)]
            detail = f"unintended plugin '{p.name}' not in the collection"
            if phantom:
                detail += f"; depends on {phantom} (phantom dependency — the real cause of missing-master errors)"
            findings.append(Finding(CONTENT, WARN, folder, detail,
                            {"plugin": p.name, "masters": masters, "phantom_masters": phantom}))
    return findings


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def verify(staging: str, *, downloads: str = "", collection_path: str = "",
           check_md5: bool = False, deep: bool = False,
           deep_only: Optional[Sequence[str]] = None, deep_limit: Optional[int] = None,
           temp_root: Optional[str] = None, workers: int = 8,
           progress: Optional[Callable[[int, int, str], None]] = None) -> VerifyReport:
    """Run all three components and return a :class:`VerifyReport`.

    ``deep`` enables the extract-and-diff content tier. When ``deep`` is on and
    ``deep_only`` is None, the deep tier auto-scopes to the folders the cheap
    checks already flagged (so you don't re-extract 4,900 clean mods for nothing).
    """
    report = VerifyReport()
    collection_path = collection_path or find_collection_json(staging, downloads) or ""
    if not collection_path or not os.path.exists(collection_path):
        report.add(STAGING, ERROR, "(collection)", "could not locate collection.json")
        return report
    collection = load_collection(collection_path)
    ledger = ls.get_ledger(ls.db_path_for(staging))

    report.findings += verify_archives(collection, ledger, downloads=downloads,
                                       check_md5=check_md5, workers=workers)
    report.findings += verify_staging(collection, staging, ledger)
    report.findings += verify_plugins(collection, staging, ledger)
    report.findings += verify_content_cheap(collection, ledger)

    if deep:
        scope = deep_only
        if scope is None:
            # Auto-scope: anything the cheap tiers flagged is worth a deep look.
            scope = sorted({f.folder for f in report.findings
                            if f.severity in (ERROR, WARN) and f.component in (STAGING, CONTENT)})
        report.findings += verify_content_deep(collection, staging, ledger, only=scope,
                                                limit=deep_limit, temp_root=temp_root,
                                                progress=progress)

    report.stats = {
        "collection": os.path.basename(os.path.dirname(collection_path)),
        "collection_mods": sum(1 for _ in iter_nexus_mods(collection)),
        "ledger_mods": len(ledger.all_mods()),
        "deep": bool(deep),
    }
    return report
