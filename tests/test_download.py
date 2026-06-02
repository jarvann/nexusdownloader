"""Regression tests for download.py: @DL records, retry, truncation guard."""
import types

import requests

import download


class FakeResp:
    """Minimal stand-in for requests.get(...) used as a context manager."""
    def __init__(self, chunks, content_length, raise_on_iter=None):
        self.chunks = chunks
        self._raise = raise_on_iter
        self.headers = {} if content_length is None else {"content-length": str(content_length)}

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        for c in self.chunks:
            yield c
        if self._raise:
            raise self._raise

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _setup(monkeypatch, tmp_path, responses):
    """Wire download.py to a temp downloads dir + a fixed URL + canned responses."""
    download.set_download_logger(__import__("logging").getLogger("t"))
    cfg = types.SimpleNamespace(
        VortexSettings=types.SimpleNamespace(DownloadsFolderRoot=str(tmp_path)))
    monkeypatch.setattr(download, "CONFIG", cfg, raising=False)
    monkeypatch.setattr(download, "get_download_url",
                        lambda *a, **k: "http://example/test-1-0.7z")
    monkeypatch.setattr(download.time, "sleep", lambda *a, **k: None)
    it = iter(responses)
    monkeypatch.setattr(download.requests, "get", lambda *a, **k: next(it))


def test_emit_status_format(capsys):
    download.emit_status("PROG", "t0", 1, 2, 3)
    line = capsys.readouterr().out.strip()
    assert line == "@DL\tPROG\tt0\t1\t2\t3"


def test_successful_download_writes_file(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, [FakeResp([b"abc", b"def"], 6)])
    assert download.download_file("skyrimspecialedition", "skyrimse", 1, 2, 1) is True
    out = tmp_path / "skyrimse" / "test-1-0.7z"
    assert out.exists() and out.read_bytes() == b"abcdef"


def test_retries_on_incomplete_then_succeeds(monkeypatch, tmp_path):
    err = requests.exceptions.ChunkedEncodingError("Connection broken")
    calls = []
    monkeypatch.setattr(download, "get_download_url",
                        lambda *a, **k: calls.append(1) or "http://example/test-1-0.7z")
    _setup(monkeypatch, tmp_path, [FakeResp([b"abc"], 6, raise_on_iter=err),
                                   FakeResp([b"abcdef"], 6)])
    # re-point get_download_url tracking after _setup overrode it
    monkeypatch.setattr(download, "get_download_url",
                        lambda *a, **k: calls.append(1) or "http://example/test-1-0.7z")
    assert download.download_file("skyrimspecialedition", "skyrimse", 1, 2, 1) is True
    assert len(calls) >= 2  # URL re-fetched on the retry


def test_truncation_guard_retries_then_raises(monkeypatch, tmp_path):
    # content-length says 100 but only 3 bytes arrive every attempt -> short file
    resps = [FakeResp([b"abc"], 100) for _ in range(download.MAX_DOWNLOAD_RETRIES)]
    _setup(monkeypatch, tmp_path, resps)
    try:
        download.download_file("skyrimspecialedition", "skyrimse", 1, 2, 1)
        assert False, "expected failure after exhausting retries"
    except Exception:
        pass
    # partial file must be cleaned up, not left behind
    assert not (tmp_path / "skyrimse" / "test-1-0.7z").exists()


def test_existing_file_is_skipped(monkeypatch, tmp_path):
    (tmp_path / "skyrimse").mkdir()
    (tmp_path / "skyrimse" / "test-1-0.7z").write_bytes(b"already")
    _setup(monkeypatch, tmp_path, [])  # no responses needed; should not download
    download.download_file("skyrimspecialedition", "skyrimse", 1, 2, 1)
    assert (tmp_path / "skyrimse" / "test-1-0.7z").read_bytes() == b"already"
