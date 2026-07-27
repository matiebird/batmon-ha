"""Stage 2: read-only Daly Android-app Modbus capability probe (unit 0x81 / D2)."""

import asyncio
import struct
import time
from unittest.mock import MagicMock

import pytest

from bmslib.bms import BmsSample, MIN_VALUE_EXPIRY
from bmslib import FuturesPool
from bmslib.models.daly import DalyBt, daly_command_message
from bmslib.models.daly_android_probe import (
    ANDROID_PROBE_REQUEST_81,
    ANDROID_PROBE_REQUEST_D2,
    A5_NOTIFY_FRAME_LEN,
    DalyAndroidProbeUnsupported,
    MAX_A5_RX_BUF,
    ModbusProbeMalformed,
    android_probe_request_bytes,
    modbus_crc16,
    modbus_probe_future_key,
    parse_android_probe_response,
    parse_enable_daly_android_protocol_probe,
    try_extract_modbus_probe_frame,
)
from bmslib.models.daly_uart import DalyUart
from bmslib.mqtt_util import publish_hass_discovery, sample_desc
from bmslib.test.data import daly_fixtures
from bmslib.test.data.daly_fixtures import wrap_ble_response

NORMAL_TELEMETRY_CMDS = frozenset({0x90, 0x93, 0x94})
EXTENDED_A5_DIAG_CMDS = frozenset({0x50, 0x53, 0x62, 0x63})
MODBUS_PROBE_REQUESTS = frozenset({ANDROID_PROBE_REQUEST_81, ANDROID_PROBE_REQUEST_D2})
ADVERSARIAL_FUNCTION_06_81 = bytes.fromhex("810600380001d607")


def _notify_test_bms(modbus_unit=None):
    bms = _make_daly()
    received = {}

    class _TrackingFuturesPool(FuturesPool):
        def set_result(self, key, value):
            received[key] = value
            super().set_result(key, value)

    bms._fetch_nr = {}
    bms._fetch_futures = _TrackingFuturesPool()
    bms._a5_rx_buf = bytearray()
    bms._modbus_rx_buf = bytearray()
    if modbus_unit is not None:
        bms._modbus_pending = {
            "unit": modbus_unit,
            "key": modbus_probe_future_key(modbus_unit),
        }
    else:
        bms._modbus_pending = None
    return bms, received


def _a5_status_frame():
    return wrap_ble_response(0x93, daly_fixtures.STATUS_DSG_ON["raw"])


def _a5_soc_frame():
    return wrap_ble_response(0x90, daly_fixtures.SOC_SYNTHETIC_265V_5A["raw"])


def _a5_states_frame():
    return wrap_ble_response(0x94, daly_fixtures.STATES_8CELL["raw"])


def _make_daly(enable_diagnostics=False, enable_android_probe=False):
    return DalyBt(
        "00:11:22:33:44:55",
        name="daly",
        enable_daly_diagnostics=enable_diagnostics,
        enable_daly_android_protocol_probe=enable_android_probe,
    )


def _modbus_success(unit: int, raw_u16: int) -> bytes:
    body = bytes([unit, 0x03, 0x02]) + struct.pack(">H", raw_u16)
    crc = modbus_crc16(body)
    return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def _modbus_exception(unit: int, exc: int = 0x01) -> bytes:
    body = bytes([unit, 0x83, exc])
    crc = modbus_crc16(body)
    return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def _normal_q_responses(cmd, num_responses=1):
    if cmd == 0x90:
        return daly_fixtures.SOC_SYNTHETIC_265V_5A["raw"]
    if cmd == 0x93:
        return daly_fixtures.STATUS_DSG_ON["raw"]
    if cmd == 0x94:
        return daly_fixtures.STATES_8CELL["raw"]
    raise AssertionError(f"unexpected cmd 0x{cmd:02x}")


def test_probe_request_bytes_exact():
    assert ANDROID_PROBE_REQUEST_81 == bytes.fromhex("8103003800011a07")
    assert ANDROID_PROBE_REQUEST_D2 == bytes.fromhex("d203003800011664")


def test_parse_success_bytearray():
    frame = bytearray(_modbus_success(0x81, 528))  # 52.8 V
    got = parse_android_probe_response(frame, 0x81)
    assert got["android_protocol_voltage"] == pytest.approx(52.8, abs=0.01)


def test_parse_success_memoryview():
    frame = memoryview(_modbus_success(0xD2, 3100))
    got = parse_android_probe_response(frame, 0xD2)
    assert got["android_protocol_voltage"] == pytest.approx(310.0, abs=0.01)


def test_parse_rejects_wrong_unit():
    frame = _modbus_success(0x81, 500)
    with pytest.raises(ValueError, match="unit mismatch"):
        parse_android_probe_response(frame, 0xD2)


def test_parse_rejects_wrong_function():
    body = bytes([0x81, 0x04, 0x02, 0x01, 0x00])
    crc = modbus_crc16(body)
    frame = body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    with pytest.raises(ValueError, match="function invalid"):
        parse_android_probe_response(frame, 0x81)


def test_parse_rejects_wrong_byte_count():
    body = bytes([0x81, 0x03, 0x01, 0x02, 0x08])
    crc = modbus_crc16(body)
    frame = body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    with pytest.raises(ValueError, match="byte count invalid"):
        parse_android_probe_response(frame, 0x81)


def test_parse_rejects_bad_crc():
    frame = bytearray(_modbus_success(0x81, 500))
    frame[-1] ^= 0xFF
    with pytest.raises(ValueError, match="CRC invalid"):
        parse_android_probe_response(bytes(frame), 0x81)


def test_parse_rejects_short_frame():
    with pytest.raises(ValueError, match="must be 7 bytes"):
        parse_android_probe_response(bytes([0x81, 0x03, 0x02]), 0x81)


def test_parse_rejects_voltage_out_of_range():
    frame = _modbus_success(0x81, 50)  # 5.0 V
    with pytest.raises(ValueError, match="voltage out of range"):
        parse_android_probe_response(frame, 0x81)


def test_parse_modbus_exception_unsupported():
    frame = _modbus_exception(0x81)
    with pytest.raises(DalyAndroidProbeUnsupported):
        parse_android_probe_response(frame, 0x81)


def test_parse_rejects_coercive_inputs():
    with pytest.raises(TypeError):
        parse_android_probe_response(True, 0x81)
    with pytest.raises(TypeError):
        parse_android_probe_response([0x81], 0x81)


def test_extract_fragmented_notification():
    buf = bytearray()
    full = _modbus_success(0x81, 520)
    chunks = (full[:3], full[3:5], full[5:])
    got = None
    for i, chunk in enumerate(chunks):
        buf.extend(chunk)
        got = try_extract_modbus_probe_frame(buf, 0x81)
        if i < len(chunks) - 1:
            assert got is None
    assert got == full
    assert not buf


def test_extract_wrong_unit_at_head_malformed():
    buf = bytearray(_modbus_success(0xD2, 520))
    with pytest.raises(ModbusProbeMalformed):
        try_extract_modbus_probe_frame(buf, 0x81)


def test_extract_bad_crc_at_head_malformed():
    bad = bytearray(_modbus_success(0x81, 520))
    bad[-1] ^= 0xFF
    with pytest.raises(ModbusProbeMalformed):
        try_extract_modbus_probe_frame(bad, 0x81)


@pytest.mark.parametrize("unit", [0x81, 0xD2])
def test_extract_single_byte_prefix_waits(unit):
    buf = bytearray([unit])
    assert try_extract_modbus_probe_frame(buf, unit) is None
    assert buf == bytearray([unit])


@pytest.mark.parametrize("unit", [0x81, 0xD2])
def test_extract_unit_func03_two_bytes_waits_without_index_error(unit):
    buf = bytearray([unit, 0x03])
    assert try_extract_modbus_probe_frame(buf, unit) is None
    assert buf == bytearray([unit, 0x03])


@pytest.mark.parametrize("unit", [0x81, 0xD2])
def test_extract_success_frame_byte_by_byte(unit):
    full = _modbus_success(unit, 520)
    buf = bytearray()
    got = None
    for i, b in enumerate(full):
        buf.append(b)
        got = try_extract_modbus_probe_frame(buf, unit)
        if i < len(full) - 1:
            assert got is None
    assert got == full
    assert not buf


@pytest.mark.parametrize("unit", [0x81, 0xD2])
def test_extract_exception_frame_byte_by_byte(unit):
    full = _modbus_exception(unit)
    buf = bytearray()
    got = None
    for i, b in enumerate(full):
        buf.append(b)
        got = try_extract_modbus_probe_frame(buf, unit)
        if i < len(full) - 1:
            assert got is None
    assert got == full
    assert not buf


@pytest.mark.parametrize("unit", [0x81, 0xD2])
def test_extract_success_frame_every_split_boundary(unit):
    full = _modbus_success(unit, 520)
    for split in range(1, len(full)):
        buf = bytearray(full[:split])
        assert try_extract_modbus_probe_frame(buf, unit) is None
        buf.extend(full[split:])
        got = try_extract_modbus_probe_frame(buf, unit)
        assert got == full
        assert not buf


@pytest.mark.parametrize("unit", [0x81, 0xD2])
def test_extract_exception_frame_every_split_boundary(unit):
    full = _modbus_exception(unit)
    for split in range(1, len(full)):
        buf = bytearray(full[:split])
        assert try_extract_modbus_probe_frame(buf, unit) is None
        buf.extend(full[split:])
        got = try_extract_modbus_probe_frame(buf, unit)
        assert got == full
        assert not buf


def test_no_raw_modbus_exchange_api_in_source():
    import inspect
    from bmslib.models import daly as daly_mod
    source = inspect.getsource(daly_mod.DalyBt)
    assert "_modbus_exchange" not in source
    assert "_android_probe_unit_exchange" in source
    sig = inspect.signature(daly_mod.DalyBt._android_probe_unit_exchange)
    assert list(sig.parameters) == ["self", "unit"]


def test_adversarial_function06_not_in_allowlist():
    assert android_probe_request_bytes(0x81) == ANDROID_PROBE_REQUEST_81
    assert android_probe_request_bytes(0x81)[1] == 0x03
    assert ADVERSARIAL_FUNCTION_06_81 != ANDROID_PROBE_REQUEST_81


def test_adversarial_unit_rejected_before_gatt():
    bms = _make_daly()
    writes = []

    async def write_gatt_char(tx, msg):
        writes.append(bytes(msg))

    bms.UUID_TX = "fff2"
    bms.client = MagicMock()
    bms.client.write_gatt_char = write_gatt_char

    with pytest.raises(ValueError, match="not allowed"):
        asyncio.run(bms._android_probe_unit_exchange(0x01))
    assert writes == []


def test_android_probe_request_bytes_rejects_wrong_unit():
    with pytest.raises(ValueError, match="not allowed"):
        android_probe_request_bytes(0x50)


def test_demux_late_modbus_then_a5_after_probe():
    bms, received = _notify_test_bms(modbus_unit=None)
    modbus = _modbus_success(0x81, 520)
    a5 = _a5_status_frame()
    bms._notification_callback(None, bytearray(modbus) + bytearray(a5))
    assert received[0x93] == daly_fixtures.STATUS_DSG_ON["raw"]
    assert modbus_probe_future_key(0x81) not in received


def test_modbus_pending_trailing_a5_malformed():
    bms, received = _notify_test_bms(modbus_unit=0x81)
    modbus = _modbus_success(0x81, 520)
    a5 = _a5_status_frame()
    bms._notification_callback(None, bytearray(modbus) + bytearray(a5))
    assert modbus_probe_future_key(0x81) not in received
    assert bms._modbus_probe_malformed
    assert 0x93 not in received


def test_probe_notify_trailing_second_success_frame_malformed():
    bms, received = _notify_test_bms(modbus_unit=0x81)
    payload = _modbus_success(0x81, 520) + _modbus_success(0x81, 528)
    bms._notification_callback(None, bytearray(payload))
    assert modbus_probe_future_key(0x81) not in received
    assert bms._modbus_probe_malformed


def test_probe_notify_trailing_exception_then_success_malformed():
    bms, received = _notify_test_bms(modbus_unit=0x81)
    payload = _modbus_exception(0x81) + _modbus_success(0x81, 528)
    bms._notification_callback(None, bytearray(payload))
    assert modbus_probe_future_key(0x81) not in received
    assert bms._modbus_probe_malformed


@pytest.mark.parametrize(
    "frame, trailing",
    [
        (_modbus_success(0x81, 520), b"\xff"),
        (_modbus_exception(0x81), b"\xaa"),
    ],
)
def test_probe_notify_trailing_byte_malformed(frame, trailing):
    bms, received = _notify_test_bms(modbus_unit=0x81)
    bms._notification_callback(None, bytearray(frame) + bytearray(trailing))
    assert modbus_probe_future_key(0x81) not in received
    assert bms._modbus_probe_malformed


async def _run_probe_exchange_expect_malformed(bms, payload):
    async def write_gatt_char(tx, msg):
        bms._notification_callback(None, bytearray(payload))

    bms.UUID_TX = "fff2"
    bms.client = MagicMock()
    bms.client.write_gatt_char = write_gatt_char
    bms.TIMEOUT = 0.05
    with pytest.raises(ValueError, match="malformed"):
        await bms._android_probe_unit_exchange(0x81)


def test_probe_exchange_trailing_second_success_malformed():
    payload = _modbus_success(0x81, 520) + _modbus_success(0x81, 528)
    asyncio.run(_run_probe_exchange_expect_malformed(_make_daly(), payload))


def test_probe_exchange_trailing_exception_then_success_malformed():
    payload = _modbus_exception(0x81) + _modbus_success(0x81, 528)
    asyncio.run(_run_probe_exchange_expect_malformed(_make_daly(), payload))


def test_probe_81_exception_plus_success_no_d2():
    bms = _make_daly(enable_android_probe=True)
    writes = []
    payload = _modbus_exception(0x81) + _modbus_success(0x81, 528)

    async def write_gatt_char(tx, msg):
        writes.append(bytes(msg))
        if msg == ANDROID_PROBE_REQUEST_81:
            bms._notification_callback(None, bytearray(payload))

    _probe_fetch_setup(bms, write_gatt_char)
    asyncio.run(bms.fetch())
    assert writes == [ANDROID_PROBE_REQUEST_81]


@pytest.mark.parametrize("unit", [0x81, 0xD2])
@pytest.mark.parametrize("kind", ["success", "exception"])
def test_modbus_pending_fragmented_every_split_boundary(unit, kind):
    modbus = (
        _modbus_success(unit, 520)
        if kind == "success"
        else _modbus_exception(unit)
    )
    for split in range(1, len(modbus)):
        bms, received = _notify_test_bms(modbus_unit=unit)
        for chunk in (modbus[:split], modbus[split:]):
            bms._notification_callback(None, bytearray(chunk))
        assert received[modbus_probe_future_key(unit)] == modbus
        assert not bms._modbus_rx_buf


def test_multi_a5_responses_outside_probe():
    bms, received = _notify_test_bms(modbus_unit=None)
    bms._notification_callback(None, bytearray(_a5_status_frame()) + bytearray(_a5_soc_frame()))
    assert received[0x93] == daly_fixtures.STATUS_DSG_ON["raw"]
    assert received[0x90] == daly_fixtures.SOC_SYNTHETIC_265V_5A["raw"]


def test_three_coalesced_a5_frames_resolve_before_noise_cap():
    bms, received = _notify_test_bms(modbus_unit=None)
    frames = _a5_status_frame() + _a5_soc_frame() + _a5_states_frame()
    noise = bytes([0x44] * MAX_A5_RX_BUF)
    bms._notification_callback(None, bytearray(frames) + noise)
    assert received[0x93] == daly_fixtures.STATUS_DSG_ON["raw"]
    assert received[0x90] == daly_fixtures.SOC_SYNTHETIC_265V_5A["raw"]
    assert received[0x94] == daly_fixtures.STATES_8CELL["raw"]
    assert len(bms._a5_rx_buf) <= MAX_A5_RX_BUF


@pytest.mark.parametrize(
    "noise_len",
    [
        MAX_A5_RX_BUF - A5_NOTIFY_FRAME_LEN,
        MAX_A5_RX_BUF - A5_NOTIFY_FRAME_LEN + 1,
        MAX_A5_RX_BUF,
    ],
)
def test_a5_leading_frame_parsed_before_noise_cap_outside_probe(noise_len):
    bms, received = _notify_test_bms(modbus_unit=None)
    a5 = _a5_status_frame()
    noise = bytes([0x44] * noise_len)
    bms._notification_callback(None, bytearray(a5) + noise)
    assert received[0x93] == daly_fixtures.STATUS_DSG_ON["raw"]
    assert len(bms._a5_rx_buf) <= MAX_A5_RX_BUF
    if noise_len > 0:
        assert all(b == 0x44 for b in bms._a5_rx_buf)


def _probe_fetch_setup(bms, writes):
    async def fake_q(cmd, num_responses=1):
        return _normal_q_responses(cmd, num_responses)

    bms.UUID_TX = "fff2"
    bms.client = MagicMock()
    bms.client.write_gatt_char = writes
    bms._q = fake_q
    bms.TIMEOUT = 0.05


def test_probe_81_bad_crc_negative_cache_no_d2():
    bms = _make_daly(enable_android_probe=True)
    writes = []
    bad = bytearray(_modbus_success(0x81, 520))
    bad[-1] ^= 0xFF

    async def write_gatt_char(tx, msg):
        writes.append(bytes(msg))
        if msg == ANDROID_PROBE_REQUEST_81:
            bms._notification_callback(None, bad)

    _probe_fetch_setup(bms, write_gatt_char)
    asyncio.run(bms.fetch())
    assert writes == [ANDROID_PROBE_REQUEST_81]


def test_probe_81_voltage_out_of_range_negative_cache_no_d2():
    bms = _make_daly(enable_android_probe=True)
    writes = []

    async def write_gatt_char(tx, msg):
        writes.append(bytes(msg))
        if msg == ANDROID_PROBE_REQUEST_81:
            bms._notification_callback(None, bytearray(_modbus_success(0x81, 50)))

    _probe_fetch_setup(bms, write_gatt_char)
    asyncio.run(bms.fetch())
    assert writes == [ANDROID_PROBE_REQUEST_81]


def test_probe_81_wrong_function_response_negative_cache_no_d2():
    bms = _make_daly(enable_android_probe=True)
    writes = []
    body = bytes([0x81, 0x04, 0x02, 0x02, 0x08])
    crc = modbus_crc16(body)
    frame = body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])

    async def write_gatt_char(tx, msg):
        writes.append(bytes(msg))
        if msg == ANDROID_PROBE_REQUEST_81:
            bms._notification_callback(None, bytearray(frame))

    _probe_fetch_setup(bms, write_gatt_char)
    asyncio.run(bms.fetch())
    assert writes == [ANDROID_PROBE_REQUEST_81]


def test_probe_81_cancelled_error_propagates():
    bms = _make_daly(enable_android_probe=True)
    writes = []

    async def write_gatt_char(tx, msg):
        writes.append(bytes(msg))
        if msg == ANDROID_PROBE_REQUEST_81:
            raise asyncio.CancelledError()

    _probe_fetch_setup(bms, write_gatt_char)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bms._probe_android_protocol())
    assert ANDROID_PROBE_REQUEST_D2 not in writes


def _exchange_test_bms():
    bms = _make_daly()
    received = {}

    class _TrackingFuturesPool(FuturesPool):
        def set_result(self, key, value):
            received[key] = value
            super().set_result(key, value)

    bms._fetch_futures = _TrackingFuturesPool()
    bms._fetch_nr = {}
    bms._a5_rx_buf = bytearray()
    bms._modbus_rx_buf = bytearray()
    bms._modbus_pending = None
    bms.TIMEOUT = 0.05
    return bms, received


def test_a5_before_probe_and_after_cleanup():
    bms, received = _exchange_test_bms()
    modbus = _modbus_success(0x81, 520)
    a5 = _a5_status_frame()

    async def run():
        bms._notification_callback(None, bytearray(a5))

        async def write_gatt_char(tx, msg):
            bms._notification_callback(None, bytearray(modbus))

        bms.UUID_TX = "fff2"
        bms.client = MagicMock()
        bms.client.write_gatt_char = write_gatt_char
        await bms._android_probe_unit_exchange(0x81)
        bms._notification_callback(None, bytearray(_a5_soc_frame()))

    asyncio.run(run())
    assert received[0x93] == daly_fixtures.STATUS_DSG_ON["raw"]
    assert received[0x90] == daly_fixtures.SOC_SYNTHETIC_265V_5A["raw"]


def test_wire_lock_q_blocked_while_probe_holds_lock():
    bms = _make_daly()
    writes = []
    probe_gate = asyncio.Event()
    modbus = _modbus_success(0x81, 520)

    async def write_gatt_char(tx, msg):
        writes.append(bytes(msg))
        if msg == ANDROID_PROBE_REQUEST_81:
            probe_gate.set()
            await asyncio.sleep(0.15)
            bms._notification_callback(None, bytearray(modbus))
        elif msg[0] == 0xA5:
            bms._notification_callback(None, bytearray(_a5_status_frame()))

    bms.UUID_TX = "fff2"
    bms.client = MagicMock()
    bms.client.write_gatt_char = write_gatt_char
    bms.TIMEOUT = 0.05

    async def run():
        probe_task = asyncio.create_task(bms._android_probe_unit_exchange(0x81))
        await probe_gate.wait()
        q_task = asyncio.create_task(bms._q(0x93))
        await asyncio.sleep(0.05)
        assert ANDROID_PROBE_REQUEST_81 in writes
        assert not any(w[0] == 0xA5 for w in writes if len(w) >= 1)
        await probe_task
        await q_task

    asyncio.run(run())
    assert writes[0] == ANDROID_PROBE_REQUEST_81
    assert any(w[0] == 0xA5 for w in writes)


def test_wire_lock_probe_blocked_while_q_holds_lock():
    bms = _make_daly()
    writes = []
    q_gate = asyncio.Event()

    async def write_gatt_char(tx, msg):
        writes.append(bytes(msg))
        if msg[0] == 0xA5:
            q_gate.set()
            await asyncio.sleep(0.15)
            bms._notification_callback(None, bytearray(_a5_status_frame()))
        elif msg == ANDROID_PROBE_REQUEST_81:
            bms._notification_callback(None, bytearray(_modbus_success(0x81, 520)))

    bms.UUID_TX = "fff2"
    bms.client = MagicMock()
    bms.client.write_gatt_char = write_gatt_char
    bms.TIMEOUT = 0.05

    async def run():
        q_task = asyncio.create_task(bms._q(0x93))
        await q_gate.wait()
        probe_task = asyncio.create_task(bms._android_probe_unit_exchange(0x81))
        await asyncio.sleep(0.05)
        assert ANDROID_PROBE_REQUEST_81 not in writes
        await q_task
        await probe_task

    asyncio.run(run())
    assert writes.index(ANDROID_PROBE_REQUEST_81) > 0
    assert writes[0][0] == 0xA5


def test_cancel_during_81_wait_no_d2_write():
    bms = _make_daly(enable_android_probe=True)
    writes = []

    async def write_gatt_char(tx, msg):
        writes.append(bytes(msg))

    _probe_fetch_setup(bms, write_gatt_char)
    bms.TIMEOUT = 1.0

    async def run():
        task = asyncio.create_task(bms._probe_android_protocol())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert writes == [ANDROID_PROBE_REQUEST_81]


def test_cancel_waiting_for_wire_lock_no_writes():
    bms = _make_daly(enable_android_probe=True)
    writes = []
    lock_held = asyncio.Event()

    async def hold_lock():
        async with bms._wire_lock:
            lock_held.set()
            try:
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                pass

    async def write_gatt_char(tx, msg):
        writes.append(bytes(msg))

    bms.UUID_TX = "fff2"
    bms.client = MagicMock()
    bms.client.write_gatt_char = write_gatt_char

    async def run():
        holder = asyncio.create_task(hold_lock())
        await lock_held.wait()
        task = asyncio.create_task(bms._probe_android_protocol())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        holder.cancel()
        try:
            await holder
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert writes == []


def test_cancel_during_fragmented_81_response_no_d2_write():
    bms = _make_daly(enable_android_probe=True)
    writes = []
    modbus = _modbus_success(0x81, 520)

    async def write_gatt_char(tx, msg):
        writes.append(bytes(msg))
        bms._notification_callback(None, bytearray(modbus[:4]))
        await asyncio.sleep(0.2)

    _probe_fetch_setup(bms, write_gatt_char)
    bms.TIMEOUT = 1.0

    async def run():
        task = asyncio.create_task(bms._probe_android_protocol())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert writes == [ANDROID_PROBE_REQUEST_81]


def test_cancel_between_81_timeout_and_d2_propagates_without_completing_probe():
    bms = _make_daly(enable_android_probe=True)
    calls = []

    async def exchange(unit):
        calls.append(unit)
        if unit == 0x81:
            raise TimeoutError("timeout awaiting modbus probe unit=0x81")
        raise asyncio.CancelledError()

    bms._android_probe_unit_exchange = exchange

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bms._probe_android_protocol())
    assert calls == [0x81, 0xD2]


def test_a5_request_works_after_probe_cancel():
    bms = _make_daly(enable_android_probe=True)
    writes = []

    async def write_gatt_char(tx, msg):
        writes.append(bytes(msg))

    _probe_fetch_setup(bms, write_gatt_char)
    bms.TIMEOUT = 0.05

    async def cancel_probe():
        task = asyncio.create_task(bms._probe_android_protocol())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(cancel_probe())

    calls = []
    async def fake_q(cmd, num_responses=1):
        calls.append(cmd)
        return _normal_q_responses(cmd, num_responses)

    bms._q = fake_q
    asyncio.run(bms._q(0x93))
    assert calls == [0x93]


def test_set_switch_mos_write_waits_for_inflight_modbus_exchange():
    bms = _make_daly()
    writes = []
    gate = asyncio.Event()
    modbus_released = asyncio.Event()
    expected_d9 = bytes(daly_command_message(0xD9, extra="01", address=8))

    async def write_gatt_char(tx, msg):
        writes.append(bytes(msg))
        if msg == ANDROID_PROBE_REQUEST_81:
            gate.set()
            await asyncio.sleep(0.12)
            bms._notification_callback(None, bytearray(_modbus_success(0x81, 520)))
            modbus_released.set()

    async def fake_q(cmd, num_responses=1):
        return _normal_q_responses(cmd, num_responses)

    bms.UUID_TX = "fff2"
    bms.client = MagicMock()
    bms.client.write_gatt_char = write_gatt_char
    bms._q = fake_q
    bms.TIMEOUT = 0.05
    asyncio.run(bms._fetch_status())  # warm status cache like a normal fetch cycle

    async def run_modbus():
        await bms._android_probe_unit_exchange(0x81)

    async def run_switch():
        await gate.wait()
        await bms.set_switch("discharge", True)

    async def run_both():
        await asyncio.gather(run_modbus(), run_switch())

    asyncio.run(run_both())
    assert writes[0] == ANDROID_PROBE_REQUEST_81
    assert expected_d9 in writes
    assert modbus_released.is_set()


def test_android_probe_unit_exchange_via_notification_callback():
    bms = _make_daly()
    response = _modbus_success(0x81, 528)

    async def write_gatt_char(tx, msg):
        assert msg == ANDROID_PROBE_REQUEST_81
        bms._notification_callback(None, bytearray(response))

    bms.UUID_TX = "fff2"
    bms.client = MagicMock()
    bms.client.write_gatt_char = write_gatt_char
    frame = asyncio.run(bms._android_probe_unit_exchange(0x81))
    assert frame == response


def test_probe_81_success_suppresses_d2():
    bms = _make_daly(enable_android_probe=True)
    writes = []

    async def write_gatt_char(tx, msg):
        writes.append(bytes(msg))
        if msg == ANDROID_PROBE_REQUEST_81:
            bms._notification_callback(None, bytearray(_modbus_success(0x81, 520)))

    async def fake_q(cmd, num_responses=1):
        return _normal_q_responses(cmd, num_responses)

    bms.UUID_TX = "fff2"
    bms.client = MagicMock()
    bms.client.write_gatt_char = write_gatt_char
    bms._q = fake_q
    sample = asyncio.run(bms.fetch())
    assert ANDROID_PROBE_REQUEST_D2 not in writes
    assert sample.android_protocol_unit == "81"
    assert sample.android_protocol_voltage == pytest.approx(52.0, abs=0.01)


def test_probe_81_exception_then_d2_success():
    bms = _make_daly(enable_android_probe=True)
    writes = []

    async def write_gatt_char(tx, msg):
        writes.append(bytes(msg))
        if msg == ANDROID_PROBE_REQUEST_81:
            bms._notification_callback(None, bytearray(_modbus_exception(0x81)))
        elif msg == ANDROID_PROBE_REQUEST_D2:
            bms._notification_callback(None, bytearray(_modbus_success(0xD2, 528)))

    async def fake_q(cmd, num_responses=1):
        return _normal_q_responses(cmd, num_responses)

    bms.UUID_TX = "fff2"
    bms.client = MagicMock()
    bms.client.write_gatt_char = write_gatt_char
    bms._q = fake_q
    sample = asyncio.run(bms.fetch())
    assert writes == [ANDROID_PROBE_REQUEST_81, ANDROID_PROBE_REQUEST_D2]
    assert sample.android_protocol_unit == "D2"
    assert sample.android_protocol_voltage == pytest.approx(52.8, abs=0.01)


def test_probe_disabled_preserves_a5_sequence():
    bms = _make_daly()
    calls = []

    async def fake_q(cmd, num_responses=1):
        calls.append(cmd)
        return _normal_q_responses(cmd, num_responses)

    async def write_gatt_char(tx, msg):
        raise AssertionError("modbus write when probe disabled")

    bms.UUID_TX = "fff2"
    bms.client = MagicMock()
    bms.client.write_gatt_char = write_gatt_char
    bms._q = fake_q
    asyncio.run(bms.fetch())
    assert set(calls) <= NORMAL_TELEMETRY_CMDS


def test_probe_runs_before_final_normal_telemetry():
    bms = _make_daly(enable_android_probe=True)
    order = []

    async def write_gatt_char(tx, msg):
        order.append(("modbus", bytes(msg)))
        if msg == ANDROID_PROBE_REQUEST_81:
            bms._notification_callback(None, bytearray(_modbus_success(0x81, 520)))

    async def fake_q(cmd, num_responses=1):
        order.append(("a5", cmd))
        return _normal_q_responses(cmd, num_responses)

    bms.UUID_TX = "fff2"
    bms.client = MagicMock()
    bms.client.write_gatt_char = write_gatt_char
    bms._q = fake_q
    asyncio.run(bms.fetch())
    modbus_idx = next(i for i, t in enumerate(order) if t[0] == "modbus")
    tele_indices = [i for i, t in enumerate(order) if t == ("a5", 0x93) or t == ("a5", 0x90)]
    assert tele_indices
    assert modbus_idx < min(tele_indices)


def test_probe_timeouts_cached_no_repeat_modbus_writes():
    bms = _make_daly(enable_android_probe=True)
    writes = []

    async def write_gatt_char(tx, msg):
        writes.append(bytes(msg))

    async def fake_q(cmd, num_responses=1):
        if cmd in EXTENDED_A5_DIAG_CMDS:
            raise AssertionError("unexpected extended A5 when only android probe enabled")
        return _normal_q_responses(cmd, num_responses)

    bms.UUID_TX = "fff2"
    bms.client = MagicMock()
    bms.client.write_gatt_char = write_gatt_char
    bms._q = fake_q
    bms.TIMEOUT = 0.05
    asyncio.run(bms.fetch())
    assert writes == [ANDROID_PROBE_REQUEST_81, ANDROID_PROBE_REQUEST_D2]
    writes.clear()
    asyncio.run(bms.fetch())
    assert writes == []


def test_probe_slow_timeouts_keep_sample_fresh():
    expire_after_seconds = 20
    delay = 0.2
    bms = _make_daly(enable_android_probe=True)

    async def write_gatt_char(tx, msg):
        await asyncio.sleep(delay)

    async def fake_q(cmd, num_responses=1):
        return _normal_q_responses(cmd, num_responses)

    bms.UUID_TX = "fff2"
    bms.client = MagicMock()
    bms.client.write_gatt_char = write_gatt_char
    bms._q = fake_q
    bms.TIMEOUT = 0.05
    sample = asyncio.run(bms.fetch())
    t_now = time.time()
    age = t_now - sample.timestamp
    assert sample.timestamp >= t_now - max(expire_after_seconds, MIN_VALUE_EXPIRY)
    assert age < delay + 0.15


def test_enable_android_probe_rejects_string():
    with pytest.raises(TypeError, match="enable_daly_android_protocol_probe must be a bool"):
        DalyBt("00:11:22:33:44:55", name="daly", enable_daly_android_protocol_probe="false")


def test_daly_uart_rejects_android_probe_enabled():
    with pytest.raises(ValueError, match="requires BLE type: daly"):
        DalyUart("serial", name="uart", enable_daly_android_protocol_probe=True)


def test_sample_desc_android_probe_read_only():
    assert "bms/android_protocol_unit" in sample_desc
    assert "bms/android_protocol_voltage" in sample_desc
    for key in ("bms/android_protocol_unit", "bms/android_protocol_voltage"):
        assert "command_topic" not in sample_desc[key]


def test_hass_discovery_android_probe_no_command_topic():
    sample = BmsSample(
        voltage=26.0,
        current=0.0,
        android_protocol_unit="81",
        android_protocol_voltage=52.0,
    )
    captured = {}

    class _MqttClient:
        def publish(self, topic, data, retain=False):
            captured[topic] = data
            return type("Info", (), {"rc": 0})()

    publish_hass_discovery(
        _MqttClient(),
        device_topic="bat/daly",
        expire_after_seconds=120,
        sample=sample,
        num_cells=0,
        temperatures=[],
    )
    for topic, payload in captured.items():
        if "android_protocol" in topic:
            assert "command_topic" not in payload
            assert '"command_topic"' not in payload
