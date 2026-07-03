"""Global setup header: Game + Collection pickers above the tabs.

This is the single place the user chooses what to work on. Picking a game reads
its paths from Vortex (Downloads / Staging / Data folder / SKSE) and the
collection dropdown lists the collections in that game's staging. Both writes go
to :mod:`gui.session_paths`, the shared store every tab subscribes to -- so there
is exactly one game picker and one collection picker for the whole app.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QComboBox, QPushButton,
)

from gui.collection_selector import CollectionSelector
from gui.session_paths import session_paths


class SetupHeader(QWidget):
    """One Game dropdown + one Collection dropdown, driving the shared store."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._games = []
        self._session = session_paths()
        self._build()
        self._populate_games()

    # expose the embedded collection selector for back-compat references
    @property
    def collection_selector(self) -> CollectionSelector:
        return self._collection

    def _build(self):
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 4, 4, 4)
        row.addWidget(QLabel("Game:"))
        self.game_combo = QComboBox()
        self.game_combo.setMinimumWidth(200)
        self.game_combo.setToolTip("Games Vortex manages (read from its database)")
        self.game_combo.currentIndexChanged.connect(self._on_game_changed)
        row.addWidget(self.game_combo)
        self.refresh_btn = QPushButton("↻")
        self.refresh_btn.setFixedWidth(32)
        self.refresh_btn.setToolTip("Re-read games from Vortex (close Vortex first)")
        self.refresh_btn.clicked.connect(self._populate_games)
        row.addWidget(self.refresh_btn)

        row.addSpacing(12)
        self._collection = CollectionSelector()
        self._collection.collection_selected.connect(
            lambda p: self._session.update(collection=p) if p else None)
        row.addWidget(self._collection, 1)

    def _populate_games(self):
        from gui.vortex_detect import read_games
        self._games = read_games(self, warn=False)
        self.game_combo.blockSignals(True)
        self.game_combo.clear()
        if not self._games:
            self.game_combo.addItem("— no games (close Vortex, ↻) —", None)
        for g in self._games:
            self.game_combo.addItem(
                g["game"] + (f"  [{g['store']}]" if g.get("store") else ""), g["game"])
        self.game_combo.blockSignals(False)
        if self._games:
            self._on_game_changed(0)

    def _on_game_changed(self, idx: int):
        if not self._games or idx < 0 or idx >= len(self._games):
            return
        g = self._games[idx]
        self._session.apply_game(g)          # publishes downloads/staging/Data/SKSE
        if g.get("staging"):
            self._collection.set_staging(g["staging"])   # repopulate collections
