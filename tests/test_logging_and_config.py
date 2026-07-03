"""Regression tests for the log-rotation crash fix and the Vortex state.v2 dir skip."""
import logging
import logging.handlers
import time

from utils.unified_logging import CustomRotatingFileHandler
from utils.vortex_config import get_vortex_config_reader


def test_rollover_survives_locked_file(tmp_path, monkeypatch):
    """A locked log file (WinError 32) must not crash -- rollover is skipped/paused."""
    handler = CustomRotatingFileHandler(str(tmp_path / "t.log"), maxBytes=10,
                                        backupCount=2, delay=False)

    def boom(self):
        raise PermissionError("[WinError 32] used by another process")
    monkeypatch.setattr(logging.handlers.RotatingFileHandler, "doRollover", boom)

    handler.doRollover()                       # must NOT raise
    assert handler._rollover_paused_until > time.time()
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "x" * 100, None, None)
    assert handler.shouldRollover(rec) is False   # suppressed during the pause
    handler.close()


def test_read_state_file_skips_leveldb_directory(tmp_path):
    """state.v2 as a LevelDB directory returns None quietly, not a permission error."""
    (tmp_path / "state.v2").mkdir()
    reader = get_vortex_config_reader()
    assert reader.read_state_file(tmp_path) is None


def test_read_state_file_missing(tmp_path):
    reader = get_vortex_config_reader()
    assert reader.read_state_file(tmp_path) is None   # no state.v2 at all
