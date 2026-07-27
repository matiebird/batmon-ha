"""Tests for daly_full_ble read-only D2 register map extension."""

from __future__ import annotations

import asyncio
import inspect
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from aiobmsble import BMSSample
from aiobmsble.basebms import crc_modbus
from aiobmsble.bms.daly_bms import BMS as AiobmsbleDalyBMS

from bmslib.bms_ble.plugins import daly_full_bms
from bmslib.bms_ble.plugins.daly_full_bms import (
    READOUT_BLOCKS,
    READOUT_CACHE_SECONDS,
    BMS as DalyFullBMS,
    build_d2_read_frame,
    parse_enable_daly_full_readout,
    validate_d2_read_response,
)
from bmslib.models import BLE_BMS_wrap, get_bms_model_class
from bmslib.models.BLE_BMS_wrap import BMS as BleWrapBMS


def _d2_response_payload(addr: int, count: int, fill: int = 0xAB) -> bytes:
    payload = bytes([fill] * (count * 2))
    body = bytes([0xD2, 0x03, count * 2]) + payload
    crc = crc_modbus(body).to_bytes(2, "little")
    return body + crc


# --- resolution / wiring ---


def test_daly_ble_resolution_unchanged():
    cls = get_bms_model_class("daly_ble")
    assert cls is not None
    assert cls.func == BleWrapBMS  # partial target
    assert cls.keywords["type"] == "daly_bms"
    assert cls.keywords["blebms_class"] is AiobmsbleDalyBMS


def test_daly_full_ble_resolves_to_subclass_plugin():
    cls = get_bms_model_class("daly_full_ble")
    assert cls is not None
    assert cls.keywords["type"] == "daly_full_bms"
    assert cls.keywords["blebms_class"] is DalyFullBMS
    assert issubclass(DalyFullBMS, AiobmsbleDalyBMS)


def test_daly_full_bms_module_has_no_write_or_raw_api():
    src = inspect.getsource(daly_full_bms)
    assert "_cmd_modbus" not in src.replace("DalyBMS._cmd_modbus", "")
    for token in ("fct=0x06", "fct=6", "fct=0x10", "fct=16", "async_update(raw=True)"):
        assert token not in src
    assert not hasattr(daly_full_bms, "send_raw")
    assert not hasattr(daly_full_bms, "write_register")


# --- exact boolean ---


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, True),
        (False, False),
        (None, False),
    ],
)
def test_parse_enable_daly_full_readout_accepts_exact_bool(value, expected):
    assert parse_enable_daly_full_readout(value) is expected


@pytest.mark.parametrize("value", ["true", "1", 1, "yes", 0])
def test_parse_enable_daly_full_readout_rejects_non_bool(value):
    with pytest.raises(ValueError, match="boolean"):
        parse_enable_daly_full_readout(value)


# --- immutable egress frames ---


def test_readout_blocks_immutable():
    assert READOUT_BLOCKS == (
        daly_full_bms.DalyReadoutBlock(0x80, 0x50),
        daly_full_bms.DalyReadoutBlock(0xD0, 0x1E),
    )
    with pytest.raises(Exception):
        READOUT_BLOCKS[0].addr = 0  # type: ignore[misc]


def test_build_d2_read_frame_exact_bytes():
    assert build_d2_read_frame(0x80, 0x50) == bytes.fromhex("d2030080005057bd")
    assert build_d2_read_frame(0xD0, 0x1E) == bytes.fromhex("d20300d0001ed798")


# --- framing / CRC validation ---


def test_validate_d2_read_response_accepts_good_frame():
    frame = _d2_response_payload(0x80, 0x50)
    payload = validate_d2_read_response(frame, addr=0x80, count=0x50)
    assert len(payload) == 0x50 * 2


def test_validate_d2_read_response_rejects_bad_unit():
    frame = _d2_response_payload(0x80, 0x50)
    bad = bytes([0xD3]) + frame[1:]
    with pytest.raises(ValueError, match="unit"):
        validate_d2_read_response(bad, addr=0x80, count=0x50)


def test_validate_d2_read_response_rejects_bad_function():
    frame = _d2_response_payload(0x80, 0x50)
    bad = frame[:1] + bytes([0x04]) + frame[2:]
    with pytest.raises(ValueError, match="function"):
        validate_d2_read_response(bad, addr=0x80, count=0x50)


def test_validate_d2_read_response_accepts_modbus_exception_83():
    body = bytes([0xD2, 0x83, 0x02])
    frame = body + crc_modbus(body).to_bytes(2, "little")
    with pytest.raises(daly_full_bms.DalyReadoutUnsupported):
        validate_d2_read_response(frame, addr=0x80, count=0x50)


def test_validate_d2_read_response_rejects_byte_count_mismatch():
    frame = _d2_response_payload(0x80, 0x50)
    bad = frame[:2] + bytes([0x10]) + frame[3:]
    with pytest.raises(ValueError, match="byte count"):
        validate_d2_read_response(bad, addr=0x80, count=0x50)


def test_validate_d2_read_response_rejects_length_and_crc():
    frame = _d2_response_payload(0x80, 0x50)
    with pytest.raises(ValueError, match="length"):
        validate_d2_read_response(frame[:-1], addr=0x80, count=0x50)
    bad_crc = frame[:-2] + b"\x00\x00"
    with pytest.raises(ValueError, match="CRC"):
        validate_d2_read_response(bad_crc, addr=0x80, count=0x50)


# --- async behavior: order, cache, fail-soft, cancellation ---


class _StubDalyFull(DalyFullBMS):
    def __init__(self, *args, enable_daly_full_readout=True, **kwargs):
        super().__init__(
            MagicMock(),
            keep_alive=True,
            enable_daly_full_readout=enable_daly_full_readout,
            **kwargs,
        )
        self.await_calls: list[bytes] = []
        self.super_update = AsyncMock(return_value={"voltage": 52.0, "cell_count": 4})
        self._msg = b""

    async def _await_msg(self, data: bytes, *args, **kwargs):
        self.await_calls.append(data)
        if data == build_d2_read_frame(0x80, 0x50):
            self._msg = _d2_response_payload(0x80, 0x50, fill=0x11)
        elif data == build_d2_read_frame(0xD0, 0x1E):
            self._msg = _d2_response_payload(0xD0, 0x1E, fill=0x22)
        else:
            raise AssertionError(f"unexpected frame {data.hex()}")
        self._msg_event.set()

    async def _async_update(self):
        if self._enable_daly_full_readout:
            await self._maybe_full_readout()
        return await self.super_update()


def test_readout_runs_before_normal_telemetry():
    bms = _StubDalyFull()
    asyncio.run(bms._async_update())
    assert len(bms.await_calls) == 2
    assert bms.await_calls[0] == build_d2_read_frame(0x80, 0x50)
    assert bms.await_calls[1] == build_d2_read_frame(0xD0, 0x1E)
    bms.super_update.assert_awaited_once()


def test_readout_skipped_when_disabled():
    bms = _StubDalyFull(enable_daly_full_readout=False)
    asyncio.run(bms._async_update())
    assert bms.await_calls == []
    bms.super_update.assert_awaited_once()


def test_readout_cached_for_one_hour(monkeypatch):
    bms = _StubDalyFull()
    t = [1000.0]
    monkeypatch.setattr(daly_full_bms.time, "time", lambda: t[0])

    asyncio.run(bms._maybe_full_readout())
    assert len(bms.await_calls) == 2
    ro = bms.diagnostic_readout
    assert ro is not None

    asyncio.run(bms._maybe_full_readout())
    assert len(bms.await_calls) == 2  # still cached

    t[0] += READOUT_CACHE_SECONDS
    asyncio.run(bms._maybe_full_readout())
    assert len(bms.await_calls) == 4


def test_readout_failure_cached_fail_soft(monkeypatch):
    bms = _StubDalyFull()
    t = [2000.0]
    monkeypatch.setattr(daly_full_bms.time, "time", lambda: t[0])
    calls = 0

    async def fail_all(data, *a, **k):
        nonlocal calls
        calls += 1
        raise TimeoutError("ble timeout")

    bms._await_msg = fail_all  # type: ignore[method-assign]
    asyncio.run(bms._maybe_full_readout())
    assert bms.diagnostic_readout is None
    assert bms.decoded_settings is None
    assert calls == 1

    asyncio.run(bms._maybe_full_readout())
    assert calls == 1

    t[0] += READOUT_CACHE_SECONDS
    asyncio.run(bms._maybe_full_readout())
    assert calls == 2


def test_readout_does_not_block_normal_on_failure():
    bms = _StubDalyFull()

    async def fail_all(data, *a, **k):
        raise TimeoutError("ble timeout")

    bms._await_msg = fail_all  # type: ignore[method-assign]
    sample = asyncio.run(bms._async_update())
    assert sample == {"voltage": 52.0, "cell_count": 4}
    bms.super_update.assert_awaited_once()


def test_readout_propagates_cancellation(monkeypatch):
    bms = _StubDalyFull()
    t = [3000.0]
    monkeypatch.setattr(daly_full_bms.time, "time", lambda: t[0])

    async def cancel(data, *a, **k):
        raise asyncio.CancelledError

    asyncio.run(bms._maybe_full_readout())
    assert bms.decoded_settings is not None
    cache_after_success = bms._readout_cache_until

    t[0] += READOUT_CACHE_SECONDS + 1
    bms._await_msg = cancel  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bms._maybe_full_readout())
    assert bms.decoded_settings is None
    assert bms.diagnostic_readout is None
    assert bms._readout_cache_until == cache_after_success


def test_diagnostic_readout_immutable_after_success():
    bms = _StubDalyFull()
    asyncio.run(bms._maybe_full_readout())
    ro = bms.diagnostic_readout
    assert ro is not None
    assert ro["protocol_unit"] == 0xD2
    assert ro["register_count"] == 0x50 + 0x1E
    assert "blocks" not in ro
    assert "998877" not in str(ro)


def _frame_from_payload(payload: bytes) -> bytes:
    body = bytes([0xD2, 0x03, len(payload)]) + payload
    return body + crc_modbus(body).to_bytes(2, "little")


def test_expired_failed_refresh_clears_stale_decoded_settings(monkeypatch):
    from bmslib.test.test_daly_full_decode import build_fixture_blocks

    b1, b2 = build_fixture_blocks()

    class _FixtureStub(_StubDalyFull):
        async def _await_msg(self, data, *a, **k):
            self.await_calls.append(data)
            if data == build_d2_read_frame(0x80, 0x50):
                self._msg = _frame_from_payload(b1[1])
            elif data == build_d2_read_frame(0xD0, 0x1E):
                self._msg = _frame_from_payload(b2[1])
            else:
                raise AssertionError(data.hex())
            self._msg_event.set()

    bms = _FixtureStub()
    t = [4000.0]
    monkeypatch.setattr(daly_full_bms.time, "time", lambda: t[0])
    asyncio.run(bms._maybe_full_readout())
    assert bms.decoded_settings is not None
    assert bms.decoded_settings.values["rated_capacity_ah"] == 310.0

    t[0] += READOUT_CACHE_SECONDS + 1

    async def fail_second(data, *a, **k):
        raise TimeoutError("timeout")

    bms._await_msg = fail_second  # type: ignore[method-assign]
    asyncio.run(bms._maybe_full_readout())
    assert bms.decoded_settings is None
    assert bms.diagnostic_readout is None


def test_notification_handler_never_logs_password_payload(caplog):
    import logging
    from bmslib.test.test_daly_full_decode import build_fixture_blocks

    payload = build_fixture_blocks()[0][1]
    frame = _frame_from_payload(payload)
    bms = DalyFullBMS(MagicMock(), keep_alive=True)
    bms._log.setLevel(logging.DEBUG)
    with caplog.at_level(logging.DEBUG, logger=bms._log.name):
        bms._notification_handler(None, bytearray(frame))

    assert "998877" not in caplog.text
    assert payload.hex() not in caplog.text
    assert "len=%d" % len(frame) in caplog.text or "len=" in caplog.text
    assert bms._msg == frame


def test_daly_full_wrap_sample_has_readonly_switches():
    wrap = BleWrapBMS(
        "AA:BB:CC:DD:EE:FF",
        type="daly_full_bms",
        blebms_class=DalyFullBMS,
    )
    wrap.ble_bms = MagicMock()
    wrap.ble_bms.decoded_settings = None
    wrap.ble_bms.async_update = AsyncMock(
        return_value={
            "voltage": 52.0,
            "current": 0.0,
            "chrg_mosfet": True,
            "dischrg_mosfet": False,
        }
    )
    sample = asyncio.run(wrap.fetch())
    assert sample.switches == {"charge": True, "discharge": False}
    assert sample.switches_writable is False
    assert sample.extra_values is None


# --- BLE wrapper: one client, kwargs pass-through, telemetry mapping regression ---


def test_ble_wrap_telemetry_mapping_identical_for_daly_full():
    raw: BMSSample = {
        "battery_level": 80.0,
        "voltage": 52.1,
        "current": 10.5,
        "power": 547.0,
        "cycle_charge": 100.0,
        "design_capacity": 280.0,
        "cycles": 42,
        "chrg_mosfet": True,
        "dischrg_mosfet": False,
        "problem_code": 0,
        "cell_voltages": [3.25, 3.26, 3.27, 3.28],
        "cell_count": 4,
    }

    async def _run_for(ble_cls, type_name):
        wrap = BleWrapBMS("AA:BB:CC:DD:EE:FF", type=type_name, blebms_class=ble_cls)
        wrap.ble_bms = MagicMock()
        wrap.ble_bms.async_update = AsyncMock(return_value=raw)
        return await wrap.fetch()

    for ble_cls, type_name in (
        (AiobmsbleDalyBMS, "daly_bms"),
        (DalyFullBMS, "daly_full_bms"),
    ):
        got = asyncio.run(_run_for(ble_cls, type_name))
        assert got.soc == 80.0
        assert got.voltage == 52.1
        assert got.current == -10.5
        assert got.power == -547.0
        assert got.switches == {"charge": True, "discharge": False}
        assert got.num_cycles == 42


def test_ble_wrap_stores_enable_flag_for_plugin_connect():
    wrap = BleWrapBMS(
        "AA:BB:CC:DD:EE:FF",
        type="daly_full_bms",
        blebms_class=DalyFullBMS,
        enable_daly_full_readout=True,
        psk="1234",
        verbose_log=True,
    )
    assert wrap._enable_daly_full_readout is True


def test_connect_daly_ble_uses_real_aiobmsble_constructor_despite_legacy_kwargs(monkeypatch):
    async def fake_resolve(addr, adapter=None):
        return MagicMock(address=addr, name=addr)

    monkeypatch.setattr(BLE_BMS_wrap.BLEDeviceResolver, "resolve", staticmethod(fake_resolve))

    captured: list[dict] = []
    real_init = AiobmsbleDalyBMS.__init__

    def track_init(self, ble_device, keep_alive=True, secret="", logger_name=""):
        captured.append(
            {
                "keep_alive": keep_alive,
                "secret": secret,
                "logger_name": logger_name,
            }
        )
        return real_init(self, ble_device, keep_alive, secret, logger_name)

    monkeypatch.setattr(AiobmsbleDalyBMS, "__init__", track_init)

    async def noop_connect(self):
        self._client = MagicMock(is_connected=True)

    monkeypatch.setattr(AiobmsbleDalyBMS, "_connect", noop_connect)

    wrap = BleWrapBMS(
        "AA:BB:CC:DD:EE:FF",
        type="daly_bms",
        blebms_class=AiobmsbleDalyBMS,
        psk="1234",
        verbose_log=True,
        keep_alive=True,
    )
    asyncio.run(wrap.connect())

    assert len(captured) == 1
    assert captured[0] == {"keep_alive": True, "secret": "", "logger_name": ""}
    assert isinstance(wrap.ble_bms, AiobmsbleDalyBMS)


def test_connect_daly_full_ble_forwards_only_enable_boolean(monkeypatch):
    async def fake_resolve(addr, adapter=None):
        return MagicMock(address=addr, name=addr)

    monkeypatch.setattr(BLE_BMS_wrap.BLEDeviceResolver, "resolve", staticmethod(fake_resolve))

    captured: list[dict] = []
    real_init = DalyFullBMS.__init__

    def track_init(
        self,
        ble_device,
        keep_alive=True,
        secret="",
        logger_name="",
        enable_daly_full_readout=False,
    ):
        captured.append(
            {
                "keep_alive": keep_alive,
                "secret": secret,
                "logger_name": logger_name,
                "enable_daly_full_readout": enable_daly_full_readout,
            }
        )
        return real_init(
            self,
            ble_device,
            keep_alive,
            secret,
            logger_name,
            enable_daly_full_readout=enable_daly_full_readout,
        )

    monkeypatch.setattr(DalyFullBMS, "__init__", track_init)

    async def noop_connect(self):
        self._client = MagicMock(is_connected=True)

    monkeypatch.setattr(DalyFullBMS, "_connect", noop_connect)

    wrap = BleWrapBMS(
        "AA:BB:CC:DD:EE:FF",
        type="daly_full_bms",
        blebms_class=DalyFullBMS,
        enable_daly_full_readout=True,
        psk="1234",
        verbose_log=True,
        keep_alive=False,
    )
    asyncio.run(wrap.connect())

    assert len(captured) == 1
    assert captured[0] == {
        "keep_alive": False,
        "secret": "",
        "logger_name": "",
        "enable_daly_full_readout": True,
    }
    assert isinstance(wrap.ble_bms, DalyFullBMS)


def test_construct_bms_daly_ble_connects_despite_legacy_kwargs(monkeypatch):
    from bmslib.models import construct_bms

    async def fake_resolve(addr, adapter=None):
        return MagicMock(address=addr, name=addr)

    monkeypatch.setattr(BLE_BMS_wrap.BLEDeviceResolver, "resolve", staticmethod(fake_resolve))
    monkeypatch.setattr(AiobmsbleDalyBMS, "_connect", AsyncMock())

    dev = {
        "address": "AA:BB:CC:DD:EE:FF",
        "type": "daly_ble",
        "alias": "farm",
        "pin": "1234",
    }
    bt_dev = MagicMock(address="AA:BB:CC:DD:EE:FF", name="DL-test")
    bms = construct_bms(dev, verbose_log=True, bt_discovered_devices=[bt_dev])
    assert isinstance(bms, BleWrapBMS)
    asyncio.run(bms.connect())
    assert isinstance(bms.ble_bms, AiobmsbleDalyBMS)


def test_construct_bms_daly_full_ble_connects_with_only_boolean(monkeypatch):
    from bmslib.models import construct_bms

    async def fake_resolve(addr, adapter=None):
        return MagicMock(address=addr, name=addr)

    monkeypatch.setattr(BLE_BMS_wrap.BLEDeviceResolver, "resolve", staticmethod(fake_resolve))

    captured: list[bool] = []
    real_init = DalyFullBMS.__init__

    def track_init(self, ble_device, keep_alive=True, secret="", logger_name="", enable_daly_full_readout=False):
        captured.append(enable_daly_full_readout)
        return real_init(
            self, ble_device, keep_alive, secret, logger_name,
            enable_daly_full_readout=enable_daly_full_readout,
        )

    monkeypatch.setattr(DalyFullBMS, "__init__", track_init)
    monkeypatch.setattr(DalyFullBMS, "_connect", AsyncMock())

    dev = {
        "address": "AA:BB:CC:DD:EE:FF",
        "type": "daly_full_ble",
        "alias": "farm",
        "pin": "1234",
        "enable_daly_full_readout": True,
    }
    bt_dev = MagicMock(address="AA:BB:CC:DD:EE:FF", name="DL-test")
    bms = construct_bms(dev, verbose_log=True, bt_discovered_devices=[bt_dev])
    asyncio.run(bms.connect())
    assert captured == [True]
    assert isinstance(bms.ble_bms, DalyFullBMS)


def test_ble_wrap_uses_single_ble_bms_client(monkeypatch):
    instances = []

    class _OneClientBMS(DalyFullBMS):
        def __init__(self, ble_device, keep_alive=False, **kwargs):
            instances.append(self)
            super().__init__(ble_device, keep_alive=keep_alive, **kwargs)

        async def _connect(self):
            self._client = MagicMock(is_connected=True)

        async def async_update(self, raw=False):
            return {"voltage": 48.0}

    async def fake_resolve(addr, adapter=None):
        return MagicMock(address=addr, name=addr)

    monkeypatch.setattr(BLE_BMS_wrap.BLEDeviceResolver, "resolve", staticmethod(fake_resolve))

    wrap = BleWrapBMS(
        "AA:BB:CC:DD:EE:FF",
        type="daly_full_bms",
        blebms_class=_OneClientBMS,
        enable_daly_full_readout=True,
    )

    async def _run():
        await wrap.connect()
        await wrap.fetch()

    asyncio.run(_run())
    assert len(instances) == 1
    assert wrap.ble_bms is instances[0]


def test_ble_wrap_attaches_cached_decoded_settings():
    wrap = BleWrapBMS(
        "AA:BB:CC:DD:EE:FF",
        type="daly_full_bms",
        blebms_class=DalyFullBMS,
    )
    from bmslib.bms_ble.plugins.daly_full_decode import decode_daly_settings_blocks
    from bmslib.test.test_daly_full_decode import build_fixture_blocks

    wrap.ble_bms = MagicMock()
    wrap.ble_bms.decoded_settings = decode_daly_settings_blocks(build_fixture_blocks())
    wrap.ble_bms.async_update = AsyncMock(return_value={"voltage": 52.0, "current": 0.0, "cell_count": 0})
    sample = asyncio.run(wrap.fetch())
    assert sample.extra_values is not None
    assert sample.extra_desc is not None
    assert sample.extra_values["rated_capacity_ah"] == 310.0
    assert "998877" not in str(sample.extra_values)


def test_construct_bms_daly_full_ble_parses_option():
    from bmslib.models import construct_bms

    dev = {
        "address": "AA:BB:CC:DD:EE:FF",
        "type": "daly_full_ble",
        "alias": "farm",
        "enable_daly_full_readout": True,
    }
    bt_dev = MagicMock(address="AA:BB:CC:DD:EE:FF", name="DL-test")
    bms = construct_bms(dev, verbose_log=False, bt_discovered_devices=[bt_dev])
    assert isinstance(bms, BleWrapBMS)
    assert bms._enable_daly_full_readout is True
