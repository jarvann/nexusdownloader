"""Single source of truth for the paths every tab needs.

Picking a game + collection once should wire up Downloads, Install (staging), and
Deploy (game Data folder / plugins / SKSE) everywhere. Previously each tab kept
its own copies synced by ad-hoc signals, which drifted. Now all tabs share one
``SessionPaths`` instance: writers call :meth:`update` / :meth:`apply_game`, and
every tab subscribes to :attr:`changed` to refresh its own widgets.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QObject, Signal

# Fields the app threads through the tabs. ``game_data`` is the game's Data
# folder (deploy target); ``staging`` is Vortex's mod staging; ``downloads`` is
# the Nexus download cache.
_FIELDS = ("game", "collection", "downloads", "staging",
           "game_data", "localappdata", "skse")


class SessionPaths(QObject):
    """Shared, observable bag of the current game/collection paths."""

    changed = Signal()

    def __init__(self):
        super().__init__()
        for f in _FIELDS:
            setattr(self, f, "")

    def update(self, **kw) -> bool:
        """Set any provided non-empty fields; emit ``changed`` if anything moved."""
        dirty = False
        for k, v in kw.items():
            if k in _FIELDS and v and getattr(self, k) != v:
                setattr(self, k, v)
                dirty = True
        if dirty:
            self.changed.emit()
        return dirty

    def apply_game(self, g: dict) -> bool:
        """Populate from a ``vortex_db.read_vortex_games`` dict.

        Derives the deploy-side paths the game pick uniquely knows: the Data
        folder (``<install>/Data``) and the SKSE loader, so the Deploy tab gets
        them without a separate detection pass.
        """
        kw = {
            "game": g.get("game", ""),
            "downloads": g.get("downloads", ""),
            "staging": g.get("staging", ""),
        }
        install = g.get("install") or ""
        if install:
            data = os.path.join(install, "Data")
            if os.path.isdir(data):
                kw["game_data"] = data
            skse = os.path.join(install, "skse64_loader.exe")
            if os.path.exists(skse):
                kw["skse"] = skse
        return self.update(**kw)


_INSTANCE: "SessionPaths | None" = None


def session_paths() -> SessionPaths:
    """The process-wide shared SessionPaths (created on first use)."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = SessionPaths()
    return _INSTANCE
