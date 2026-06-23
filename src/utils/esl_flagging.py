"""
ESL ("light plugin") auto-flagging -- our replacement for the manual Vortex step
the collection author documents:

    "sort by could be light, select all, and mark them all as light"

Skyrim SE caps the load order at 254 *full* plugins + 4096 *light* plugins. Big
collections stay under the full cap by ESL-flagging every plugin that is
*eligible* -- i.e. all of the records the plugin **adds** (not the ones it
overrides) have an object index that fits the light FormID range (0x000-0xFFF).
Vortex gets this eligibility from LOOT (``isValidAsLightPlugin``) and sets the
flag by writing the 0x200 bit into the TES4 header flags at byte offset 8
(``ESPFile.setLightFlag``). We reproduce both here:

* :func:`analyze_plugin` parses the plugin's record tree (headers only -- record
  *data* is skipped, never decompressed) and decides ``could_be_light``.
* :func:`set_light_flag` writes the 0x200 bit exactly like Vortex.
* :func:`mark_eligible_as_light` runs the pass over a list of deployed plugins.

This is a faithful re-implementation of LOOT's rule, not a call into LOOT, so the
resulting count should be sanity-checked against the author's target (~250 full).
"""

from __future__ import annotations

import os
import struct
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

FLAG_MASTER = 0x00000001     # ESM
FLAG_LIGHT = 0x00000200      # ESL / light master (Skyrim SE / Fallout 4)
_TAG_TES4 = b"TES4"
_TAG_GRUP = b"GRUP"
_TAG_HEDR = b"HEDR"
_TAG_MAST = b"MAST"
# Light plugins may only add records whose object index is in 0x000-0xFFF
# (Skyrim SE 1.6.1130+/AE). Anything above means the plugin cannot be light.
_MAX_LIGHT_OBJECT_INDEX = 0xFFF
_MAX_LIGHT_RECORDS = 4096


@dataclass
class PluginInfo:
    is_plugin: bool = False
    flags: int = 0
    is_light: bool = False        # already ESL-flagged (or .esl extension)
    is_master: bool = False
    new_records: int = 0          # records the plugin ADDS (not overrides)
    could_be_light: bool = False  # eligible to be flagged but isn't yet
    reason: str = ""              # why not eligible (for logging)


def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _count_masters(buf: bytes, start: int, end: int) -> int:
    """Count MAST subrecords in the TES4 record's data region."""
    pos, masters = start, 0
    while pos + 6 <= end and pos + 6 <= len(buf):
        tag = buf[pos:pos + 4]
        size = struct.unpack_from("<H", buf, pos + 4)[0]
        pos += 6
        if tag == _TAG_MAST:
            masters += 1
        pos += size
    return masters


def analyze_buffer(buf: bytes, ext: str = "") -> PluginInfo:
    """Decide ``could_be_light`` from an in-memory plugin file.

    Walks the GRUP/record tree reading only 24-byte (or 20 for Oblivion-style)
    headers, skipping record *data* by its declared size -- so compressed records
    are handled without decompressing (the FormID lives in the uncompressed
    header at offset 12). Bails early the moment an added record's object index
    exceeds the light range. On any structural surprise it returns
    ``could_be_light=False`` -- we only ever flag when we're certain.
    """
    info = PluginInfo()
    if len(buf) < 24 or buf[:4] != _TAG_TES4:
        return info
    info.is_plugin = True

    tes4_size = _u32(buf, 4)
    info.flags = _u32(buf, 8)
    info.is_master = bool(info.flags & FLAG_MASTER)
    info.is_light = bool(info.flags & FLAG_LIGHT) or ext.lower() == ".esl"

    # Oblivion-style records have a 20-byte header (the 4 "version" bytes are
    # actually the first subrecord tag, "HEDR"); Skyrim/Fallout use 24.
    hdr = 20 if buf[20:24] == _TAG_HEDR else 24
    masters = _count_masters(buf, hdr, hdr + tes4_size)

    if info.is_light:
        info.could_be_light = False
        info.reason = "already light"
        return info

    pos = hdr + tes4_size
    n = len(buf)
    new_records = 0
    eligible = True
    reason = ""
    try:
        while pos + hdr <= n:
            sig = buf[pos:pos + 4]
            size = _u32(buf, pos + 4)
            if sig == _TAG_GRUP:
                pos += hdr           # descend: children follow the group header
                continue
            formid = _u32(buf, pos + 12)
            # A record is "new" (added by this plugin) when its FormID's mod
            # index equals the plugin's own slot == the number of masters.
            if (formid >> 24) == masters:
                new_records += 1
                if (formid & 0x00FFFFFF) > _MAX_LIGHT_OBJECT_INDEX:
                    eligible = False
                    reason = f"object index 0x{formid & 0xFFFFFF:X} > 0xFFF"
                    break
            pos += hdr + size
    except struct.error:
        eligible = False
        reason = "malformed record structure"

    info.new_records = new_records
    if eligible and new_records > _MAX_LIGHT_RECORDS:
        eligible = False
        reason = f"{new_records} new records > {_MAX_LIGHT_RECORDS}"
    info.could_be_light = eligible
    info.reason = reason if not eligible else "eligible"
    return info


def analyze_plugin(path: str) -> PluginInfo:
    """Read a plugin file and analyze it (returns a non-plugin PluginInfo on error)."""
    try:
        with open(path, "rb") as fh:
            buf = fh.read()
    except OSError:
        return PluginInfo()
    return analyze_buffer(buf, ext=os.path.splitext(path)[1])


def set_light_flag(path: str, enabled: bool = True) -> None:
    """Set/clear the 0x200 light bit in the TES4 header flags (offset 8).

    Identical to Vortex's ``ESPFile.setLightFlag`` -- a 4-byte in-place write, so
    it transparently updates the hard-linked copy in the game's Data folder too.
    """
    with open(path, "r+b") as fh:
        fh.seek(8)
        flags = struct.unpack("<I", fh.read(4))[0]
        flags = (flags | FLAG_LIGHT) if enabled else (flags & ~FLAG_LIGHT)
        fh.seek(8)
        fh.write(struct.pack("<I", flags))


@dataclass
class EslifyResult:
    flagged: int = 0              # plugins newly flagged light
    already_light: int = 0
    not_eligible: int = 0
    errors: int = 0
    flagged_names: List[str] = None
    error_names: List[str] = None

    def __post_init__(self):
        if self.flagged_names is None:
            self.flagged_names = []
        if self.error_names is None:
            self.error_names = []


def mark_eligible_as_light(data_dir: str, names: Sequence[str], *,
                           workers: int = 8,
                           log: Optional[Callable[[str, str], None]] = None,
                           progress: Optional[Callable[[int, int], None]] = None
                           ) -> EslifyResult:
    """Flag every eligible-but-unflagged plugin in ``names`` as light.

    ``names`` are plugin filenames under ``data_dir`` (typically the collection's
    active plugin list). Only ``.esp`` files are considered -- ``.esl``/``.esm``
    are skipped. Analysis is threaded (header-only reads); the in-place flag
    write is tiny. Failures never raise: a plugin we can't read/parse/flag is
    counted as an error and left untouched.
    """
    res = EslifyResult()
    candidates = [n for n in names if n.lower().endswith(".esp")]
    total = len(candidates)

    def work(name: str) -> str:
        path = os.path.join(data_dir, name)
        info = analyze_plugin(path)
        if not info.is_plugin:
            return "error"
        if info.is_light:
            return "already_light"
        if not info.could_be_light:
            return "not_eligible"
        try:
            set_light_flag(path, True)
        except OSError:
            return "error"
        return "flagged"

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for name, outcome in zip(candidates, ex.map(work, candidates)):
            done += 1
            if outcome == "flagged":
                res.flagged += 1
                res.flagged_names.append(name)
            elif outcome == "already_light":
                res.already_light += 1
            elif outcome == "not_eligible":
                res.not_eligible += 1
            else:
                res.errors += 1
                res.error_names.append(name)
            if progress and (done % 50 == 0 or done == total):
                progress(done, total)
    if log:
        log("INFO", f"ESL pass: flagged {res.flagged} light, "
                    f"{res.already_light} already light, {res.not_eligible} not eligible, "
                    f"{res.errors} errors")
    return res
