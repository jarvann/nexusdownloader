"""
Guarded access layer for Vortex's state.v2 LevelDB.

Vortex holds an exclusive lock on its DB while running, and writing to it
concurrently would corrupt it (we saw Vortex freeze/crash during a bulk in-app
op). So every write here is gated by a lock/concurrency check:

* :func:`probe` asks the Node bridge whether the DB can be opened exclusively
  (i.e. Vortex is *not* holding the lock).
* :func:`is_vortex_running` is a secondary signal via process inspection.
* :func:`ensure_available` raises :class:`VortexBusyError` (with a "close Vortex"
  message) if either check says Vortex is active -- call it before *and*
  periodically *during* long operations.
* :func:`write_records` backs up the DB, re-checks availability, then applies the
  batch atomically through the bridge.

The actual LevelDB I/O is delegated to ``vortex_leveldb.js`` (Node + classic-level)
because Python has no reliable LevelDB writer on Windows.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a listed dependency
    psutil = None
_PSUTIL = psutil is not None


class VortexBusyError(RuntimeError):
    """Raised when Vortex is running / holding the DB lock and we must not write."""


class NodeNotFoundError(RuntimeError):
    """Raised when the Node.js runtime the LevelDB bridge needs can't be located.

    Kept distinct from :class:`VortexBusyError` so a missing runtime is reported
    as exactly that ("install Node / fix PATH") instead of masquerading as a
    locked database.
    """


def _bridge_path() -> str:
    # vortex_leveldb.js sits at the project root next to run_gui.py
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "..", "vortex_leveldb.js"))


def _node_candidates() -> List[str]:
    """Likely ``node`` executables, in priority order.

    PATH first (the common case), then an explicit override env var, then the
    standard Windows/Unix install locations -- so a process launched without the
    user's interactive PATH (a GUI double-click, a fresh terminal after a Node
    reinstall) can still find it.
    """
    out: List[str] = []

    def add(p: Optional[str]) -> None:
        if p and p not in out:
            out.append(p)

    # Explicit override wins if set.
    add(os.environ.get("NEXUS_NODE") or os.environ.get("NODE"))
    # Whatever PATH resolves (handles nvm/volta shims, custom installs).
    add(shutil.which("node"))
    add(shutil.which("node.exe"))

    if os.name == "nt":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        appdata = os.environ.get("APPDATA", "")
        for base in (pf, pf86):
            add(os.path.join(base, "nodejs", "node.exe"))
        # nvm-windows symlinks the active version here; also raw npm shim dir.
        add(os.path.join(appdata, "npm", "node.exe") if appdata else None)
        for root in (os.path.join(local, "Programs", "nodejs") if local else "",
                     os.path.join(appdata, "nvm") if appdata else "",
                     os.environ.get("NVM_HOME", ""), os.environ.get("NVM_SYMLINK", "")):
            if root:
                add(os.path.join(root, "node.exe"))
                for hit in sorted(glob.glob(os.path.join(root, "v*", "node.exe")), reverse=True):
                    add(hit)
    else:
        for p in ("/usr/local/bin/node", "/usr/bin/node", "/opt/homebrew/bin/node"):
            add(p)
        nvm = os.path.join(os.path.expanduser("~"), ".nvm", "versions", "node")
        for hit in sorted(glob.glob(os.path.join(nvm, "*", "bin", "node")), reverse=True):
            add(hit)
    return out


_NODE_CACHE: Optional[str] = None


def resolve_node() -> str:
    """Return a usable ``node`` executable path, or raise :class:`NodeNotFoundError`.

    Result is cached for the process. The chosen candidate is verified by running
    ``node --version`` so a stale path (uninstalled version) is skipped rather
    than failing later mid-read.
    """
    global _NODE_CACHE
    if _NODE_CACHE:
        return _NODE_CACHE
    tried = _node_candidates()
    for cand in tried:
        try:
            r = subprocess.run([cand, "--version"], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0:
            _NODE_CACHE = cand
            return cand
    raise NodeNotFoundError(
        "Node.js could not be found. The Vortex database bridge needs Node to "
        "read/write state.v2. Install Node.js (https://nodejs.org) and ensure "
        "'node' is on your PATH, or set the NEXUS_NODE environment variable to "
        "the full path of node.exe.\n  Looked in: "
        + (", ".join(tried) if tried else "(PATH only — nothing found)"))


def _run_bridge(cmd: str, db_path: str, *args: Optional[str],
                node: Optional[str] = None, timeout: float = 120.0) -> subprocess.CompletedProcess:
    node = node or resolve_node()
    argv = [node, _bridge_path(), cmd, db_path]
    argv.extend(a for a in args if a is not None)
    # The bridge emits UTF-8 JSON (mod names contain non-ASCII chars). Decode as
    # UTF-8 explicitly -- on Windows text=True defaults to cp1252, which crashes
    # the stdout reader thread on bytes like 0x81/0x9d and returns empty output.
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")


def probe(db_path: str, node: Optional[str] = None) -> bool:
    """Return True if the DB can be opened exclusively (Vortex not holding it)."""
    try:
        res = _run_bridge("probe", db_path, node=node, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return res.returncode == 0 and "AVAILABLE" in res.stdout


def is_vortex_running() -> bool:
    """Best-effort check for a running Vortex process."""
    if psutil is None:
        return False
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name in ("vortex.exe", "vortex"):
            return True
    return False


def ensure_available(db_path: str, node: Optional[str] = None) -> None:
    """Raise :class:`VortexBusyError` if Vortex is running or holds the DB lock."""
    if is_vortex_running():
        raise VortexBusyError(
            "Vortex is currently running. Close Vortex completely before "
            "syncing, then try again.")
    if not probe(db_path, node=node):
        raise VortexBusyError(
            "Vortex's database is locked (Vortex may still be running or "
            "shutting down). Make sure Vortex is fully closed, then try again.")


@dataclass
class WriteResult:
    keys_written: int
    backup_path: str


def backup_db(db_path: str) -> str:
    """Copy state.v2 to a timestamped sibling backup and return its path."""
    ts = time.strftime("%Y%m%d_%H%M%S")
    bak = f"{db_path.rstrip(os.sep)}.bak-{ts}"
    shutil.copytree(db_path, bak)
    return bak


def write_records(db_path: str, records: Dict[str, str], *,
                  backup: bool = True, node: Optional[str] = None,
                  delete_prefixes: Optional[list] = None) -> WriteResult:
    """Atomically write ``{full###key: json_value}`` records after safety checks.

    ``delete_prefixes`` (optional) removes every key under each prefix in the same
    batch -- used to replace an old collection revision cleanly. Re-checks
    availability immediately before writing (defends against Vortex being
    launched between an earlier check and this call).
    """
    ensure_available(db_path, node=node)
    bak = backup_db(db_path) if backup else ""

    # Re-check right before the write to narrow the race window.
    ensure_available(db_path, node=node)

    batch = {"put": records, "delPrefixes": list(delete_prefixes or [])}
    fd, batch_file = tempfile.mkstemp(suffix=".json", prefix="vortex_batch_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(batch, fh)
        res = _run_bridge("write", db_path, batch_file, node=node)
        if res.returncode == 3:
            raise VortexBusyError(
                "Vortex locked the database mid-write. Close Vortex and retry.")
        if res.returncode != 0:
            raise RuntimeError(f"DB write failed: {res.stderr.strip()}")
        return WriteResult(int(res.stdout.strip() or "0"), bak)
    finally:
        try:
            os.remove(batch_file)
        except OSError:
            pass


def read_prefix(db_path: str, prefix: str, node: Optional[str] = None,
                suffix: Optional[str] = None) -> Dict[str, object]:
    """Read keys under ``prefix`` (optionally only those ending with ``suffix``).

    The ``suffix`` filter keeps reads small -- e.g. fetching just collectionId
    keys instead of the whole (huge) mods section -- which is far more robust
    under heavy disk load.
    """
    res = _run_bridge("read", db_path, prefix, suffix, node=node)
    if res.returncode != 0:
        raise RuntimeError(f"DB read failed: {res.stderr.strip()}")
    raw = json.loads(res.stdout or "{}")
    return {k: json.loads(v) for k, v in raw.items()}


# --------------------------------------------------------------------------- #
# Discovery helpers (find the DB, active profile, and collection identity)
# --------------------------------------------------------------------------- #
def _db_recency(db_dir: str) -> float:
    """Most-recent mtime among a state.v2's files (dir mtime is unreliable)."""
    try:
        return max((os.path.getmtime(os.path.join(db_dir, f)) for f in os.listdir(db_dir)),
                   default=0.0)
    except OSError:
        return 0.0


def find_state_db() -> Optional[str]:
    """Locate Vortex's active ``state.v2``.

    Vortex runs in two modes with different data dirs, and a user may switch
    between them:
      * per-user (default): ``%APPDATA%/Vortex``
      * shared:            ``%PROGRAMDATA%/vortex``
    Both are checked (each may itself be a junction); when both exist we return
    the **most recently written** one -- i.e. the mode currently in use.
    """
    candidates = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(os.path.join(appdata, "Vortex", "state.v2"))          # per-user
    programdata = os.environ.get("PROGRAMDATA")
    if programdata:
        candidates.append(os.path.join(programdata, "vortex", "state.v2"))       # shared
    candidates.append(os.path.join(os.path.expanduser("~"), "AppData", "Roaming",
                                   "Vortex", "state.v2"))

    seen, existing = set(), []
    for c in candidates:
        real = os.path.realpath(c)   # collapse junctions so we don't double-count
        if real in seen:
            continue
        seen.add(real)
        if os.path.isdir(c):
            existing.append(c)
    if not existing:
        return None
    existing.sort(key=_db_recency, reverse=True)
    return existing[0]


def read_app_instance_id(db_path: str, node: Optional[str] = None) -> Optional[str]:
    """Return Vortex's ``app.instanceId`` -- the value the deployment manifest's
    ``instance`` field MUST match.

    On a deploy, ``loadActivation`` compares the manifest's ``instance`` to
    ``state.app.instanceId``; if they differ it assumes another Vortex instance
    modded the game, PURGES the deployed files, and re-links everything from
    scratch. Writing this exact id into our manifest makes Vortex accept our
    deployment as its own (no purge, no full re-deploy).
    """
    data = read_prefix(db_path, "app###instanceId", node=node)
    val = data.get("app###instanceId")
    return val if isinstance(val, str) else None


def read_active_profile(db_path: str, node: Optional[str] = None) -> Optional[str]:
    """Return the id of the currently active Vortex profile."""
    data = read_prefix(db_path, "settings###profiles###activeProfileId", node=node)
    val = data.get("settings###profiles###activeProfileId")
    return val if isinstance(val, str) else None


def read_vortex_games(db_path: str, node: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read the mod-managed games Vortex knows about, with their paths.

    Modern Vortex keeps everything in the LevelDB (no JSON to parse), so we pull:
    * ``settings.gameMode.discovered.<game>`` -- install path / store / hidden
    * ``settings.mods.installPath.<game>``    -- staging path template
    * ``settings.downloads.path``             -- downloads path template
    The ``{game}`` token in the templates expands to the game id. Only games that
    are discovered (have an install path) AND have a staging path are returned
    (i.e. ones actually set up for mods). Requires Vortex CLOSED (LevelDB lock).
    """
    discovered = read_prefix(db_path, "settings###gameMode###discovered###", node=node)
    install_paths = read_prefix(db_path, "settings###mods###installPath###", node=node)
    dl_tmpl = read_prefix(db_path, "settings###downloads###path",
                          node=node).get("settings###downloads###path", "")

    by_game: Dict[str, Dict[str, Any]] = {}
    for k, v in discovered.items():
        parts = k.split("###")
        if len(parts) >= 5:
            by_game.setdefault(parts[3], {})[".".join(parts[4:])] = v

    games: List[Dict[str, Any]] = []
    for game, info in by_game.items():
        if info.get("hidden") is True or not info.get("path"):
            continue
        staging_tmpl = install_paths.get(f"settings###mods###installPath###{game}", "")
        staging = staging_tmpl.replace("{game}", game) if staging_tmpl else ""
        if not staging:
            continue   # discovered but not set up for mods
        games.append({
            "game": game,
            "install": info.get("path", ""),
            "staging": staging,
            "downloads": dl_tmpl.replace("{game}", game) if dl_tmpl else "",
            "store": info.get("store", ""),
        })
    return sorted(games, key=lambda g: g["game"])


def read_collection_identity(db_path: str, game: str = "skyrimse",
                             node: Optional[str] = None) -> Optional[Tuple[int, str]]:
    """Find an installed collection's ``(collectionId, slug)`` from existing state.

    Looks first in the installed mod records (the common "update" case), then
    falls back to the collection's DOWNLOAD record -- which survives a Vortex
    "remove collection" as long as the archives were kept -- so a clean re-Link
    after removing the collection still resolves the identity. Returns None only
    when neither source has it.
    """
    base = f"persistent###mods###{game}###"
    # Two small, filtered reads instead of pulling the whole mods section (which
    # includes the multi-hundred-KB collection `rules` value and can choke a read
    # under heavy disk load).
    cid_data = read_prefix(db_path, base, node=node, suffix="###attributes###collectionId")
    slug_data = read_prefix(db_path, base, node=node, suffix="###attributes###collectionSlug")
    cid = next((v for v in cid_data.values() if isinstance(v, int)), None)
    slug = next((v for v in slug_data.values() if isinstance(v, str)), None)
    if cid is not None and slug:
        return (cid, slug)

    # Fallback: the collection .7z download record carries the same identity under
    # modInfo.nexus.ids -- present even after the collection mod is removed.
    dl = "persistent###downloads###files###"
    cid_dl = read_prefix(db_path, dl, node=node, suffix="###modInfo###nexus###ids###collectionId")
    slug_dl = read_prefix(db_path, dl, node=node, suffix="###modInfo###nexus###ids###collectionSlug")
    cid = cid if cid is not None else next(
        (int(v) for v in cid_dl.values() if isinstance(v, (int, str)) and str(v).isdigit()), None)
    slug = slug or next((v for v in slug_dl.values() if isinstance(v, str)), None)
    return (cid, slug) if (cid is not None and slug) else None


def collection_diagnostic(db_path: str, game: str = "skyrimse", node: Optional[str] = None) -> str:
    """Short diagnostic of what the DB read sees -- included in errors so a failure
    is self-explaining (db path, how many mods / collection records were found)."""
    try:
        states = read_prefix(db_path, f"persistent###mods###{game}###",
                             node=node, suffix="###state")
        types = read_prefix(db_path, f"persistent###mods###{game}###",
                            node=node, suffix="###type")
        cids = read_prefix(db_path, f"persistent###mods###{game}###",
                          node=node, suffix="###attributes###collectionId")
        collections = sum(1 for v in types.values() if v == "collection")
        return (f"db={db_path}; mods={len(states)}, collections={collections}, "
                f"collectionId_keys={len(cids)}")
    except Exception as e:
        return f"db={db_path}; diagnostic read failed: {e}"
