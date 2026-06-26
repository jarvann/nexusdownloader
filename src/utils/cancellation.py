"""Cooperative cancellation -- the Python analogue of .NET's CancellationToken.

Python can't forcibly kill a thread, so cancellation here is *cooperative*: a
shared, thread-safe flag that long-running work polls (``token.cancelled`` /
``token.raise_if_cancelled()``) and unwinds itself when set. The token also
carries a callback registry so cancelling can actively tear down a resource that
isn't poll-friendly -- e.g. terminating a child process.

Typical use:

    token = CancellationToken()
    token.register(lambda: proc.terminate())     # fire-on-cancel teardown
    for item in work:
        token.raise_if_cancelled()               # bail between units
        do(item)

and from another thread / a signal handler: ``token.cancel()``.
"""

from __future__ import annotations

import signal
import threading
from typing import Callable, List


class OperationCancelled(Exception):
    """Raised by :meth:`CancellationToken.raise_if_cancelled` to unwind work."""


class CancellationToken:
    """A one-way, thread-safe cancellation flag with fire-on-cancel callbacks."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: List[Callable[[], None]] = []

    # -- state ---------------------------------------------------------------- #
    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise OperationCancelled()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until cancelled or ``timeout`` elapses. Returns True if cancelled.

        Use this instead of ``time.sleep`` in polling loops so a cancel wakes the
        wait immediately rather than after the full interval.
        """
        return self._event.wait(timeout)

    # -- control -------------------------------------------------------------- #
    def cancel(self) -> None:
        """Signal cancellation (idempotent) and run any registered callbacks."""
        with self._lock:
            first = not self._event.is_set()
            self._event.set()
            callbacks = list(self._callbacks)
        if first:
            for cb in callbacks:
                try:
                    cb()
                except Exception:
                    pass            # teardown must never raise back into cancel()

    def register(self, callback: Callable[[], None]) -> None:
        """Run ``callback`` when cancelled -- immediately if already cancelled.

        Used for non-poll teardown (kill a subprocess, close a handle). Each
        callback runs at most once.
        """
        with self._lock:
            if not self._event.is_set():
                self._callbacks.append(callback)
                return
        callback()


def install_graceful_shutdown(on_signal: Callable[[int], None]) -> bool:
    """Route Ctrl-C / kill signals to ``on_signal`` for a graceful wind-down.

    Registers SIGINT and SIGTERM (plus SIGBREAK on Windows, raised by
    CTRL_BREAK_EVENT) so a kill request starts an orderly shutdown instead of
    crashing with a raw KeyboardInterrupt. ``on_signal`` runs at most once -- a
    second signal is ignored, so an impatient double Ctrl-C won't re-enter it
    (the OS still hard-kills on repeated signals as a last resort).

    MUST be called from the main thread (a Python limitation). Returns True if at
    least one handler was installed.
    """
    state = {"fired": False}

    def handler(signum: int, _frame) -> None:
        if state["fired"]:
            return
        state["fired"] = True
        try:
            on_signal(signum)
        except Exception:
            pass

    sigs = [signal.SIGINT, signal.SIGTERM]
    sigbreak = getattr(signal, "SIGBREAK", None)   # Windows-only
    if sigbreak is not None:
        sigs.append(sigbreak)

    installed = False
    for s in sigs:
        try:
            signal.signal(s, handler)
            installed = True
        except (ValueError, OSError, RuntimeError):
            pass        # not main thread / unsupported on this platform
    return installed
