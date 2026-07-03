"""Detect whether the current process is running elevated (Administrator / root).

Mixing elevation levels corrupts staging: files created by an elevated run are
owned by the Administrators group, so a later *non-elevated* run gets
``WinError 5 (Access denied)`` trying to delete them -- which silently breaks
Remove Installation. We surface the current elevation so the user can keep every
run at the same (ideally non-admin) level.
"""
from __future__ import annotations

import os


def is_elevated() -> bool:
    """True if running as Administrator (Windows) or root (POSIX). Never raises."""
    try:
        if os.name == "nt":
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return hasattr(os, "geteuid") and os.geteuid() == 0
    except Exception:
        return False


def looks_like_permission_error(text: str) -> bool:
    """Heuristic: does this error text look like an access/ownership denial?

    Used to tell an elevation-mismatch failure apart from a generic I/O error so
    we can point the user at the real fix (match the install's elevation)."""
    t = (text or "").lower()
    return any(s in t for s in (
        "winerror 5", "access is denied", "access denied",
        "errno 13", "permission denied", "permissionerror",
    ))
