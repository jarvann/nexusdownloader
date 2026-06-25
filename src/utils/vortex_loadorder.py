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

import os
import re
import shutil
import struct
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# Skyrim SE base masters in canonical load order. Always loaded, never listed in
# plugins.txt (they still appear, in this order, at the top of loadorder.txt).
# CC content (cc*.es[lmp]) loads after these but before mods and is also excluded
# from plugins.txt -- see :func:`is_vanilla_master`.
SKYRIMSE_VANILLA_ORDER = [
    "Skyrim.esm", "Update.esm", "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm",
]
SKYRIMSE_VANILLA = {n.lower() for n in SKYRIMSE_VANILLA_ORDER}
PLUGIN_EXTS = (".esp", ".esm", ".esl")

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
    from utils.vortex_sync import _best_match   # deferred: avoid import-time cost
    by_md5: Dict[str, str] = {}
    by_logical: Dict[str, str] = {}
    recorded: set = set()
    for m in mods:
        s = m.get("source") or {}
        folders = folder_by_modid.get(str(s.get("modId")), [])
        if not folders:
            continue
        # Disambiguate shared-modId variants the SAME way the Link path does, so a
        # rule endpoint resolves to the specific variant it names, not folders[0]
        # (which mis-attributes ordering constraints across variants of one modId).
        folder = _best_match([f for f in folders if f not in recorded] or folders, m)
        recorded.add(folder)
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


# Bethesda Creation Club plugins follow a fixed scheme: "cc" + a 3-char dev
# code + "sse" + a 3-digit number (e.g. ccBGSSSE001, ccafdsse001, cceejsse001).
# Match THAT, not any "cc" prefix -- collection mods legitimately start with "cc"
# (Creation Club *patches/addons* like "cc open helmets.esp", "ccquest -
# experience patch.esp"), and a bare startswith("cc") was wrongly dropping those
# from plugins.txt, leaving them disabled in-game.
_BASE_CC_RE = re.compile(r"^cc[a-z0-9]{3}sse\d{3}", re.IGNORECASE)


def is_vanilla_master(name: str, vanilla: Iterable[str] = SKYRIMSE_VANILLA) -> bool:
    """True for base-game / Creation Club plugins (excluded from plugins.txt)."""
    low = name.lower()
    return low in set(vanilla) or bool(_BASE_CC_RE.match(low))


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
                       enabled: Dict[str, bool], *, strict: bool = False) -> str:
    """``plugins.txt``: non-vanilla plugins, lowercase, ``*`` prefix when enabled.

    ``strict`` (set when the collection ships an explicit plugin list): plugins
    that are deployed but NOT in that list are written *un*-activated. Mods often
    ship optional/alternate ESPs the collection deliberately leaves off; auto-
    enabling every ESP on disk pushed the active count past Skyrim's hard caps
    (254 full / 4096 light). Without a list we keep the old default-on behavior.
    """
    lines = [
        "# This file is used by Skyrim to keep track of your downloaded content.",
        "# Please do not modify this file.",
    ]
    for p in ordered_active:
        default = not strict          # unlisted plugins: off in strict mode
        mark = "*" if enabled.get(p.lower(), default) else ""
        lines.append(f"{mark}{p.lower()}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Compose a full load order from deployed plugins + collection rules
# --------------------------------------------------------------------------- #
def scan_plugins(data_dir: str) -> List[str]:
    """List plugin files (.esp/.esm/.esl) directly in the game's Data folder."""
    try:
        names = os.listdir(data_dir)
    except OSError:
        return []
    return sorted(n for n in names
                  if n.lower().endswith(PLUGIN_EXTS)
                  and os.path.isfile(os.path.join(data_dir, n)))


def after_map(collection: dict) -> Dict[str, List[str]]:
    """Extract ``{plugin: [after...]}`` from ``collection.pluginRules.plugins``."""
    rules = ((collection.get("pluginRules") or {}).get("plugins")) or []
    return {r["name"]: list(r.get("after") or []) for r in rules if r.get("name")}


def enabled_map(collection: dict) -> Dict[str, bool]:
    """Extract ``{plugin_lower: enabled}`` from ``collection.plugins`` (deduped)."""
    out: Dict[str, bool] = {}
    for p in collection.get("plugins") or []:
        if p.get("name"):
            out[p["name"].lower()] = bool(p.get("enabled", True))
    return out


def compose_load_order(scanned: Sequence[str], data_dir: str,
                       plugin_after: Optional[Dict[str, List[str]]] = None
                       ) -> Tuple[List[str], List[str]]:
    """Build ``(full_order, active_order)`` from scanned Data-folder plugins.

    ``full_order`` (-> loadorder.txt) is: canonical vanilla masters, then CC
    content, then everything else ordered masters-first + collection rules.
    ``active_order`` (-> plugins.txt) is just the non-vanilla/non-CC plugins, in
    the same relative order.
    """
    present = {n.lower(): n for n in scanned}
    vanilla = [present[v] for v in SKYRIMSE_VANILLA if v in present]
    vanilla += [n for n in scanned if is_vanilla_master(n) and n.lower() not in SKYRIMSE_VANILLA]
    vanilla_set = {n.lower() for n in vanilla}

    others = [n for n in scanned if n.lower() not in vanilla_set]

    def master_here(name: str) -> bool:
        return is_master_block(name, os.path.join(data_dir, name))

    active = order_plugins(others, master_here, plugin_after)
    # canonical vanilla first (core in fixed order, CC sorted), then ordered rest
    core = [present[v.lower()] for v in SKYRIMSE_VANILLA_ORDER if v.lower() in present]
    cc = sorted((n for n in vanilla if n.lower() not in SKYRIMSE_VANILLA),
                key=str.lower)
    full = core + cc + active
    return full, active


def write_load_order(localappdata_dir: str, full_order: Sequence[str],
                     active_order: Sequence[str], enabled: Dict[str, bool], *,
                     backup: bool = True, strict: bool = False) -> Tuple[str, str]:
    """Write ``loadorder.txt`` + ``plugins.txt`` (backing up any existing pair).

    Returns the two written paths. ``localappdata_dir`` is the game's
    ``%LOCALAPPDATA%/<Game>`` folder (e.g. ``.../Skyrim Special Edition``).
    """
    lo_path = os.path.join(localappdata_dir, "loadorder.txt")
    pl_path = os.path.join(localappdata_dir, "plugins.txt")
    if backup:
        for path in (lo_path, pl_path):
            if os.path.isfile(path):
                shutil.copy2(path, path + ".nxd-bak")
    os.makedirs(localappdata_dir, exist_ok=True)
    with open(lo_path, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(render_loadorder(full_order))
    with open(pl_path, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(render_plugins_txt(active_order, enabled, strict=strict))
    return lo_path, pl_path


def sort_plugins(collection: dict, game_data_dir: str, localappdata_dir: str, *,
                 backup: bool = True) -> Tuple[str, str, int]:
    """End-to-end collection-rules plugin sort: scan -> order -> write the files.

    Returns ``(loadorder_path, plugins_path, active_count)``.
    """
    scanned = scan_plugins(game_data_dir)
    full, active = compose_load_order(scanned, game_data_dir, after_map(collection))
    enabled = enabled_map(collection)
    # Strict activation only when the collection actually ships a plugin list,
    # so we don't accidentally disable everything for list-less collections.
    strict = bool(enabled)
    lo_path, pl_path = write_load_order(localappdata_dir, full, active, enabled,
                                        backup=backup, strict=strict)
    # Count what's actually activated (strict drops unlisted plugins).
    active_count = sum(1 for p in active
                       if enabled.get(p.lower(), not strict))
    return lo_path, pl_path, active_count
