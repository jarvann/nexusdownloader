"""Local authoritative state ledger (SQLite).

The whole reason this exists: Vortex never *guesses* a mod's identity -- it
records ``modId``/``fileId``/``fileMD5``/``fileSize`` on a download record at the
moment it downloads (it initiated the nxm URL, so it knows them), and the
resulting mod points back at that download. We were throwing that knowledge away
and re-deriving identity from disk filenames (the ``-<modId>-`` regex, name
globs), which is the root of the wrong-archive / duplicate-mods / "Never
Installed" bugs. This module is our own version of that authoritative state: we
write the truth when we know it (download time, install time) and everything
downstream (Link/Deploy/skip-check/integrity) reads it instead of guessing.

Design:
- One SQLite DB per game, stored in the app data dir (``%APPDATA%/NexusDownloader``
  / ``~/.local/share/nexusdownloader``), NOT inside Vortex's staging root -- so
  Vortex's mod scan never mistakes it for a mod. WAL mode so many readers + one
  writer work across the parallel installer AND across processes (GUI, reconcile,
  CLI).
- All WRITES funnel through a single background writer thread (one queue), which
  both satisfies SQLite's single-writer rule under the 48-thread installer and
  lets us batch inserts. READS use short-lived per-call connections (WAL allows
  concurrent readers).
- Logging is just another table fed through the same writer, so the verbose
  install log stops hammering the filesystem and lives in one queryable place.

Schema (normalised; identity lives once, on ``downloads``):
  meta(key, value)
  collections(id, slug, name, revision_id, revision_number, added_at)
  downloads(id, local_path, mod_id, file_id, md5, file_size, received,
            logical_file_name, collection_id, state, downloaded_at)
  mods(folder, download_id, variant, installer_choices, installed_as_dependency,
       enabled, file_count, install_time, verified, state)
  mod_files(id, folder, name, rel_path, size, md5, mtime, created)
  plugins(name, mod_folder, flag, is_master, masters, could_be_light, enabled,
          load_index)
  mod_rules(source_folder, type, ref_folder, ref_raw)
  logs(id, ts, level, operation, mod_folder, thread, message)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 4
DB_FILENAME = "state.db"
# App-owned data dir name (the ledger must NOT live inside Vortex's staging root,
# or Vortex's folder scan treats it as a mod and our own reconcile walks it).
APP_DIRNAME = "NexusDownloader"

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS collections (
    id              INTEGER PRIMARY KEY,
    slug            TEXT,
    name            TEXT,
    game            TEXT,
    revision_id     INTEGER,
    revision_number INTEGER,
    added_at        INTEGER
);
CREATE TABLE IF NOT EXISTS collection_options (
    collection_key TEXT,             -- stable per-collection key (name/slug)
    mod_key        TEXT,             -- stable per-mod key (collection tag, or modId-fileId)
    selected       INTEGER,          -- 1 = user opted this optional mod in
    updated_at     INTEGER,
    PRIMARY KEY (collection_key, mod_key)
);
CREATE TABLE IF NOT EXISTS downloads (
    id                TEXT PRIMARY KEY,
    local_path        TEXT UNIQUE,
    game              TEXT,
    source            TEXT,
    mod_id            INTEGER,
    file_id           INTEGER,
    md5               TEXT,
    file_size         INTEGER,
    received          INTEGER,
    logical_file_name TEXT,
    collection_id     INTEGER,
    state             TEXT,
    downloaded_at     INTEGER,
    endorsed_at       INTEGER          -- NULL until endorsed; unix-ms when endorsed
);
CREATE INDEX IF NOT EXISTS ix_dl_ids  ON downloads(mod_id, file_id);
CREATE INDEX IF NOT EXISTS ix_dl_md5  ON downloads(md5);
CREATE INDEX IF NOT EXISTS ix_dl_size ON downloads(file_size);
CREATE INDEX IF NOT EXISTS ix_dl_game ON downloads(game);

CREATE TABLE IF NOT EXISTS mods (
    folder                  TEXT PRIMARY KEY,
    download_id             TEXT,
    variant                 TEXT,
    installer_choices       TEXT,
    installed_as_dependency INTEGER,
    enabled                 INTEGER,
    file_count              INTEGER,
    install_time            INTEGER,
    verified                INTEGER,
    state                   TEXT
);
CREATE INDEX IF NOT EXISTS ix_mods_dl ON mods(download_id);

CREATE TABLE IF NOT EXISTS mod_files (
    id       INTEGER PRIMARY KEY,
    folder   TEXT,
    name     TEXT,
    rel_path TEXT,
    size     INTEGER,
    md5      TEXT,
    mtime    INTEGER,
    created  INTEGER,
    UNIQUE(folder, rel_path)
);
CREATE INDEX IF NOT EXISTS ix_mf_folder ON mod_files(folder);
CREATE INDEX IF NOT EXISTS ix_mf_name   ON mod_files(name);
CREATE INDEX IF NOT EXISTS ix_mf_md5    ON mod_files(md5);

-- Files placed directly in the GAME ROOT (next to SkyrimSE.exe) rather than Data
-- -- SKSE loader, ENB dlls, etc. Tracked so they can be reported/cleaned; they
-- live outside Vortex's deployment, so Vortex won't manage or purge them.
CREATE TABLE IF NOT EXISTS root_files (
    id        INTEGER PRIMARY KEY,
    folder    TEXT,
    rel_dest  TEXT,
    dest_path TEXT,
    game_root TEXT,
    created   INTEGER,
    UNIQUE(dest_path)
);
CREATE INDEX IF NOT EXISTS ix_rf_folder ON root_files(folder);

CREATE TABLE IF NOT EXISTS plugins (
    name          TEXT PRIMARY KEY,
    mod_folder    TEXT,
    flag          TEXT,
    is_master     INTEGER,
    masters       TEXT,
    could_be_light INTEGER,
    enabled       INTEGER,
    load_index    INTEGER
);
CREATE INDEX IF NOT EXISTS ix_pl_folder ON plugins(mod_folder);

CREATE TABLE IF NOT EXISTS mod_rules (
    source_folder TEXT,
    type          TEXT,
    ref_folder    TEXT,
    ref_raw       TEXT,
    PRIMARY KEY (source_folder, type, ref_folder)
);

CREATE TABLE IF NOT EXISTS logs (
    id         INTEGER PRIMARY KEY,
    ts         INTEGER,
    level      TEXT,
    operation  TEXT,
    mod_folder TEXT,
    thread     TEXT,
    message    TEXT
);
CREATE INDEX IF NOT EXISTS ix_log_ts  ON logs(ts);
CREATE INDEX IF NOT EXISTS ix_log_op  ON logs(operation);
CREATE INDEX IF NOT EXISTS ix_log_mod ON logs(mod_folder);
"""


# Columns added after v1. ``CREATE TABLE IF NOT EXISTS`` leaves an existing table
# untouched, so new columns on old DBs are added here idempotently.
_ADDED_COLUMNS = {
    "downloads": {"game": "TEXT", "source": "TEXT", "endorsed_at": "INTEGER"},
    "collections": {"game": "TEXT"},
}


def _migrate(conn: sqlite3.Connection) -> None:
    """Add any post-v1 columns missing from an *existing* DB (idempotent).

    Runs BEFORE the DDL so indexes on new columns can be created. A table that
    doesn't exist yet (fresh DB) has no columns to report -- skip it and let the
    DDL create it complete.
    """
    for table, cols in _ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not have:                      # table not created yet -> DDL handles it
            continue
        for col, decl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


# --------------------------------------------------------------------------- #
# Path + hashing helpers
# --------------------------------------------------------------------------- #
def app_data_dir() -> str:
    """Return the app-owned data directory (created if missing).

    The ledger lives here -- NOT inside Vortex's staging root -- so Vortex's mod
    scan never sees it. Honours an explicit ``NEXUSDOWNLOADER_DATA_DIR`` override,
    then the OS convention: ``%APPDATA%\\NexusDownloader`` on Windows,
    ``$XDG_DATA_HOME/nexusdownloader`` (or ``~/.local/share/...``) elsewhere.
    """
    override = os.environ.get("NEXUSDOWNLOADER_DATA_DIR")
    if override:
        base = override
    elif os.name == "nt":
        base = os.path.join(os.environ.get("APPDATA")
                            or os.path.join(os.path.expanduser("~"), "AppData", "Roaming"),
                            APP_DIRNAME)
    else:
        base = os.path.join(os.environ.get("XDG_DATA_HOME")
                            or os.path.join(os.path.expanduser("~"), ".local", "share"),
                            APP_DIRNAME.lower())
    os.makedirs(base, exist_ok=True)
    return base


def db_path_for(staging_dir: str) -> str:
    """Return the ledger DB path for a given staging folder.

    One DB per staging root, stored in the app data dir (never inside staging).
    The filename is derived deterministically from the absolute, case-normalised
    staging path -- so every process (GUI, reconcile, CLI, the parallel installer)
    resolves the same file for the same game, while distinct games/instances stay
    isolated. A readable prefix (the staging basename) plus a short hash keeps the
    name both human-recognisable and collision-free.
    """
    norm = os.path.normcase(os.path.abspath(staging_dir))
    digest = hashlib.md5(norm.encode("utf-8")).hexdigest()[:12]
    prefix = re.sub(r"[^A-Za-z0-9_-]", "_", os.path.basename(norm.rstrip("\\/"))) or "staging"
    ledgers = os.path.join(app_data_dir(), "ledgers")
    os.makedirs(ledgers, exist_ok=True)
    return os.path.join(ledgers, f"{prefix}-{digest}-{DB_FILENAME}")


def hash_file(path: str, _bufsize: int = 1024 * 1024) -> Optional[str]:
    """MD5 of a file, or None if it can't be read. Streams so big files are cheap."""
    h = hashlib.md5()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(_bufsize), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


# --------------------------------------------------------------------------- #
# The ledger
# --------------------------------------------------------------------------- #
class _FlushBarrier:
    """A queue marker that makes the writer commit and signal -- so ``flush()``
    can guarantee durability before a read on another connection."""
    __slots__ = ("event",)

    def __init__(self):
        self.event = threading.Event()


class LocalState:
    """Thread-safe SQLite ledger. Writes go through one background thread (and
    are batched); reads use short-lived connections (WAL → concurrent reads)."""

    # A write job is (sql, params) for execute, or (sql, [rows]) for executemany
    # when params is a list of sequences. ``None`` is the shutdown sentinel.
    def __init__(self, db_path: str, *, busy_timeout_ms: int = 15000):
        self.db_path = db_path
        self._busy = busy_timeout_ms
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        # Initialise schema on the calling thread, synchronously.
        conn = self._connect()
        try:
            _migrate(conn)            # add new columns BEFORE the DDL builds their indexes
            conn.executescript(_DDL)
            cur = conn.execute("SELECT value FROM meta WHERE key='schema_version'")
            if cur.fetchone() is None:
                conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                             (str(SCHEMA_VERSION),))
            else:
                conn.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                             (str(SCHEMA_VERSION),))
            conn.commit()
        finally:
            conn.close()

        self._q: "queue.Queue" = queue.Queue()
        self._closed = False
        self._writer = threading.Thread(target=self._writer_loop, name="nxd-ledger-writer",
                                        daemon=True)
        self._writer.start()

    # -- connection plumbing -------------------------------------------------- #
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=self._busy / 1000.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(f"PRAGMA busy_timeout={self._busy}")
        return conn

    def _writer_loop(self) -> None:
        conn = self._connect()
        pending = 0
        last_commit = time.time()
        try:
            while True:
                try:
                    job = self._q.get(timeout=0.5)
                except queue.Empty:
                    if pending:
                        conn.commit()
                        pending = 0
                        last_commit = time.time()
                    continue
                if job is None:               # shutdown
                    break
                if isinstance(job, _FlushBarrier):   # commit + signal the waiter
                    if pending:
                        conn.commit()
                        pending = 0
                        last_commit = time.time()
                    job.event.set()
                    continue
                sql, params = job
                try:
                    if params and isinstance(params, list) and params and \
                            isinstance(params[0], (list, tuple)):
                        conn.executemany(sql, params)
                        pending += len(params)
                    else:
                        conn.execute(sql, params or ())
                        pending += 1
                except sqlite3.Error as e:
                    # Never let one bad write kill the writer (and never raise on
                    # the install thread that enqueued it).
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
                    _fallback_log(f"ledger write failed: {e} :: {sql[:80]}")
                # Commit on size or time so a crash loses at most ~1s of writes.
                if pending >= 500 or (time.time() - last_commit) > 1.0:
                    conn.commit()
                    pending = 0
                    last_commit = time.time()
            if pending:
                conn.commit()
        finally:
            conn.close()

    def _enqueue(self, sql: str, params) -> None:
        if not self._closed:
            self._q.put((sql, params))

    def flush(self, timeout: float = 30.0) -> None:
        """Block until every queued write has been committed (a real barrier, so a
        subsequent read on another connection sees the data)."""
        if self._closed:
            return
        barrier = _FlushBarrier()
        self._q.put(barrier)
        barrier.event.wait(timeout)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._q.put(None)
        self._writer.join(timeout=30)

    def __enter__(self) -> "LocalState":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- meta ----------------------------------------------------------------- #
    def set_meta(self, key: str, value: Any) -> None:
        self._enqueue("INSERT INTO meta(key,value) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, json.dumps(value) if not isinstance(value, str) else value))

    def get_meta(self, key: str) -> Optional[str]:
        with self._connect() as c:
            r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    # -- collections ---------------------------------------------------------- #
    def upsert_collection(self, cid: int, slug: str, name: str,
                          revision_id: int, revision_number: int,
                          game: str = "") -> None:
        self._enqueue(
            "INSERT INTO collections(id,slug,name,game,revision_id,revision_number,added_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "slug=excluded.slug, name=excluded.name, game=excluded.game, "
            "revision_id=excluded.revision_id, revision_number=excluded.revision_number",
            (cid, slug, name, game, revision_id, revision_number, int(time.time())))

    def set_collection_options(self, collection_key: str,
                               selections: Dict[str, bool]) -> None:
        """Persist the user's optional-mod opt-in choices for a collection.

        ``selections`` maps a stable per-mod key -> selected. Both checked and
        unchecked optionals are stored, so a deliberate de-selection persists too
        (not just additions).
        """
        now = int(time.time())
        rows = [(collection_key, mod_key, 1 if sel else 0, now)
                for mod_key, sel in selections.items()]
        if rows:
            self._enqueue(
                "INSERT INTO collection_options(collection_key,mod_key,selected,updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(collection_key,mod_key) DO UPDATE SET "
                "selected=excluded.selected, updated_at=excluded.updated_at", rows)

    def get_collection_options(self, collection_key: str) -> Dict[str, bool]:
        """Return {mod_key: selected} previously saved for a collection ({} if none)."""
        with self._connect() as c:
            return {r["mod_key"]: bool(r["selected"]) for r in c.execute(
                "SELECT mod_key, selected FROM collection_options WHERE collection_key=?",
                (collection_key,)).fetchall()}

    def link_downloads_to_collection(self, collection_id: int, game: str) -> None:
        """Associate this game's not-yet-linked downloads with a collection.

        Downloads are recorded at download time, before Vortex (and thus the
        ledger) knows the numeric collectionId. Link learns it and calls this to
        back-fill ``collection_id`` on every download for the game that doesn't
        already point at a collection.
        """
        self._enqueue(
            "UPDATE downloads SET collection_id=? WHERE game=? AND collection_id IS NULL",
            (collection_id, game))

    # -- downloads ------------------------------------------------------------ #
    def upsert_download(self, dl_id: str, local_path: str, mod_id: Optional[int],
                        file_id: Optional[int], md5: str, file_size: int,
                        received: int, logical_file_name: str,
                        collection_id: Optional[int], state: str = "finished",
                        downloaded_at: Optional[int] = None,
                        game: Optional[str] = None, source: Optional[str] = None) -> None:
        # local_path is UNIQUE (one file = one download). Make this writer
        # authoritative: release the path from any OTHER row that holds it (a
        # true-up best-guess the live download has now corrected), so claiming it
        # here can't trip the UNIQUE constraint. NULL paths (missing rows) are
        # untouched -- NULL never equals the path value.
        if local_path:
            self._enqueue(
                "UPDATE downloads SET local_path=NULL, state='missing' "
                "WHERE local_path=? AND id<>?", (local_path, dl_id))
        self._enqueue(
            "INSERT INTO downloads(id,local_path,game,source,mod_id,file_id,md5,file_size,"
            "received,logical_file_name,collection_id,state,downloaded_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "local_path=excluded.local_path, game=excluded.game, source=excluded.source, "
            "mod_id=excluded.mod_id, file_id=excluded.file_id, "
            "md5=excluded.md5, file_size=excluded.file_size, received=excluded.received,"
            "logical_file_name=excluded.logical_file_name, "
            # collection_id is back-filled by Link; never clobber a known id with NULL.
            "collection_id=COALESCE(excluded.collection_id, downloads.collection_id),"
            "state=excluded.state, downloaded_at=excluded.downloaded_at",
            (dl_id, local_path, game, source, mod_id, file_id, md5, file_size, received,
             logical_file_name, collection_id, state,
             downloaded_at if downloaded_at is not None else int(time.time())))

    def get_download(self, dl_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as c:
            return _row_to_dict(c.execute("SELECT * FROM downloads WHERE id=?", (dl_id,)).fetchone())

    def get_download_by_ids(self, mod_id: int, file_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as c:
            return _row_to_dict(c.execute(
                "SELECT * FROM downloads WHERE mod_id=? AND file_id=?",
                (mod_id, file_id)).fetchone())

    def get_download_by_md5(self, md5: str) -> Optional[Dict[str, Any]]:
        if not md5:
            return None
        with self._connect() as c:
            return _row_to_dict(c.execute("SELECT * FROM downloads WHERE md5=?", (md5,)).fetchone())

    def get_download_by_path(self, local_path: str) -> Optional[Dict[str, Any]]:
        with self._connect() as c:
            return _row_to_dict(c.execute(
                "SELECT * FROM downloads WHERE local_path=?", (local_path,)).fetchone())

    def mark_endorsed(self, mod_id: int, file_id: int,
                      when_ms: Optional[int] = None) -> None:
        """Record when a download's mod was endorsed (so a later run skips it).
        ``when_ms`` defaults to now; pass None-clearing isn't supported here."""
        self._enqueue(
            "UPDATE downloads SET endorsed_at=? WHERE mod_id=? AND file_id=?",
            (when_ms if when_ms is not None else int(time.time() * 1000), mod_id, file_id))

    def endorsed_pairs(self) -> Dict[Tuple[int, int], int]:
        """{(mod_id, file_id): endorsed_at} for every endorsed download -- used to
        preserve endorsements across a clean mapping rebuild."""
        with self._connect() as c:
            return {(r["mod_id"], r["file_id"]): r["endorsed_at"] for r in c.execute(
                "SELECT mod_id, file_id, endorsed_at FROM downloads "
                "WHERE endorsed_at IS NOT NULL AND mod_id IS NOT NULL AND file_id IS NOT NULL"
            ).fetchall()}

    def clear_mods_and_downloads(self) -> None:
        """Wipe the mod + download mapping so it can be rebuilt deterministically
        from disk. (mod_files/plugins/rules are keyed by folder and refreshed by
        their own passes; collections/logs are untouched.)"""
        self._enqueue("DELETE FROM mods", ())
        self._enqueue("DELETE FROM downloads", ())

    def prune_orphan_downloads(self) -> None:
        """Drop download rows no longer referenced by any mod -- e.g. stale rows
        left behind after a folder was re-pointed to its correct archive."""
        self._enqueue(
            "DELETE FROM downloads WHERE id NOT IN "
            "(SELECT download_id FROM mods WHERE download_id IS NOT NULL)", ())

    def endorsed_ids(self, game: Optional[str] = None) -> set:
        """Set of ``(mod_id, file_id)`` already endorsed -- used to skip them on a
        re-endorse pass. Optionally scoped to a game."""
        sql = ("SELECT mod_id, file_id FROM downloads "
               "WHERE endorsed_at IS NOT NULL AND mod_id IS NOT NULL")
        params: Tuple = ()
        if game:
            sql += " AND game=?"
            params = (game,)
        with self._connect() as c:
            return {(r["mod_id"], r["file_id"]) for r in c.execute(sql, params).fetchall()}

    def download_ids_by_path(self) -> Dict[str, str]:
        """All recorded local_path -> id, so a reconcile keeps a download's id
        stable across runs (avoids a local_path UNIQUE conflict when the id source
        changes, e.g. Vortex closed vs open)."""
        with self._connect() as c:
            return {r["local_path"]: r["id"] for r in
                    c.execute("SELECT id, local_path FROM downloads").fetchall()}

    # -- mods ----------------------------------------------------------------- #
    def upsert_mod(self, folder: str, download_id: Optional[str], variant: str = "",
                   installer_choices: Optional[dict] = None,
                   installed_as_dependency: bool = True, enabled: bool = True,
                   file_count: int = 0, install_time: Optional[int] = None,
                   verified: bool = False, state: str = "installed") -> None:
        self._enqueue(
            "INSERT INTO mods(folder,download_id,variant,installer_choices,"
            "installed_as_dependency,enabled,file_count,install_time,verified,state) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(folder) DO UPDATE SET "
            "download_id=excluded.download_id, variant=excluded.variant,"
            "installer_choices=excluded.installer_choices,"
            "installed_as_dependency=excluded.installed_as_dependency, enabled=excluded.enabled,"
            "file_count=excluded.file_count, install_time=excluded.install_time,"
            "verified=excluded.verified, state=excluded.state",
            (folder, download_id, variant,
             json.dumps(installer_choices) if installer_choices else None,
             1 if installed_as_dependency else 0, 1 if enabled else 0, file_count,
             install_time if install_time is not None else int(time.time()),
             1 if verified else 0, state))

    def set_mod_verified(self, folder: str, verified: bool, file_count: Optional[int] = None) -> None:
        if file_count is None:
            self._enqueue("UPDATE mods SET verified=? WHERE folder=?",
                          (1 if verified else 0, folder))
        else:
            self._enqueue("UPDATE mods SET verified=?, file_count=? WHERE folder=?",
                          (1 if verified else 0, file_count, folder))

    def get_mod(self, folder: str) -> Optional[Dict[str, Any]]:
        with self._connect() as c:
            return _row_to_dict(c.execute("SELECT * FROM mods WHERE folder=?", (folder,)).fetchone())

    def get_mod_by_ids(self, mod_id: int, file_id: int) -> Optional[Dict[str, Any]]:
        """The installed mod for a (modId,fileId) -- joined via its download."""
        with self._connect() as c:
            return _row_to_dict(c.execute(
                "SELECT m.* FROM mods m JOIN downloads d ON m.download_id=d.id "
                "WHERE d.mod_id=? AND d.file_id=?", (mod_id, file_id)).fetchone())

    def all_mods(self) -> List[Dict[str, Any]]:
        with self._connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM mods").fetchall()]

    def mod_with_download(self, folder: str) -> Optional[Dict[str, Any]]:
        """A mod row plus its download's modId/fileId/md5/local_path (for Link)."""
        with self._connect() as c:
            r = c.execute(
                "SELECT m.*, d.mod_id AS dl_mod_id, d.file_id AS dl_file_id, "
                "d.md5 AS dl_md5, d.local_path AS dl_local_path, "
                "d.file_size AS dl_file_size, d.logical_file_name AS dl_logical "
                "FROM mods m LEFT JOIN downloads d ON m.download_id=d.id "
                "WHERE m.folder=?", (folder,)).fetchone()
        return _row_to_dict(r)

    def all_mods_with_download(self) -> List[Dict[str, Any]]:
        with self._connect() as c:
            return [dict(r) for r in c.execute(
                "SELECT m.*, d.mod_id AS dl_mod_id, d.file_id AS dl_file_id, "
                "d.md5 AS dl_md5, d.local_path AS dl_local_path, "
                "d.file_size AS dl_file_size, d.logical_file_name AS dl_logical, "
                "d.downloaded_at AS dl_downloaded_at "
                "FROM mods m LEFT JOIN downloads d ON m.download_id=d.id").fetchall()]

    # -- mod_files ------------------------------------------------------------ #
    def replace_mod_files(self, folder: str,
                          files: Sequence[Tuple[str, str, int, Optional[str], int, Optional[int]]]
                          ) -> None:
        """Replace the file list for a mod. ``files`` = (name, rel_path, size, md5,
        mtime, created)."""
        self._enqueue("DELETE FROM mod_files WHERE folder=?", (folder,))
        if files:
            rows = [(folder, n, rp, sz, md5, mt, cr) for (n, rp, sz, md5, mt, cr) in files]
            self._enqueue(
                "INSERT OR REPLACE INTO mod_files(folder,name,rel_path,size,md5,mtime,created) "
                "VALUES(?,?,?,?,?,?,?)", rows)

    def mod_files(self, folder: str) -> List[Dict[str, Any]]:
        with self._connect() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM mod_files WHERE folder=?", (folder,)).fetchall()]

    def find_files_by_md5(self, md5: str) -> List[Dict[str, Any]]:
        with self._connect() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM mod_files WHERE md5=?", (md5,)).fetchall()]

    def find_files_by_name(self, name: str) -> List[Dict[str, Any]]:
        with self._connect() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM mod_files WHERE name=?", (name.lower(),)).fetchall()]

    # -- root_files ----------------------------------------------------------- #
    def record_root_file(self, folder: str, rel_dest: str, dest_path: str,
                         game_root: str) -> None:
        """Record a file placed in the game root (not Data) by the installer."""
        self._enqueue(
            "INSERT OR REPLACE INTO root_files(folder,rel_dest,dest_path,game_root,created) "
            "VALUES(?,?,?,?,?)",
            (folder, rel_dest, dest_path, game_root, int(time.time())))

    def all_root_files(self) -> List[Dict[str, Any]]:
        with self._connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM root_files").fetchall()]

    # -- plugins -------------------------------------------------------------- #
    def upsert_plugin(self, name: str, mod_folder: str, flag: str, is_master: bool,
                      masters: Optional[list], could_be_light: bool, enabled: bool,
                      load_index: Optional[int] = None) -> None:
        self._enqueue(
            "INSERT INTO plugins(name,mod_folder,flag,is_master,masters,could_be_light,"
            "enabled,load_index) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
            "mod_folder=excluded.mod_folder, flag=excluded.flag, is_master=excluded.is_master,"
            "masters=excluded.masters, could_be_light=excluded.could_be_light,"
            "enabled=excluded.enabled, load_index=excluded.load_index",
            (name.lower(), mod_folder, flag, 1 if is_master else 0,
             json.dumps(masters or []), 1 if could_be_light else 0,
             1 if enabled else 0, load_index))

    def all_plugins(self) -> List[Dict[str, Any]]:
        with self._connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM plugins").fetchall()]

    # -- rules ---------------------------------------------------------------- #
    def replace_mod_rules(self, source_folder: str,
                          rules: Sequence[Tuple[str, Optional[str], Optional[dict]]]) -> None:
        """Replace the rules for a source mod. ``rules`` = (type, ref_folder, ref_raw)."""
        self._enqueue("DELETE FROM mod_rules WHERE source_folder=?", (source_folder,))
        if rules:
            rows = [(source_folder, t, rf or "", json.dumps(rr) if rr else None)
                    for (t, rf, rr) in rules]
            self._enqueue(
                "INSERT OR REPLACE INTO mod_rules(source_folder,type,ref_folder,ref_raw) "
                "VALUES(?,?,?,?)", rows)

    def mod_rules(self, source_folder: str) -> List[Dict[str, Any]]:
        with self._connect() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM mod_rules WHERE source_folder=?", (source_folder,)).fetchall()]

    # -- logs ----------------------------------------------------------------- #
    def log(self, level: str, message: str, *, operation: str = "",
            mod_folder: str = "", thread: str = "", ts: Optional[int] = None) -> None:
        self._enqueue(
            "INSERT INTO logs(ts,level,operation,mod_folder,thread,message) VALUES(?,?,?,?,?,?)",
            (ts if ts is not None else int(time.time() * 1000), level, operation,
             mod_folder, thread, message))

    def query_logs(self, *, operation: Optional[str] = None, level: Optional[str] = None,
                   mod_folder: Optional[str] = None, since_ms: Optional[int] = None,
                   limit: int = 1000) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if operation:
            clauses.append("operation=?"); params.append(operation)
        if level:
            clauses.append("level=?"); params.append(level)
        if mod_folder:
            clauses.append("mod_folder=?"); params.append(mod_folder)
        if since_ms is not None:
            clauses.append("ts>=?"); params.append(since_ms)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._connect() as c:
            return [dict(r) for r in c.execute(
                f"SELECT * FROM logs {where} ORDER BY ts DESC LIMIT ?", params).fetchall()]

    # -- integrity scan ------------------------------------------------------- #
    def fast_scan(self, staging_dir: str, should_cancel=None) -> Dict[str, List[Dict[str, Any]]]:
        """Cheap integrity check: compare recorded size+mtime to disk. No hashing.
        Returns {'missing':[...], 'changed':[...], 'mods':int, 'files':int,
        'cancelled':bool}."""
        return self._scan(staging_dir, deep=False, should_cancel=should_cancel)

    def deep_scan(self, staging_dir: str, should_cancel=None) -> Dict[str, List[Dict[str, Any]]]:
        """Thorough check: re-hash every file and compare md5. Expensive."""
        return self._scan(staging_dir, deep=True, should_cancel=should_cancel)

    def _scan(self, staging_dir: str, *, deep: bool, should_cancel=None) -> Dict[str, Any]:
        missing: List[Dict[str, Any]] = []
        changed: List[Dict[str, Any]] = []
        cancelled = False
        with self._connect() as c:
            mods = [r["folder"] for r in c.execute("SELECT folder FROM mods").fetchall()]
            nfiles = 0
            for folder in mods:
                if should_cancel is not None and should_cancel():
                    cancelled = True
                    break
                for f in c.execute("SELECT * FROM mod_files WHERE folder=?", (folder,)).fetchall():
                    nfiles += 1
                    full = os.path.join(staging_dir, folder, f["rel_path"].replace("/", os.sep))
                    try:
                        st = os.stat(full)
                    except OSError:
                        missing.append({"folder": folder, "rel_path": f["rel_path"]})
                        continue
                    if f["size"] is not None and st.st_size != f["size"]:
                        changed.append({"folder": folder, "rel_path": f["rel_path"],
                                        "reason": "size"})
                        continue
                    if deep and f["md5"]:
                        if hash_file(full) != f["md5"]:
                            changed.append({"folder": folder, "rel_path": f["rel_path"],
                                            "reason": "md5"})
                    elif not deep and f["mtime"] is not None and \
                            int(st.st_mtime) != f["mtime"]:
                        changed.append({"folder": folder, "rel_path": f["rel_path"],
                                        "reason": "mtime"})
        return {"missing": missing, "changed": changed, "mods": len(mods),
                "files": nfiles, "cancelled": cancelled}

    def affected_mods(self, scan: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """Map a scan result to the mods (and their source download) that need a
        re-install -- the 'go back to the original mod to retry' lookup."""
        folders = {i["folder"] for i in scan.get("missing", [])} | \
                  {i["folder"] for i in scan.get("changed", [])}
        out: Dict[str, Dict[str, Any]] = {}
        for folder in folders:
            out[folder] = self.mod_with_download(folder)
        return out


# --------------------------------------------------------------------------- #
# Process-wide shared ledgers (one writer per DB, reused by logging + data)
# --------------------------------------------------------------------------- #
_LEDGERS: Dict[str, "LocalState"] = {}
_LEDGERS_LOCK = threading.Lock()


def get_ledger(db_path: str) -> "LocalState":
    """Return the process-wide :class:`LocalState` for ``db_path``, opening it
    once. Sharing one instance (hence one writer thread) across logging and data
    writes avoids cross-connection write contention under WAL."""
    with _LEDGERS_LOCK:
        st = _LEDGERS.get(db_path)
        if st is None or st._closed:
            st = LocalState(db_path)
            _LEDGERS[db_path] = st
        return st


def close_ledgers() -> None:
    """Flush + close all shared ledgers (registered with atexit)."""
    with _LEDGERS_LOCK:
        for st in _LEDGERS.values():
            try:
                st.close()
            except Exception:
                pass
        _LEDGERS.clear()


import atexit as _atexit          # noqa: E402  (kept local to the registry)
_atexit.register(close_ledgers)


# --------------------------------------------------------------------------- #
# Logging handler -> ledger
# --------------------------------------------------------------------------- #
def _fallback_log(msg: str) -> None:
    try:
        logging.getLogger("nxd.ledger").warning(msg)
    except Exception:
        pass


class SQLiteLogHandler(logging.Handler):
    """A logging.Handler that writes records into the ledger's ``logs`` table via
    the same batched background writer (so verbose install logging is one DB
    place, not thousands of file writes). ``operation``/``mod_folder`` can be
    attached per-record via ``extra={'operation':..., 'mod_folder':...}``."""

    def __init__(self, state: "LocalState", operation: str = ""):
        super().__init__()
        self.state = state
        self.operation = operation

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.state.log(
                record.levelname, self.format(record),
                operation=getattr(record, "operation", self.operation) or "",
                mod_folder=getattr(record, "mod_folder", "") or "",
                thread=record.threadName or "",
                ts=int(record.created * 1000))
        except Exception:
            self.handleError(record)


def attach_operation_logging(logger: logging.Logger, staging_dir: str,
                             operation: str) -> SQLiteLogHandler:
    """Route ``logger`` into the staging ledger's ``logs`` table for ``operation``
    (install/link/deploy/...). Returns the handler so the caller can
    ``logger.removeHandler(...)`` when the operation ends. Logging shares the
    process-wide ledger writer, so it's cheap and contention-free."""
    handler = SQLiteLogHandler(get_ledger(db_path_for(staging_dir)), operation)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return handler


# --------------------------------------------------------------------------- #
# CLI: inspect the ledger
# --------------------------------------------------------------------------- #
def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Inspect the NexusDownloader ledger")
    ap.add_argument("--db", help="path to state.db (or pass --staging)")
    ap.add_argument("--staging", help="staging dir (derives the app-data ledger path)")
    ap.add_argument("--dump", action="store_true", help="summary counts")
    ap.add_argument("--logs", action="store_true", help="recent log rows")
    ap.add_argument("--operation"); ap.add_argument("--level")
    ap.add_argument("--scan", action="store_true", help="fast integrity scan")
    ap.add_argument("--deep-scan", action="store_true", help="hash-verify every file")
    args = ap.parse_args(argv)

    db = args.db or (db_path_for(args.staging) if args.staging else None)
    if not db or not os.path.exists(db):
        print(f"!! no ledger at {db}"); return 1
    st = LocalState(db)
    try:
        if args.dump or not (args.logs or args.scan or args.deep_scan):
            with st._connect() as c:
                for tbl in ("collections", "downloads", "mods", "mod_files", "plugins",
                            "mod_rules", "logs"):
                    n = c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                    print(f"  {tbl:12s}: {n:,}")
                v = c.execute("SELECT COUNT(*) FROM mods WHERE verified=1").fetchone()[0]
                print(f"  (mods verified: {v:,})")
        if args.logs:
            for r in reversed(st.query_logs(operation=args.operation, level=args.level, limit=200)):
                print(f"  {r['ts']} [{r['level']}] {r['operation']} {r['mod_folder']} :: {r['message'][:120]}")
        if args.scan or args.deep_scan:
            staging = args.staging or os.path.dirname(os.path.dirname(db))
            res = st.deep_scan(staging) if args.deep_scan else st.fast_scan(staging)
            print(f"  scanned {res['files']:,} files in {res['mods']:,} mods")
            print(f"  missing: {len(res['missing'])}  changed: {len(res['changed'])}")
            for i in (res["missing"] + res["changed"])[:30]:
                print(f"    {i.get('reason','missing'):7s} {i['folder']}/{i['rel_path']}")
    finally:
        st.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())
