"""Read-only Daly Android-app Modbus RTU capability probe (Stage 2).

Evidence: DALY BMS com.daly.bms.app V3.3.0.3 decompiled at /tmp/daly-bms-app/jadx-out
(ConstantKt.java — unit 0x81 primary, D2 fallback on BLE FFF0/FFF1/FFF2).
"""
import struct
from typing import Callable, Optional

# Exact probe requests (including Modbus CRC16).
ANDROID_PROBE_REQUEST_81 = bytes.fromhex("8103003800011a07")
ANDROID_PROBE_REQUEST_D2 = bytes.fromhex("d203003800011664")

ANDROID_PROBE_UNITS = frozenset({0x81, 0xD2})
ANDROID_PROBE_REQUEST_BY_UNIT = {
    0x81: ANDROID_PROBE_REQUEST_81,
    0xD2: ANDROID_PROBE_REQUEST_D2,
}

MODBUS_PROBE_REQUEST_LEN = 8
MODBUS_PROBE_RESPONSE_LEN = 7
MODBUS_EXCEPTION_RESPONSE_LEN = 5
MIN_TOTAL_VOLTAGE_V = 10.0
MAX_TOTAL_VOLTAGE_V = 1000.0

A5_NOTIFY_FRAME_LEN = 13
A5_NOTIFY_HEADER = 0xA5
MAX_A5_RX_BUF = 32


class DalyAndroidProbeUnsupported(Exception):
    """Valid Modbus exception response (function 0x83)."""


class ModbusProbeMalformed(Exception):
    """Probe-era Modbus response is present but fail-closed unusable."""


def parse_enable_daly_android_protocol_probe(value, default: bool = False) -> bool:
    if value is None:
        return default
    if type(value) is not bool:
        raise TypeError("enable_daly_android_protocol_probe must be a bool")
    return value


def modbus_crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def modbus_crc_valid(frame: bytes) -> bool:
    if len(frame) < 2:
        return False
    expected = modbus_crc16(frame[:-2])
    received = frame[-2] | (frame[-1] << 8)
    return expected == received


def validate_android_probe_request_bytes(request: bytes, unit: int) -> None:
    """Structurally verify an immutable Stage-2 probe request before wire egress."""
    if len(request) != MODBUS_PROBE_REQUEST_LEN:
        raise ValueError("Daly Android probe request must be 8 bytes")
    if request[0] != unit:
        raise ValueError("Daly Android probe request unit mismatch")
    if request[1] != 0x03:
        raise ValueError("Daly Android probe request function invalid")
    if request[2:6] != bytes([0x00, 0x38, 0x00, 0x01]):
        raise ValueError("Daly Android probe request register/count invalid")
    if not modbus_crc_valid(request):
        raise ValueError("Daly Android probe request CRC invalid")


def android_probe_request_bytes(unit: int) -> bytes:
    """Return the sole allowed function-03 register-0x0038 probe request for ``unit``."""
    if unit not in ANDROID_PROBE_UNITS:
        raise ValueError("Daly Android probe unit 0x%02x not allowed" % unit)
    request = ANDROID_PROBE_REQUEST_BY_UNIT[unit]
    validate_android_probe_request_bytes(request, unit)
    return request


def parse_android_probe_response(frame, expected_unit: int) -> dict:
    """Parse 7-byte success or 5-byte Modbus exception frame."""
    if type(frame) is bool or isinstance(frame, int):
        raise TypeError("Daly Android probe frame must be bytes-like")
    if isinstance(frame, (bytes, bytearray, memoryview)):
        frame = bytes(frame)
    else:
        raise TypeError("Daly Android probe frame must be bytes-like")

    if len(frame) == MODBUS_EXCEPTION_RESPONSE_LEN:
        unit, func, _exc = frame[0], frame[1], frame[2]
        if unit != expected_unit:
            raise ValueError("Daly Android probe unit mismatch")
        if func != 0x83:
            raise ValueError("Daly Android probe exception function invalid")
        if not modbus_crc_valid(frame):
            raise ValueError("Daly Android probe CRC invalid")
        raise DalyAndroidProbeUnsupported("Daly Android probe Modbus exception")

    if len(frame) != MODBUS_PROBE_RESPONSE_LEN:
        raise ValueError("Daly Android probe response must be 7 bytes")

    unit, func, byte_count = frame[0], frame[1], frame[2]
    if unit != expected_unit:
        raise ValueError("Daly Android probe unit mismatch")
    if func != 0x03:
        raise ValueError("Daly Android probe function invalid")
    if byte_count != 0x02:
        raise ValueError("Daly Android probe byte count invalid")
    if not modbus_crc_valid(frame):
        raise ValueError("Daly Android probe CRC invalid")

    raw = struct.unpack(">H", frame[3:5])[0]
    voltage = raw / 10.0
    if voltage < MIN_TOTAL_VOLTAGE_V or voltage > MAX_TOTAL_VOLTAGE_V:
        raise ValueError("Daly Android probe voltage out of range")
    return {"android_protocol_voltage": voltage}


def try_extract_modbus_probe_frame(buf: bytearray, expected_unit: int) -> Optional[bytes]:
    """Extract one complete probe response from the start of ``buf`` only.

  Returns ``None`` while the frame is still incomplete.  Raises
  ``ModbusProbeMalformed`` when the buffered bytes are definitively unusable.
    """
    if not buf:
        return None
    if buf[0] != expected_unit:
        raise ModbusProbeMalformed("unexpected probe response unit")

    if len(buf) == 1:
        return None

    func = buf[1]
    if func == 0x83:
        if len(buf) < MODBUS_EXCEPTION_RESPONSE_LEN:
            return None
        candidate = bytes(buf[:MODBUS_EXCEPTION_RESPONSE_LEN])
        if modbus_crc_valid(candidate):
            del buf[:MODBUS_EXCEPTION_RESPONSE_LEN]
            return candidate
        raise ModbusProbeMalformed("invalid exception CRC")

    if func == 0x03:
        if len(buf) < 3:
            return None
        if buf[2] != 0x02:
            raise ModbusProbeMalformed("invalid probe byte count")
        if len(buf) < MODBUS_PROBE_RESPONSE_LEN:
            return None
        candidate = bytes(buf[:MODBUS_PROBE_RESPONSE_LEN])
        if modbus_crc_valid(candidate):
            del buf[:MODBUS_PROBE_RESPONSE_LEN]
            return candidate
        raise ModbusProbeMalformed("invalid success CRC")

    raise ModbusProbeMalformed("invalid probe function")


def drain_a5_notify_buf(
    buf: bytearray,
    a5_crc_valid: Callable[[bytes], bool],
    on_a5_frame: Callable[[bytes], None],
    max_buf: int = MAX_A5_RX_BUF,
) -> None:
    """Extract complete CRC-valid A5 frames from a persistent assembly buffer."""
    while len(buf) >= A5_NOTIFY_FRAME_LEN:
        if buf[0] != A5_NOTIFY_HEADER:
            try:
                next_hdr = buf.index(A5_NOTIFY_HEADER, 1)
                del buf[:next_hdr]
            except ValueError:
                buf.clear()
                break
            continue

        frame = bytes(buf[:A5_NOTIFY_FRAME_LEN])
        if a5_crc_valid(frame):
            on_a5_frame(frame)
            del buf[:A5_NOTIFY_FRAME_LEN]
            continue
        del buf[0]

    if len(buf) > max_buf:
        del buf[:-min(A5_NOTIFY_FRAME_LEN - 1, max_buf)]


def modbus_probe_future_key(unit: int) -> str:
    return f"modbus_probe_{unit}"
