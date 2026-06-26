"""One-click pipeline: Install -> Baseline -> Link -> Deploy/Finalize.

A single worker thread that runs the whole flow in order, reusing the exact code
paths the individual tabs use (the parallel installer, ``state_reconcile``,
``vortex_sync.sync_collection``, ``vortex_deploy.finalize_collection``). Steps
that need Vortex closed (Link, Deploy) wait for it to be released rather than
failing. Any step's failure stops the run and reports which phase broke.
"""

from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QThread, Signal


class PipelineStop(Exception):
    """A phase decided it can't continue (carries a user-facing reason)."""


class PipelineWorker(QThread):
    phase = Signal(str, int, int)      # phase label, index (1-based), total
    progress = Signal(int, int, str)   # current, total, detail
    log = Signal(str, str)             # level, message
    waiting_vortex = Signal(bool)      # True while blocked on Vortex being open
    failed = Signal(str, str)          # phase, message
    finished_ok = Signal(str)          # summary

    def __init__(self, paths: Dict[str, str], *, workers: int = 16,
                 temp_root: str = "", force: bool = False,
                 do_install: bool = True, do_baseline: bool = False,
                 do_link: bool = True, do_deploy: bool = True, parent=None):
        super().__init__(parent)
        self.p = paths
        self.workers = workers
        self.temp_root = temp_root
        self.force = force
        self.do_install = do_install
        self.do_baseline = do_baseline
        self.do_link, self.do_deploy = do_link, do_deploy
        self._cancel = False
        self._summary: List[str] = []

    def cancel(self):
        self._cancel = True

    # -- orchestration ------------------------------------------------------- #
    def run(self):
        steps: List[Tuple[str, callable]] = []
        if self.do_install:
            steps.append(("Install mods", self._install))
        if self.do_baseline:           # optional: the slow full-md5 integrity pass
            steps.append(("Build integrity baseline", self._baseline))
        if self.do_link:
            steps.append(("Link to Vortex", self._link))
        if self.do_deploy:
            steps.append(("Deploy + finalize", self._deploy))

        total = len(steps)
        for i, (label, fn) in enumerate(steps, 1):
            if self._cancel:
                self.failed.emit(label, "Cancelled")
                return
            self.phase.emit(label, i, total)
            self.log.emit("INFO", f"=== Phase {i}/{total}: {label} ===")
            try:
                fn()
            except PipelineStop as e:
                self.failed.emit(label, str(e))
                return
            except Exception as e:                      # unexpected -> stop, report
                self.failed.emit(label, f"{type(e).__name__}: {e}")
                return
        self.finished_ok.emit("\n".join(self._summary) or "Done.")

    # -- helpers ------------------------------------------------------------- #
    def _collection(self) -> dict:
        with open(self.p["collection"], "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _wait_for_vortex_closed(self, db: str):
        """Block (politely) until Vortex releases state.v2, so Link/Deploy can
        write. Emits ``waiting_vortex`` so the UI can prompt the user."""
        from utils import vortex_db
        if vortex_db.probe(db):
            return
        self.waiting_vortex.emit(True)
        self.log.emit("WARNING", "Vortex is open — waiting for you to close it…")
        try:
            while not self._cancel and not vortex_db.probe(db):
                time.sleep(2.0)
        finally:
            self.waiting_vortex.emit(False)
        if self._cancel:
            raise PipelineStop("Cancelled while waiting for Vortex to close")
        self.log.emit("INFO", "Vortex closed — continuing.")

    def _db_path(self) -> str:
        from utils import vortex_db
        db = self.p.get("db") or vortex_db.find_state_db()
        if not db or not os.path.exists(db):
            raise PipelineStop("Could not find Vortex's state.v2. Open Vortex once "
                               "so it exists, then re-run.")
        return db

    # -- phases -------------------------------------------------------------- #
    def _install(self):
        from utils.fomod_installer import create_parallel_fomod_installer, InstallResult
        from utils.unified_logging import create_operation_logger
        from utils import local_state
        collection = self._collection()
        logger = create_operation_logger("install", "skyrimse")
        handler = None
        try:
            handler = local_state.attach_operation_logging(logger, self.p["staging"], "install")
        except Exception:
            pass
        try:
            with create_parallel_fomod_installer(
                    self.p["staging"], None, self.workers,
                    {"installation_timeout_seconds": 600},
                    self.temp_root or None) as inst:
                inst.set_progress_callback(
                    lambda c, t, n: self.progress.emit(c, t, n))
                results = inst.install_collection_parallel(collection, self.p["downloads"])
            ok = sum(1 for r in results if r.status == InstallResult.SUCCESS)
            fail = sum(1 for r in results if r.status == InstallResult.FAILED)
            skip = sum(1 for r in results if r.status == InstallResult.SKIPPED)
            self.log.emit("INFO", f"Install: {ok} installed, {fail} failed, {skip} skipped")
            self._summary.append(f"Install: {ok} installed, {fail} failed, {skip} skipped")
            if fail:
                self.log.emit("WARNING", f"{fail} mod(s) failed — see the install log/ledger")
        finally:
            if handler is not None:
                logger.removeHandler(handler)

    def _baseline(self):
        from utils import state_reconcile
        self.progress.emit(0, 0, "hashing staged files…")
        res = state_reconcile.reconcile(
            self.p["staging"], self.p["downloads"], self.p["collection"],
            do_hash=True, workers=self.workers,
            log=lambda m: self.log.emit("INFO", m))
        self._summary.append(f"Baseline: {res.get('mods', 0)} mods, "
                             f"{res.get('files', 0):,} files hashed")

    def _link(self):
        from utils import vortex_sync
        from utils.vortex_db import VortexBusyError
        db = self._db_path()
        self._wait_for_vortex_closed(db)
        self.progress.emit(0, 0, "projecting the ledger into Vortex…")
        try:
            res = vortex_sync.sync_collection(
                self.p["collection"], self.p["downloads"], self.p["staging"],
                apply=True, force=self.force)
        except VortexBusyError as e:
            raise PipelineStop(f"Vortex is busy: {e}")
        if not res.applied:
            raise PipelineStop(res.message + "  (enable “Force” to override)")
        self.log.emit("INFO", f"Linked {res.plan.mod_count} mods "
                              f"({res.keys_written:,} DB keys).")
        self._summary.append(f"Link: {res.plan.mod_count} mods registered")

    def _deploy(self):
        from utils import vortex_deploy as dep
        from utils.vortex_db import VortexBusyError
        db = self._db_path()
        self._wait_for_vortex_closed(db)
        collection = self._collection()
        game_data = self.p.get("game_data")
        if not game_data or not os.path.isdir(game_data):
            raise PipelineStop("Game Data folder not set/found — set it on the Deploy tab.")
        self.progress.emit(0, 0, "hard-linking into the game folder…")
        try:
            res = dep.finalize_collection(
                db, collection, self.p["staging"], game_data,
                self.p.get("localappdata", ""),
                deployment_time_ms=int(time.time() * 1000), workers=self.workers,
                progress=lambda c, t, n: self.progress.emit(c, t, n))
        except VortexBusyError as e:
            raise PipelineStop(f"Vortex is busy: {e}")
        self.log.emit("INFO", f"Deployed {res.deploy.files:,} files; "
                              f"sorted {res.active_plugins:,} plugins.")
        self._summary.append(f"Deploy: {res.deploy.files:,} files, "
                             f"{res.active_plugins:,} plugins active")
