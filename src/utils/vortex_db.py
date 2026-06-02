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

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a listed dependency
    psutil = None
_PSUTIL = psutil is not None


class VortexBusyError(RuntimeError):
    """Raised when Vortex is running / holding the DB lock and we must not write."""


def _bridge_path() -> str:
    # vortex_leveldb.js sits at the project root next to run_gui.py
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "..", "vortex_leveldb.js"))


def _run_bridge(cmd: str, db_path: str, arg: Optional[str] = None,
                node: str = "node", timeout: float = 120.0) -> subprocess.CompletedProcess:
    argv = [node, _bridge_path(), cmd, db_path]
    if arg is not None:
        argv.append(arg)
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def probe(db_path: str, node: str = "node") -> bool:
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


def ensure_available(db_path: str, node: str = "node") -> None:
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
                  backup: bool = True, node: str = "node") -> WriteResult:
    """Atomically write ``{full###key: json_value}`` records after safety checks.

    Re-checks availability immediately before writing (defends against Vortex
    being launched between an earlier check and this call).
    """
    ensure_available(db_path, node=node)
    bak = backup_db(db_path) if backup else ""

    # Re-check right before the write to narrow the race window.
    ensure_available(db_path, node=node)

    fd, batch_file = tempfile.mkstemp(suffix=".json", prefix="vortex_batch_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(records, fh)
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


def read_prefix(db_path: str, prefix: str, node: str = "node") -> Dict[str, object]:
    """Read all keys under ``prefix``; returns ``{key: decoded_value}``."""
    res = _run_bridge("read", db_path, prefix, node=node)
    if res.returncode != 0:
        raise RuntimeError(f"DB read failed: {res.stderr.strip()}")
    raw = json.loads(res.stdout or "{}")
    return {k: json.loads(v) for k, v in raw.items()}


# --------------------------------------------------------------------------- #
# Discovery helpers (find the DB, active profile, and collection identity)
# --------------------------------------------------------------------------- #
def find_state_db() -> Optional[str]:
    """Locate Vortex's ``state.v2`` directory (APPDATA/Vortex, possibly a junction)."""
    candidates = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(os.path.join(appdata, "Vortex", "state.v2"))
    home = os.path.expanduser("~")
    candidates.append(os.path.join(home, "AppData", "Roaming", "Vortex", "state.v2"))
    for c in candidates:
        if os.path.isdir(c):
            return c
    return None


def read_active_profile(db_path: str, node: str = "node") -> Optional[str]:
    """Return the id of the currently active Vortex profile."""
    data = read_prefix(db_path, "settings###profiles###activeProfileId", node=node)
    val = data.get("settings###profiles###activeProfileId")
    return val if isinstance(val, str) else None


def read_collection_identity(db_path: str, game: str = "skyrimse",
                             node: str = "node") -> Optional[Tuple[int, str]]:
    """Find an installed collection's ``(collectionId, slug)`` from existing state.

    Works when a prior revision of the collection is already known to Vortex
    (the common "update" case). Returns None if no collection is present.
    """
    data = read_prefix(db_path, f"persistent###mods###{game}###", node=node)
    cid: Optional[int] = None
    slug: Optional[str] = None
    for k, v in data.items():
        if k.endswith("###attributes###collectionId") and isinstance(v, int):
            cid = v
        elif k.endswith("###attributes###collectionSlug") and isinstance(v, str):
            slug = v
    return (cid, slug) if (cid is not None and slug) else None
