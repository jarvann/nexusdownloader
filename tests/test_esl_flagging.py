"""Tests for ESL eligibility analysis + flag writing (utils.esl_flagging)."""
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from utils import esl_flagging as esl


# --- synthetic Skyrim-SE plugin builders (24-byte headers) ------------------ #
def _rec(sig: bytes, formid: int, data: bytes = b"\x00") -> bytes:
    return (sig + struct.pack("<I", len(data)) + struct.pack("<I", 0)
            + struct.pack("<I", formid) + b"\x00" * 8 + data)


def _grup(label: bytes, *records: bytes) -> bytes:
    body = b"".join(records)
    return (b"GRUP" + struct.pack("<I", 24 + len(body)) + label
            + struct.pack("<I", 0) + b"\x00" * 8 + body)


def _tes4(flags: int = 0, masters=()) -> bytes:
    hedr = (b"HEDR" + struct.pack("<H", 12) + struct.pack("<f", 1.7)
            + struct.pack("<i", 10) + struct.pack("<I", 0x800))
    mast = b"".join(b"MAST" + struct.pack("<H", len(m) + 1) + m + b"\x00"
                    for m in masters)
    data = hedr + mast
    return (b"TES4" + struct.pack("<I", len(data)) + struct.pack("<I", flags)
            + struct.pack("<I", 0) + b"\x00" * 8 + data)


def test_eligible_all_low_object_indices():
    buf = _tes4() + _grup(b"WEAP", _rec(b"WEAP", 0x00000123),
                          _rec(b"WEAP", 0x00000456))
    info = esl.analyze_buffer(buf, ".esp")
    assert info.is_plugin and not info.is_light
    assert info.new_records == 2
    assert info.could_be_light is True


def test_not_eligible_high_object_index():
    buf = _tes4() + _grup(b"WEAP", _rec(b"WEAP", 0x00000123),
                          _rec(b"WEAP", 0x00002000))   # 0x2000 > 0xFFF
    info = esl.analyze_buffer(buf, ".esp")
    assert info.could_be_light is False
    assert "0x2000" in info.reason


def test_already_light_is_not_a_candidate():
    buf = _tes4(flags=esl.FLAG_LIGHT) + _grup(b"WEAP", _rec(b"WEAP", 0x00000010))
    info = esl.analyze_buffer(buf, ".esp")
    assert info.is_light is True
    assert info.could_be_light is False


def test_overrides_do_not_disqualify():
    # 1 master: records with mod index 0 are OVERRIDES (not new) -> a high object
    # index there must NOT make the plugin ineligible. The one NEW record
    # (mod index == 1) is low, so the plugin is still a light candidate.
    buf = _tes4(masters=(b"Skyrim.esm",)) + _grup(
        b"WEAP", _rec(b"WEAP", 0x00FFFFFF),    # override of master 0
        _rec(b"WEAP", 0x01000200))             # new record, obj 0x200
    info = esl.analyze_buffer(buf, ".esp")
    assert info.new_records == 1
    assert info.could_be_light is True


def test_esl_extension_counts_as_light():
    buf = _tes4() + _grup(b"WEAP", _rec(b"WEAP", 0x00000010))
    info = esl.analyze_buffer(buf, ".esl")
    assert info.is_light is True and info.could_be_light is False


def test_set_light_flag_roundtrip(tmp_path):
    p = tmp_path / "Test.esp"
    p.write_bytes(_tes4() + _grup(b"WEAP", _rec(b"WEAP", 0x00000010)))
    assert esl.analyze_plugin(str(p)).could_be_light is True
    esl.set_light_flag(str(p), True)
    info = esl.analyze_plugin(str(p))
    assert info.is_light is True
    assert info.could_be_light is False        # now flagged, no longer a candidate
    esl.set_light_flag(str(p), False)
    assert esl.analyze_plugin(str(p)).is_light is False


def test_mark_eligible_as_light_batch(tmp_path):
    (tmp_path / "Good.esp").write_bytes(
        _tes4() + _grup(b"WEAP", _rec(b"WEAP", 0x00000123)))
    (tmp_path / "Big.esp").write_bytes(
        _tes4() + _grup(b"WEAP", _rec(b"WEAP", 0x00009999)))   # high -> not eligible
    (tmp_path / "Already.esl").write_bytes(
        _tes4(flags=esl.FLAG_LIGHT) + _grup(b"WEAP", _rec(b"WEAP", 0x1)))
    res = esl.mark_eligible_as_light(
        str(tmp_path), ["Good.esp", "Big.esp", "Already.esl"], workers=2)
    assert res.flagged == 1 and "Good.esp" in res.flagged_names
    assert res.not_eligible == 1
    # .esl is filtered out (only .esp considered), so it's neither flagged nor counted
    assert esl.analyze_plugin(str(tmp_path / "Good.esp")).is_light is True
