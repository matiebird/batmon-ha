"""Daly D2 Modbus protocol helpers (read/write framing, CRC, echo validation)."""

from __future__ import annotations

from typing import Final

from aiobmsble.basebms import crc_modbus
from aiobmsble.bms.daly_bms import BMS as DalyBMS

PROTOCOL_UNIT: Final[int] = 0xD2
READ_FUNCTION: Final[int] = 0x03
WRITE_FUNCTION: Final[int] = 0x06
EXCEPTION_FUNCTION: Final[int] = 0x83
WRITE_FRAME_LEN: Final[int] = 8
RESTART_ADDRESS: Final[int] = 0x00F0


def modbus_crc_append(frame: bytes) -> bytes:
    return frame + crc_modbus(frame).to_bytes(2, "little")


def build_d2_read_frame(addr: int, count: int) -> bytes:
    return DalyBMS._cmd_modbus(dev_id=PROTOCOL_UNIT, fct=READ_FUNCTION, addr=addr, count=count)


def assert_allowlisted_write_address(addr: int) -> None:
    from bmslib.bms_ble.plugins.daly_full_write_registry import (
        ALLOWED_WRITE_ADDRESSES,
        EXCLUDED_WRITE_ADDRESSES,
    )

    if addr in EXCLUDED_WRITE_ADDRESSES:
        raise ValueError("excluded write address 0x%04X" % addr)
    if addr not in ALLOWED_WRITE_ADDRESSES:
        raise ValueError("address 0x%04X is not allowlisted" % addr)


def _assemble_d2_write_frame(addr: int, raw_value: int) -> bytes:
    assert_allowlisted_write_address(addr)
    if not (0 <= addr <= 0xFFFF):
        raise ValueError("invalid D2 write address")
    if not (0 <= raw_value <= 0xFFFF):
        raise ValueError("invalid D2 write raw value")
    frame = (
        PROTOCOL_UNIT.to_bytes(1)
        + WRITE_FUNCTION.to_bytes(1)
        + addr.to_bytes(2, "big")
        + raw_value.to_bytes(2, "big")
    )
    return modbus_crc_append(frame)


def build_d2_write_frame(addr: int, raw_value: int) -> bytes:
    """Low-level frame builder for unit tests only; production uses field-key builders."""
    return _assemble_d2_write_frame(addr, raw_value)


def assert_valid_registry_write_frame(
    frame: bytes,
    *,
    field_key: str,
    raw_value: int,
) -> None:
    from bmslib.bms_ble.plugins.daly_full_write_registry import WRITE_FIELDS_BY_KEY

    if len(frame) != WRITE_FRAME_LEN:
        raise ValueError("invalid D2 write frame length")
    if frame[0] != PROTOCOL_UNIT or frame[1] != WRITE_FUNCTION:
        raise ValueError("invalid D2 write frame header")
    field = WRITE_FIELDS_BY_KEY[field_key]
    addr = int.from_bytes(frame[2:4], "big")
    value = int.from_bytes(frame[4:6], "big")
    if addr != field.address:
        raise ValueError("frame address mismatch for field %r" % field_key)
    if value != raw_value:
        raise ValueError("frame raw value mismatch for field %r" % field_key)
    if not check_crc(frame):
        raise ValueError("invalid D2 write frame CRC")
    expected = _assemble_d2_write_frame(field.address, raw_value)
    if frame != expected:
        raise ValueError("frame bytes mismatch for field %r" % field_key)


def build_field_write_frame(field_key: str, raw_value: int) -> bytes:
    from bmslib.bms_ble.plugins.daly_full_write_registry import WRITE_FIELDS_BY_KEY

    field = WRITE_FIELDS_BY_KEY.get(field_key)
    if field is None:
        raise ValueError("unknown writable field %r" % field_key)
    frame = _assemble_d2_write_frame(field.address, raw_value)
    assert_valid_registry_write_frame(frame, field_key=field_key, raw_value=raw_value)
    return frame


def assert_valid_restart_write_frame(frame: bytes) -> None:
    if len(frame) != WRITE_FRAME_LEN:
        raise ValueError("invalid restart frame length")
    if frame[0] != PROTOCOL_UNIT or frame[1] != WRITE_FUNCTION:
        raise ValueError("invalid restart frame header")
    addr = int.from_bytes(frame[2:4], "big")
    value = int.from_bytes(frame[4:6], "big")
    if addr != RESTART_ADDRESS or value != 0:
        raise ValueError("invalid restart frame address/value")
    if not check_crc(frame):
        raise ValueError("invalid restart frame CRC")
    expected = _assemble_d2_write_frame(RESTART_ADDRESS, 0)
    if frame != expected:
        raise ValueError("restart frame bytes mismatch")


def build_restart_write_frame() -> bytes:
    frame = _assemble_d2_write_frame(RESTART_ADDRESS, 0)
    assert_valid_restart_write_frame(frame)
    return frame


def check_crc(frame: bytes) -> bool:
    if len(frame) < 3:
        return False
    calc = crc_modbus(frame[:-2])
    expected = int.from_bytes(frame[-2:], byteorder="little")
    return calc == expected


def validate_d2_write_echo(frame: bytes, *, expected_frame: bytes) -> None:
    if len(frame) != WRITE_FRAME_LEN:
        raise ValueError("invalid D2 write echo length")
    if frame != expected_frame:
        raise ValueError("D2 write echo mismatch")
    if frame[0] != PROTOCOL_UNIT or frame[1] != WRITE_FUNCTION:
        raise ValueError("invalid D2 write echo header")
    if not check_crc(frame):
        raise ValueError("invalid D2 write echo CRC")
    if frame[2:6] != expected_frame[2:6]:
        raise ValueError("invalid D2 write echo address/value")


def parse_write_echo_address_value(frame: bytes) -> tuple[int, int]:
    validate_d2_write_echo(frame, expected_frame=frame)
    addr = int.from_bytes(frame[2:4], "big")
    value = int.from_bytes(frame[4:6], "big")
    return addr, value
