"""Local-state / integrity panel.

A self-contained widget over the SQLite ledger (:mod:`utils.local_state`): build
the checksum baseline, verify staging against it (fast size+mtime, or deep md5),
and repair -- delete the folders of broken/missing mods so the next install
rebuilds them from their source download. Decoupled from the install tab via a
``paths_provider`` callable returning ``(staging, downloads, collection)``.
"""

from __future__ import annotations

import os
import shutil
from typing import Callable, Dict, Optional, Tuple

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QPlainTextEdit,
    QMessageBox,
)

from utils import local_state, state_reconcile
from utils.cancellation import CancellationToken


class _LedgerWorker(QThread):
    """Runs a blocking ledger op (reconcile/scan) off the UI thread.

    The op callable receives ``(progress_emit, should_cancel)`` so long passes
    (baseline hashing, deep verify) can bail promptly when the user cancels or
    closes the app.
    """
    progress = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[[Callable[[str], None], Callable[[], bool]], object]):
        super().__init__()
        self._fn = fn
        self._token = CancellationToken()

    def cancel(self):
        self._token.cancel()

    def run(self):
        try:
            self.done.emit(self._fn(self.progress.emit, lambda: self._token.cancelled))
        except Exception as e:           # surface, don't crash the UI thread
            self.failed.emit(str(e))


class LedgerPanel(QGroupBox):
    """Build / verify / repair the local checksum ledger."""

    def __init__(self, paths_provider: Callable[[], Tuple[str, str, str]], parent=None):
        super().__init__("Local State • Integrity", parent)
        self._paths = paths_provider
        self._worker: Optional[_LedgerWorker] = None
        self._last_scan: Optional[Dict] = None
        self._build_ui()
        self.refresh_status()

    # -- UI ------------------------------------------------------------------ #
    def _build_ui(self):
        layout = QVBoxLayout(self)
        self.status = QLabel("Ledger: —")
        layout.addWidget(self.status)

        row = QHBoxLayout()
        self.btn_baseline = QPushButton("Build / Refresh Baseline")
        self.btn_baseline.setToolTip("Record every staged file's size + md5 (the integrity baseline)")
        self.btn_verify = QPushButton("Verify (fast)")
        self.btn_verify.setToolTip("Compare staging to the ledger by size + mtime (no hashing)")
        self.btn_deep = QPushButton("Deep Verify (md5)")
        self.btn_deep.setToolTip("Re-hash every file and compare md5 (thorough, slow)")
        self.btn_repair = QPushButton("Repair Broken…")
        self.btn_repair.setToolTip("Delete folders of missing/changed mods so the next install rebuilds them")
        self.btn_repair.setEnabled(False)
        for b in (self.btn_baseline, self.btn_verify, self.btn_deep, self.btn_repair):
            row.addWidget(b)
        layout.addLayout(row)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumBlockCount(2000)
        self.output.setPlaceholderText("Integrity results appear here…")
        layout.addWidget(self.output)

        self.btn_baseline.clicked.connect(self._build_baseline)
        self.btn_verify.clicked.connect(lambda: self._scan(deep=False))
        self.btn_deep.clicked.connect(lambda: self._scan(deep=True))
        self.btn_repair.clicked.connect(self._repair)

    def _log(self, text: str):
        self.output.appendPlainText(text)

    def _busy(self, on: bool):
        for b in (self.btn_baseline, self.btn_verify, self.btn_deep):
            b.setEnabled(not on)
        if on:
            self.btn_repair.setEnabled(False)

    def _db_path(self) -> Optional[str]:
        staging, _, _ = self._paths()
        if not staging:
            return None
        return local_state.db_path_for(staging)

    # -- status -------------------------------------------------------------- #
    def refresh_status(self):
        db = self._db_path()
        if not db or not os.path.exists(db):
            self.status.setText("Ledger: not built yet — click “Build / Refresh Baseline”")
            return
        try:
            st = local_state.get_ledger(db)
            with st._connect() as c:
                mods = c.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
                files = c.execute("SELECT COUNT(*) FROM mod_files").fetchone()[0]
                verified = c.execute("SELECT COUNT(*) FROM mods WHERE verified=1").fetchone()[0]
                hashed = c.execute("SELECT COUNT(*) FROM mod_files WHERE md5 IS NOT NULL").fetchone()[0]
            self.status.setText(
                f"Ledger: {mods:,} mods ({verified:,} verified) • "
                f"{files:,} files ({hashed:,} hashed)")
        except Exception as e:
            self.status.setText(f"Ledger: error reading ({e})")

    # -- actions ------------------------------------------------------------- #
    def _start(self, fn: Callable, on_done: Callable[[object], None]):
        if self._worker and self._worker.isRunning():
            return
        self._busy(True)
        self._worker = _LedgerWorker(fn)
        self._worker.progress.connect(self._log)
        self._worker.failed.connect(self._on_failed)

        def _wrap(result):
            self._busy(False)
            on_done(result)
            self.refresh_status()
        self._worker.done.connect(_wrap)
        self._worker.start()

    def _on_failed(self, msg: str):
        self._busy(False)
        self._log(f"ERROR: {msg}")
        QMessageBox.warning(self, "Ledger error", msg)

    def _build_baseline(self):
        staging, downloads, collection = self._paths()
        if not (staging and downloads and collection):
            QMessageBox.information(self, "Paths needed",
                                    "Set the staging, downloads, and collection paths first.")
            return
        self.output.clear()
        self._log("Building baseline (identity + md5 of every staged file). This can take a while…")
        self._start(
            lambda log, cancel: state_reconcile.reconcile(staging, downloads, collection,
                                                          do_hash=True, log=log,
                                                          should_cancel=cancel),
            lambda res: self._log(
                f"Baseline {'cancelled' if res.get('cancelled') else 'done'}: "
                f"{res.get('mods', 0)} mods, {res.get('files', 0):,} files."))

    def _scan(self, deep: bool):
        staging, _, _ = self._paths()
        db = self._db_path()
        if not db or not os.path.exists(db):
            QMessageBox.information(self, "No baseline",
                                    "Build the baseline first.")
            return
        self.output.clear()
        kind = "deep (md5)" if deep else "fast (size+mtime)"
        self._log(f"Verifying staging — {kind} scan…")

        def work(log, cancel):
            st = local_state.get_ledger(db)
            return st.deep_scan(staging, should_cancel=cancel) if deep \
                else st.fast_scan(staging, should_cancel=cancel)
        self._start(work, lambda res: self._show_scan(res))

    def _show_scan(self, res: Dict):
        self._last_scan = res
        if res.get("cancelled"):
            self._log("Scan cancelled — partial results below.")
        miss, chg = res.get("missing", []), res.get("changed", [])
        self._log(f"Scanned {res.get('files', 0):,} files in {res.get('mods', 0):,} mods.")
        self._log(f"  missing: {len(miss)}   changed: {len(chg)}")
        for i in (miss + chg)[:50]:
            self._log(f"    {i.get('reason', 'missing'):7s} {i['folder']}/{i['rel_path']}")
        broken = len({i["folder"] for i in miss} | {i["folder"] for i in chg})
        self.btn_repair.setEnabled(broken > 0)
        if broken:
            self._log(f"\n{broken} mod(s) need repair — click “Repair Broken…”")
        else:
            self._log("\nAll good — staging matches the ledger.")

    def _repair(self):
        if not self._last_scan:
            return
        staging, _, _ = self._paths()
        st = local_state.get_ledger(self._db_path())
        affected = st.affected_mods(self._last_scan)
        if not affected:
            return
        lines = "\n".join(f"  {f}  (from {(m or {}).get('dl_local_path') or '?'})"
                          for f, m in list(affected.items())[:30])
        more = "" if len(affected) <= 30 else f"\n  …and {len(affected)-30} more"
        if QMessageBox.question(
                self, "Repair broken mods",
                f"Delete the staging folders of {len(affected)} broken mod(s) so the next "
                f"install rebuilds them from their source archive?\n\n{lines}{more}",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        removed = 0
        for folder in affected:
            try:
                shutil.rmtree(os.path.join(staging, folder), ignore_errors=True)
                removed += 1
            except OSError as e:
                self._log(f"  could not remove {folder}: {e}")
        self._log(f"\nRemoved {removed} folder(s). Re-run the install to rebuild them.")
        self.btn_repair.setEnabled(False)
        self._last_scan = None
        self.refresh_status()
