"""Tests for dependency-free Daly D2/81 protocol framing."""

from __future__ import annotations

import importlib
import sys
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec

import pytest

from bmslib.bms_ble.plugins.daly_full_protocol import (
    PROTOCOL_UNIT_81,
    PROTOCOL_UNIT_D2,
    WRITE_FRAME_LEN,
    build_d2_read_frame,
    build_d2_write_frame,
    build_field_write_frame,
    build_restart_write_frame,
    crc_modbus,
    modbus_crc_append,
)


class _BlockAiobmsble(MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "aiobmsble" or fullname.startswith("aiobmsble."):
            return ModuleSpec(fullname, loader=None)
        return None


@pytest.fixture
def block_aiobmsble_imports():
    saved = {
        name: module
        for name, module in list(sys.modules.items())
        if name == "aiobmsble" or name.startswith("aiobmsble.")
    }
    for name in saved:
        del sys.modules[name]
    blocker = _BlockAiobmsble()
    sys.meta_path.insert(0, blocker)
    try:
        yield
    finally:
        sys.meta_path.remove(blocker)
        for name in list(sys.modules.keys()):
            if name == "bmslib.bms_ble.plugins.daly_full_protocol":
                del sys.modules[name]
        sys.modules.update(saved)


def test_crc_modbus_matches_aiobmsble_reference_vectors():
    from aiobmsble.basebms import crc_modbus as aiobmsble_crc_modbus

    vectors = (
        bytes.fromhex("d20300800050"),
        bytes.fromhex("d20600800c1c"),
        bytes.fromhex("810601210001"),
        bytes.fromhex("d20600f00000"),
    )
    for body in vectors:
        assert crc_modbus(body) == aiobmsble_crc_modbus(body)


@pytest.mark.parametrize(
    "addr,count,expected_hex",
    [
        (0x0080, 0x0050, "d2030080005057bd"),
        (0x00D0, 0x001E, "d20300d0001ed798"),
    ],
)
def test_build_d2_read_frame_exact_bytes(addr, count, expected_hex):
    assert build_d2_read_frame(addr, count) == bytes.fromhex(expected_hex)


@pytest.mark.parametrize(
    "builder,expected_hex",
    [
        (lambda: build_d2_write_frame(0x0080, 3100), "d20600800c1c9f48"),
        (lambda: build_restart_write_frame(), "d20600f000009a5a"),
        (lambda: build_field_write_frame("charge_mos_switch_control", 1), "810601210001063c"),
        (lambda: build_field_write_frame("active_balance_switch", 1), "81060119000187f1"),
        (lambda: build_field_write_frame("discharge_mos_switch_control", 1), "d20600a60001bb8a"),
    ],
)
def test_known_write_frames_exact_bytes(builder, expected_hex):
    frame = builder()
    assert frame == bytes.fromhex(expected_hex)
    assert len(frame) == WRITE_FRAME_LEN
    assert frame[0] in (PROTOCOL_UNIT_D2, PROTOCOL_UNIT_81)


def test_modbus_crc_append_low_byte_first():
    body = bytes.fromhex("d20600800c1c")
    framed = modbus_crc_append(body)
    assert framed == bytes.fromhex("d20600800c1c9f48")


def test_daly_full_protocol_imports_without_aiobmsble(block_aiobmsble_imports):
    mod = importlib.import_module("bmslib.bms_ble.plugins.daly_full_protocol")
    assert mod.build_d2_read_frame(0x80, 0x50) == bytes.fromhex("d2030080005057bd")
    assert mod.build_d2_read_frame(0xD0, 0x1E) == bytes.fromhex("d20300d0001ed798")
    assert mod.build_d2_write_frame(0x0080, 3100) == bytes.fromhex("d20600800c1c9f48")
    assert "aiobmsble" not in mod.__dict__
