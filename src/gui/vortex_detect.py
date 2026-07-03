"""Shared "Detect from Vortex" logic for every GUI auto-detect button.

All auto-detect buttons must go through here so they behave identically. They
read Vortex's live ``state.v2`` LevelDB (``utils.vortex_db.read_vortex_games``),
which is the only thing that works on modern Vortex. The legacy JSON-config
reader (``utils.vortex_config``) returns nothing on current Vortex -- that's why
several buttons silently did nothing while one (this path) worked.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtWidgets import QMessageBox, QInputDialog

# Collections use the Nexus DOMAIN (e.g. 'skyrimspecialedition'); Vortex's game
# id is shorter (e.g. 'skyrimse'). Map the common ones so the right game gets
# pre-selected from a loaded collection.
DOMAIN_TO_GAME = {
    "skyrimspecialedition": "skyrimse", "skyrim": "skyrim",
    "skyrimvr": "skyrimvr", "oblivion": "oblivion",
    "fallout4": "fallout4", "fallout4vr": "fallout4vr",
    "falloutnv": "falloutnv", "starfield": "starfield",
}


def domain_to_game_id(domain: Optional[str]) -> Optional[str]:
    return DOMAIN_TO_GAME.get((domain or "").lower(), domain)


def read_games(parent=None, warn: bool = True) -> List[Dict[str, Any]]:
    """Return Vortex's mod-managed games (``{game,install,staging,downloads,store}``).

    On no-DB / locked-DB returns ``[]`` and (when ``warn`` and a ``parent`` is
    given) shows the appropriate dialog. Requires Vortex CLOSED.
    """
    from utils import vortex_db
    db = vortex_db.find_state_db()
    if not db:
        if warn and parent is not None:
            QMessageBox.warning(parent, "Vortex Not Found",
                "Could not find Vortex's database (state.v2). Make sure Vortex is "
                "installed and has been run at least once.")
        return []
    try:
        return vortex_db.read_vortex_games(db)
    except Exception as e:
        if warn and parent is not None:
            QMessageBox.warning(parent, "Close Vortex",
                "Couldn't read Vortex's configuration -- its database is locked.\n\n"
                "Close Vortex completely, then try again.\n\n"
                f"Details: {e}")
        return []


def pick_game(parent, prefer_domain: Optional[str] = None, warn: bool = True,
              title: str = "Select Game",
              prompt: str = "Pick the game to set up:") -> Optional[Dict[str, Any]]:
    """Read games, then return the one the user picks (auto-picks if only one).

    ``prefer_domain`` (a collection's domainName) pre-selects the matching game.
    Returns ``None`` if there are no games or the user cancels.
    """
    games = read_games(parent, warn=warn)
    if not games:
        if warn and parent is not None:
            # read_games warns for no-DB/locked; this is the "DB ok but no games" case.
            from utils import vortex_db
            if vortex_db.find_state_db():
                QMessageBox.information(parent, "No Games Found",
                    "Vortex has no mod-managed games configured.")
        return None
    if len(games) == 1:
        return games[0]
    game_id = domain_to_game_id(prefer_domain)
    labels = [g["game"] + (f"  [{g['store']}]" if g.get("store") else "") for g in games]
    default_idx = next((i for i, g in enumerate(games) if g["game"] == game_id), 0)
    choice, ok = QInputDialog.getItem(parent, title, prompt, labels, default_idx, False)
    if not ok:
        return None
    return games[labels.index(choice)]


def find_staging_for(game_id: str = "skyrimse") -> str:
    """Non-interactive staging path for one game id (``''`` if unknown/locked)."""
    for g in read_games(None, warn=False):
        if g["game"] == game_id:
            return g.get("staging") or ""
    return ""
