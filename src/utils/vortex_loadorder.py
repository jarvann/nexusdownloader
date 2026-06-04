"""
Collection rule ordering: mod deploy order (pre-deploy) and plugin load order
(post-deploy).

Two distinct orderings, both reverse-engineered from a real collection.json +
the load-order files Vortex writes:

* **Mod rules -> deploy order.** ``collection.json:modRules`` is a list of
  ``{type: "after"|"before", source, reference}`` constraints (endpoints matched
  by ``fileMD5``/``logicalFileName``/folder name). They decide which mod's files
  win a conflict, so they must be resolved into a deploy order *before* deploying
  (the override is "last wins"; an "after" mod ends up later -> wins). See
  :func:`order_mods`.

* **Plugin rules -> load order.** ``collection.json:pluginRules.plugins`` gives
  explicit per-plugin ``after`` constraints; with a masters-first invariant these
  produce ``plugins.txt`` / ``loadorder.txt`` *after* deploy. See
  :func:`order_plugins` and :func:`render_plugins_txt` / :func:`render_loadorder`.

  NOTE: ``pluginRules.groups`` (LOOT group ordering) is intentionally NOT applied
  here -- the group->plugin assignment comes from LOOT's masterlist, which the
  collection doesn't carry. This module honors the collection's *explicit* plugin
  rules + masters-first; a full LOOT pass is a separate concern.
"""

from __future__ import annotations

import struct
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# Skyrim SE base masters that are always loaded and never listed in plugins.txt
# (they still appear in loadorder.txt). CC content (cc*.es[lmp]) is treated as
# vanilla-ish via the cc-prefix check in :func:`is_vanilla_master`.
SKYRIMSE_VANILLA = {
    "skyrim.esm", "update.esm", "dawnguard.esm", "hearthfires.esm", "dragonborn.esm",
}

_MASTER_FLAG = 0x00000001   # ESM flag in the TES4 record header
_LIGHT_FLAG = 0x00000200    # ESL / light-master flag


# --------------------------------------------------------------------------- #
# Generic stable topological sort
# --------------------------------------------------------------------------- #
def stable_toposort(nodes: Sequence[str],
                    edges: Iterable[Tuple[str, str]]) -> List[str]:
    """Topologically sort ``nodes`` honoring ``(before, after)`` edges, breaking
    ties by original order. Unknown edge endpoints and cycles are tolerated
    (a cycle's remaining nodes are appended in original order)."""
    pos = {n: i for i, n in enumerate(nodes)}
    succ: Dict[str, List[str]] = {n: [] for n in nodes}
    indeg: Dict[str, int] = {n: 0 for n in nodes}
    seen = set()
    for a, b in edges:
        if a in pos and b in pos and a != b and (a, b) not in seen:
            seen.add((a, b))
            succ[a].append(b)
            indeg[b] += 1

    import heapq
    avail = [pos[n] for n in nodes if indeg[n] == 0]
    heapq.heapify(avail)
    order: List[str] = []
    while avail:
        n = nodes[heapq.heappop(avail)]
        order.append(n)
        for m in succ[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                heapq.heappush(avail, pos[m])
    if len(order) != len(nodes):   # cycle -> append leftovers stably
        done = set(order)
        order += [n for n in nodes if n not in done]
    return order


# --------------------------------------------------------------------------- #
# Mod rules -> deploy order
# --------------------------------------------------------------------------- #
def build_mod_resolver(mods: List[dict], folder_by_modid: Dict[str, List[str]]
                       ) -> Callable[[dict], Optional[str]]:
    """Return a resolver mapping a modRule endpoint to an installed folder name.

    Matches by ``fileMD5`` first (most reliable), then ``logicalFileName``, then
    falls back to a literal ``fileExpression``/``idHint`` (which is a folder name).
    """
    by_md5: Dict[str, str] = {}
    by_logical: Dict[str, str] = {}
    for m in mods:
        s = m.get("source") or {}
        folders = folder_by_modid.get(str(s.get("modId")), [])
        if not folders:
            continue
        folder = folders[0]
        if s.get("md5"):
            by_md5[s["md5"].lower()] = folder
        if s.get("logicalFilename"):
            by_logical[s["logicalFilename"].lower()] = folder

    def resolve(endpoint: dict) -> Optional[str]:
        md5 = (endpoint.get("fileMD5") or "").lower()
        if md5 and md5 in by_md5:
            return by_md5[md5]
        lf = (endpoint.get("logicalFileName") or "").lower()
        if lf and lf in by_logical:
            return by_logical[lf]
        for key in ("fileExpression", "idHint"):
            if endpoint.get(key):
                return endpoint[key]
        return None

    return resolve


def mod_rule_edges(mod_rules: List[dict],
                   resolve: Callable[[dict], Optional[str]]) -> List[Tuple[str, str]]:
    """Convert modRules into ``(before_folder, after_folder)`` edges."""
    edges: List[Tuple[str, str]] = []
    for r in mod_rules or []:
        src = resolve(r.get("source") or {})
        ref = resolve(r.get("reference") or {})
        if not src or not ref or src == ref:
            continue
        if r.get("type") == "after":        # source after reference
            edges.append((ref, src))
        elif r.get("type") == "before":     # source before reference
            edges.append((src, ref))
    return edges


def order_mods(folder_keys: Sequence[str], mod_rules: List[dict],
               resolve: Callable[[dict], Optional[str]]) -> List[str]:
    """Order installed mod folders for deployment per the collection's modRules."""
    return stable_toposort(folder_keys, mod_rule_edges(mod_rules, resolve))


# --------------------------------------------------------------------------- #
# Plugin master detection + load order
# --------------------------------------------------------------------------- #
def read_record_flags(path: str) -> int:
    """Read the TES4 header record flags (0 if unreadable / not a plugin)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
    except OSError:
        return 0
    if len(head) < 12 or head[:4] != b"TES4":
        return 0
    return struct.unpack("<I", head[8:12])[0]


def is_master_block(name: str, path: Optional[str] = None) -> bool:
    """True if the plugin loads in the master block (.esm/.esl, or ESM/ESL flag)."""
    ext = name.lower().rsplit(".", 1)[-1]
    if ext in ("esm", "esl"):
        return True
    if path and (read_record_flags(path) & (_MASTER_FLAG | _LIGHT_FLAG)):
        return True
    return False


def is_vanilla_master(name: str, vanilla: Iterable[str] = SKYRIMSE_VANILLA) -> bool:
    """True for base-game / Creation Club plugins (excluded from plugins.txt)."""
    low = name.lower()
    return low in set(vanilla) or low.startswith("cc")


def order_plugins(plugin_names: Sequence[str],
                  is_master_fn: Callable[[str], bool],
                  plugin_after: Optional[Dict[str, List[str]]] = None) -> List[str]:
    """Order plugins masters-first, applying explicit ``after`` rules within each
    block (cross-block ``after`` edges are dropped -- masters always precede)."""
    lower_to_name = {p.lower(): p for p in plugin_names}
    edges: List[Tuple[str, str]] = []
    for name, afters in (plugin_after or {}).items():
        tgt = lower_to_name.get(name.lower())
        if not tgt:
            continue
        for a in afters:
            src = lower_to_name.get(a.lower())
            if src and src != tgt:
                edges.append((src, tgt))   # src loads before tgt

    masters = [p for p in plugin_names if is_master_fn(p)]
    regular = [p for p in plugin_names if not is_master_fn(p)]
    ms, rs = set(masters), set(regular)
    ordered_m = stable_toposort(masters, [(a, b) for a, b in edges if a in ms and b in ms])
    ordered_r = stable_toposort(regular, [(a, b) for a, b in edges if a in rs and b in rs])
    return ordered_m + ordered_r


# --------------------------------------------------------------------------- #
# Render the load-order files (exact Vortex format)
# --------------------------------------------------------------------------- #
def render_loadorder(ordered_all: Sequence[str]) -> str:
    """``loadorder.txt``: every plugin, original case, Vortex header."""
    return "# Automatically generated by Vortex\n" + "".join(f"{p}\n" for p in ordered_all)


def render_plugins_txt(ordered_active: Sequence[str],
                       enabled: Dict[str, bool]) -> str:
    """``plugins.txt``: non-vanilla plugins, lowercase, ``*`` prefix when enabled."""
    lines = [
        "# This file is used by Skyrim to keep track of your downloaded content.",
        "# Please do not modify this file.",
    ]
    for p in ordered_active:
        mark = "*" if enabled.get(p.lower(), True) else ""
        lines.append(f"{mark}{p.lower()}")
    return "\n".join(lines) + "\n"
