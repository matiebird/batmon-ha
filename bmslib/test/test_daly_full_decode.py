"""Synthetic fixtures and decode tests for Daly D2 settings blocks."""

from __future__ import annotations

import json
import logging
import math
from typing import Iterable
from unittest.mock import MagicMock

import pytest

from bmslib.bms import BmsSample
from bmslib.bms_ble.plugins.daly_full_bms import BMS as DalyFullBMS
from bmslib.bms_ble.plugins.daly_full_decode import (
    FACTORY_PARAMETER_PASSWORD,
    decode_daly_settings_blocks,
)
from bmslib.models.BLE_BMS_wrap import BMS as BleWrapBMS
from bmslib.mqtt_util import (
    format_extra_numeric,
    publish_hass_discovery,
    publish_sample,
    round_to_n,
)
import bmslib.mqtt_util as mqtt_util


@pytest.fixture(autouse=True)
def _reset_mqtt_publish_cache():
    mqtt_util._last_values = {}
    yield
    mqtt_util._last_values = {}


def _empty_blocks() -> bytearray:
    return bytearray(160)


def _empty_block2() -> bytearray:
    return bytearray(60)


def _set_u16(buf: bytearray, base_addr: int, addr: int, value: int) -> None:
    off = (addr - base_addr) * 2
    buf[off : off + 2] = int(value).to_bytes(2, "big")


def _set_ascii(buf: bytearray, base_addr: int, start: int, text: str, count: int, *, storage_reverse: bool = False) -> None:
    data = text.encode("ascii")
    if storage_reverse:
        data = data[::-1]
    padded = data.ljust(count * 2, b"\x00")[: count * 2]
    for i in range(count):
        _set_u16(buf, base_addr, start + i, int.from_bytes(padded[i * 2 : i * 2 + 2], "big"))


def build_fixture_blocks(**overrides: int | str) -> tuple[tuple[int, bytes], tuple[int, bytes]]:
    b1 = _empty_blocks()
    b2 = _empty_block2()

    numeric_defaults = {
        0x80: 3100,
        0x81: 3200,
        0x82: 1,
        0x83: 16,
        0x84: 0,
        0x85: 0,
        0x86: 2,
        0x87: 0,
        0x88: 0,
        0x89: 0,
        0x8A: 60,
        0x8B: 3600,
        0x8C: 3650,
        0x8D: 2800,
        0x8E: 2700,
        0x8F: 5200,
        0x90: 5500,
        0x91: 4400,
        0x92: 4200,
        0x93: 30100,
        0x94: 30050,
        0x95: 29900,
        0x96: 29850,
        0x97: 85,
        0x98: 90,
        0x99: 45,
        0x9A: 40,
        0x9B: 80,
        0x9C: 85,
        0x9D: 35,
        0x9E: 30,
        0x9F: 50,
        0xA0: 100,
        0xA1: 5,
        0xA2: 8,
        0xA3: 3400,
        0xA4: 10,
        0xA5: 1,
        0xA6: 1,
        0xA7: 1000,
        0xA8: 95,
        0xB0: 0xBEEF,
        0xB8: 0xCAFE,
        0xCE: 1,
        0xCF: 1,
        0xD0: 25,
        0xD1: 1,
        0xD2: 10,
        0xD3: 9600,
        0xD4: (25 << 8) | 7,
        0xD5: (28 << 8) | 14,
        0xD6: (30 << 8) | 45,
        0xD7: 0,
        0xD8: 1,
    }
    for addr, raw in numeric_defaults.items():
        base = 0x80 if addr < 0xD0 else 0xD0
        buf = b1 if addr < 0xD0 else b2
        _set_u16(buf, base, addr, raw)

    _set_ascii(b1, 0x80, 0xA9, "7.1.0-BMS", 7, storage_reverse=True)
    _set_ascii(b1, 0x80, 0xB1, "HW-2.0.1", 7, storage_reverse=False)
    _set_ascii(b1, 0x80, 0xB9, "DL-MACHINE-001", 16, storage_reverse=False)

    # Synthetic non-factory password digits in 0xC9-0xCB (must never be published)
    _set_ascii(b1, 0x80, 0xC9, "998877", 3, storage_reverse=False)

    _set_u16(b1, 0x80, 0xCC, (24 << 8) | 6)
    _set_u16(b1, 0x80, 0xCD, (15 << 8) | 0)

    for addr in range(0xD9, 0xEE):
        _set_u16(b2, 0xD0, addr, 0x1000 + (addr - 0xD9))

    for key, val in overrides.items():
        if isinstance(key, str):
            continue
        base = 0x80 if key < 0xD0 else 0xD0
        buf = b1 if key < 0xD0 else b2
        if isinstance(val, str):
            raise TypeError("string overrides not supported here")
        _set_u16(buf, base, key, val)

    return ((0x80, bytes(b1)), (0xD0, bytes(b2)))


@pytest.fixture
def decoded_fixture():
    return decode_daly_settings_blocks(build_fixture_blocks())


def test_decode_rated_capacity_and_reference_voltage(decoded_fixture):
    v = decoded_fixture.values
    assert v["rated_capacity_ah"] == pytest.approx(310.0)
    assert v["cell_reference_voltage_v"] == pytest.approx(3.2)


def test_decode_collection_board_counts(decoded_fixture):
    v = decoded_fixture.values
    assert v["collection_board_count"] == 1
    assert v["collection_board_1_cell_count"] == 16


def test_decode_battery_chemistry_enum(decoded_fixture):
    assert decoded_fixture.values["battery_chemistry"] == "LiFePO4"


def test_decode_hibernate_wait_time(decoded_fixture):
    assert decoded_fixture.values["hibernate_wait_time_s"] == 60


@pytest.mark.parametrize(
    "key,expected",
    [
        ("cell_voltage_high_level_1_alarm_v", 3.6),
        ("total_voltage_high_level_1_alarm_v", 520.0),
        ("charge_current_high_level_1_alarm_a", 10.0),
        ("discharge_current_high_level_1_alarm_a", -10.0),
        ("charge_temperature_high_level_1_alarm_c", 45.0),
        ("temperature_difference_level_1_alarm_c", 5.0),
        ("balance_start_voltage_v", 3.4),
        ("soc_setting_percent", 100.0),
        ("mos_temperature_protection_alarm_c", 55.0),
    ],
)
def test_decode_scaling_and_bias(key, expected, decoded_fixture):
    assert decoded_fixture.values[key] == pytest.approx(expected)


def test_decode_mos_switch_enums(decoded_fixture):
    v = decoded_fixture.values
    assert v["charge_mos_switch_control"] == "on"
    assert v["discharge_mos_switch_control"] == "on"
    assert v["active_balance_switch"] == "open"


def test_decode_software_version_reverse_rule(decoded_fixture):
    assert decoded_fixture.values["software_version"] == "7.1.0-BMS"


def test_decode_hardware_version_forward(decoded_fixture):
    assert decoded_fixture.values["hardware_version"] == "HW-2.0.1"


def test_decode_machine_code_excludes_password_overlap(decoded_fixture):
    code = decoded_fixture.values["machine_code"]
    assert "998877" not in code
    assert "DL-MACHINE-001".startswith(code.split()[0]) or code.startswith("DL-MACHINE")


def test_password_never_in_decoded_values_plaintext(decoded_fixture):
    for val in decoded_fixture.values.values():
        if isinstance(val, str):
            assert "998877" not in val
            assert FACTORY_PARAMETER_PASSWORD not in val or val == "--"


def test_password_flags_custom(decoded_fixture):
    v = decoded_fixture.values
    assert v["parameter_password_configured"] is True
    assert v["parameter_password_factory_default"] is False


def test_password_flags_factory_default():
    blocks = build_fixture_blocks()
    b1 = bytearray(blocks[0][1])
    _set_ascii(b1, 0x80, 0xC9, FACTORY_PARAMETER_PASSWORD, 3, storage_reverse=False)
    decoded = decode_daly_settings_blocks(((0x80, bytes(b1)), blocks[1]))
    assert decoded.values["parameter_password_factory_default"] is True
    assert decoded.values["parameter_password_configured"] is False


def test_decode_production_date_and_rtc(decoded_fixture):
    assert decoded_fixture.values["production_date"] == "2024-06-15"
    assert decoded_fixture.values["rtc_datetime"] == "2025-07-28 14:30:45"


def test_decode_advanced_enums_and_current(decoded_fixture):
    v = decoded_fixture.values
    assert v["active_balance_current_a"] == pytest.approx(2.5)
    assert v["communication_method"] == "CAN"
    assert v["inverter_manufacturer"] == "DEYE"


def test_decode_d3_labelled_opaque_likely_uart(decoded_fixture):
    assert decoded_fixture.values["raw_0xD3"] == 9600
    assert "likely UART baud" in decoded_fixture.desc["daly_config/raw_0xD3"]["name"]


def test_decode_opaque_b0_b8_and_d9_ed(decoded_fixture):
    v = decoded_fixture.values
    assert v["raw_0xB0"] == 0xBEEF
    assert v["raw_0xB8"] == 0xCAFE
    assert v["raw_0xD9"] == 0x1000
    assert v["raw_0xED"] == 0x1000 + (0xED - 0xD9)


def test_no_password_raw_registers_published(decoded_fixture):
    keys = decoded_fixture.values.keys()
    assert not any("0xC9" in k or "0xCA" in k or "0xCB" in k for k in keys)


def test_every_proven_numeric_address_has_desc(decoded_fixture):
    for addr in range(0x80, 0xA9):
        if addr in {0x89, 0xA5, 0xA6}:
            continue
        assert any(
            meta.get("name") for meta in decoded_fixture.desc.values()
        )


def test_charge_current_abs_boundary():
    blocks = list(build_fixture_blocks())
    b1 = bytearray(blocks[0][1])
    _set_u16(b1, 0x80, 0x93, 30000)
    blocks[0] = (0x80, bytes(b1))
    v = decode_daly_settings_blocks(tuple(blocks)).values
    assert v["charge_current_high_level_1_alarm_a"] == 0.0


def test_all_addresses_decode_without_error():
    decode_daly_settings_blocks(build_fixture_blocks())


class _CaptureClient:
    def __init__(self):
        self.messages: list[tuple[str, object, bool]] = []

    def publish(self, topic, data, retain=False):
        self.messages.append((topic, data, retain))
        info = type("I", (), {"rc": 0})()
        return info


@pytest.mark.parametrize(
    "value,precision,expected",
    [
        (65535, 0, "65535"),
        (3600, 0, "3600"),
        (3.650, 3, "3.650"),
        (0.020, 3, "0.020"),
        (-5.5, 1, "-5.5"),
        (65535, None, "65535"),
    ],
)
def test_format_extra_numeric_fixed_decimal(value, precision, expected):
    assert format_extra_numeric(value, precision) == expected


def test_round_to_n_distorts_large_integers_regression():
    assert round_to_n(65535, 2) == "66000"
    assert format_extra_numeric(65535, 0) == "65535"


def test_publish_extra_values_preserves_exact_integers():
    client = _CaptureClient()
    desc = {
        "daly_config/force_start_switch": {
            "field": "force_start_switch",
            "name": "Force Start Switch",
            "precision": 0,
            "entity_category": "diagnostic",
        },
        "daly_config/raw_0xD9": {
            "field": "raw_0xD9",
            "name": "Daly Raw Register 0xD9",
            "precision": None,
            "entity_category": "diagnostic",
        },
        "daly_config/cell_voltage_high_level_1_alarm_v": {
            "field": "cell_voltage_high_level_1_alarm_v",
            "name": "Cell Voltage High Level 1 Alarm",
            "precision": 3,
            "entity_category": "diagnostic",
        },
        "daly_config/charge_temperature_low_level_2_alarm_c": {
            "field": "charge_temperature_low_level_2_alarm_c",
            "name": "Charge Temperature Low Level 2 Alarm",
            "precision": 1,
            "entity_category": "diagnostic",
        },
    }
    sample = BmsSample(
        voltage=48.0,
        current=0.0,
        extra_values={
            "force_start_switch": 65535,
            "raw_0xD9": 65535,
            "cell_voltage_high_level_1_alarm_v": 3.650,
            "charge_temperature_low_level_2_alarm_c": -5.5,
        },
        extra_desc=desc,
    )
    publish_sample(client, "farm", sample)
    published = {t: d for t, d, _ in client.messages}
    assert published["farm/daly_config/force_start_switch"] == "65535"
    assert published["farm/daly_config/raw_0xD9"] == "65535"
    assert published["farm/daly_config/cell_voltage_high_level_1_alarm_v"] == "3.650"
    assert published["farm/daly_config/charge_temperature_low_level_2_alarm_c"] == "-5.5"


def test_mqtt_extra_publish_and_discovery_no_password(caplog, decoded_fixture):
    caplog.set_level(logging.INFO)
    client = _CaptureClient()
    sample = BmsSample(
        voltage=52.0,
        current=0.0,
        extra_values=decoded_fixture.values,
        extra_desc=decoded_fixture.desc,
    )
    publish_sample(client, "farm", sample)
    publish_hass_discovery(
        client,
        device_topic="farm",
        expire_after_seconds=20,
        sample=sample,
        num_cells=0,
        temperatures=[],
    )

    blob = json.dumps([(t, m, r) for t, m, r in client.messages])
    assert "998877" not in blob
    assert "command_topic" not in blob
    assert any(t.endswith("daly_config/rated_capacity_ah") for t, _, _ in client.messages)
    assert any("homeassistant/sensor/farm/_daly_config_rated_capacity_ah/config" in t for t, _, _ in client.messages)
    assert any(
        isinstance(m, str) and '"expire_after": 7200' in m
        for t, m, _ in client.messages
        if t.startswith("homeassistant/") and "daly_config" in t
    )
    assert "blocks=" not in caplog.text


def test_mqtt_skips_nonfinite_extra():
    client = _CaptureClient()
    desc = {
        "daly_config/bad": {
            "field": "bad",
            "name": "Bad",
            "long_expiry": True,
            "entity_category": "diagnostic",
        }
    }
    sample = BmsSample(voltage=48.0, current=0.0, extra_values={"bad": math.nan}, extra_desc=desc)
    publish_sample(client, "farm", sample)
    assert not any("daly_config/bad" in t for t, _, _ in client.messages)


def _discovery_messages(sample: BmsSample) -> list[tuple[str, object, bool]]:
    client = _CaptureClient()
    publish_hass_discovery(
        client,
        device_topic="farm",
        expire_after_seconds=20,
        sample=sample,
        num_cells=0,
        temperatures=[],
    )
    return client.messages


def _discovery_blob(sample: BmsSample) -> str:
    return json.dumps(
        [(t, m) for t, m, _ in _discovery_messages(sample) if isinstance(m, str)]
    )


def test_daly_full_mos_discovery_read_only_no_command_topic():
    sample = BmsSample(
        voltage=52.0,
        current=0.0,
        switches={"charge": True, "discharge": False},
        switches_writable=False,
    )
    messages = _discovery_messages(sample)
    config_msgs = {t: (d, r) for t, d, r in messages}
    blob = json.dumps([(t, d) for t, d, _ in messages if isinstance(d, str)])
    assert "command_topic" not in blob
    assert config_msgs["homeassistant/switch/farm/charge/config"] == ("", True)
    assert config_msgs["homeassistant/switch/farm/discharge/config"] == ("", True)
    assert "homeassistant/binary_sensor/farm/charge_state/config" in config_msgs


def test_daly_ble_mos_discovery_retains_command_topic():
    sample = BmsSample(
        voltage=52.0,
        current=0.0,
        switches={"charge": True, "discharge": False},
        switches_writable=True,
    )
    blob = _discovery_blob(sample)
    assert "command_topic" in blob
    assert "homeassistant/switch/farm/charge/config" in blob


def test_subscribe_skipped_for_readonly_switches():
    from bmslib.mqtt_util import subscribe_switches, _switch_callbacks

    _switch_callbacks.clear()
    client = MagicMock()
    sample = BmsSample(
        voltage=48.0,
        current=0.0,
        switches={"charge": True, "discharge": False},
        switches_writable=False,
    )
    if sample.switches and sample.switches_writable:
        subscribe_switches(client, "farm", MagicMock(), sample.switches.keys())
    client.subscribe.assert_not_called()
    assert not _switch_callbacks


def test_subscribe_active_for_writable_switches():
    from bmslib.mqtt_util import subscribe_switches, _switch_callbacks

    _switch_callbacks.clear()
    client = MagicMock()
    sample = BmsSample(
        voltage=48.0,
        current=0.0,
        switches={"charge": True},
        switches_writable=True,
    )
    if sample.switches and sample.switches_writable:
        subscribe_switches(client, "farm", MagicMock(), sample.switches.keys())
    assert client.subscribe.call_count == 1
    assert _switch_callbacks


def test_writable_to_readonly_switch_transition_tombstones_and_skips_subscribe():
    from bmslib.mqtt_util import subscribe_switches, _switch_callbacks

    device_topic = "farm_transition"

    writable = BmsSample(
        voltage=52.0,
        current=0.0,
        switches={"charge": True, "discharge": False},
        switches_writable=True,
    )
    client = _CaptureClient()
    publish_hass_discovery(
        client,
        device_topic=device_topic,
        expire_after_seconds=20,
        sample=writable,
        num_cells=0,
        temperatures=[],
    )
    writable_msgs = client.messages
    assert any(
        t == f"homeassistant/switch/{device_topic.replace('/', '_')}/charge/config"
        and isinstance(d, str)
        and "command_topic" in d
        for t, d, _ in writable_msgs
    )

    readonly = BmsSample(
        voltage=52.0,
        current=0.0,
        switches={"charge": True, "discharge": False},
        switches_writable=False,
    )
    client = _CaptureClient()
    publish_hass_discovery(
        client,
        device_topic=device_topic,
        expire_after_seconds=20,
        sample=readonly,
        num_cells=0,
        temperatures=[],
    )
    readonly_msgs = {t: (d, r) for t, d, r in client.messages}
    node = device_topic.replace("/", "_")
    assert readonly_msgs[f"homeassistant/switch/{node}/charge/config"] == ("", True)
    assert readonly_msgs[f"homeassistant/switch/{node}/discharge/config"] == ("", True)
    assert f"homeassistant/binary_sensor/{node}/charge_state/config" in readonly_msgs
    assert "command_topic" not in json.dumps(
        [d for d, _ in readonly_msgs.values() if isinstance(d, str)]
    )

    _switch_callbacks.clear()
    sub_client = MagicMock()
    if readonly.switches and readonly.switches_writable:
        subscribe_switches(sub_client, device_topic, MagicMock(), readonly.switches.keys())
    sub_client.subscribe.assert_not_called()
    assert not _switch_callbacks


def test_secret_absent_from_diagnostic_and_debug_paths(decoded_fixture):
    from bmslib.test.test_daly_full_decode import build_fixture_blocks

    b1, b2 = build_fixture_blocks()
    blocks = (b1, b2)
    plugin = DalyFullBMS(MagicMock(), enable_daly_full_readout=True)
    plugin._decoded_settings = decode_daly_settings_blocks(blocks)
    plugin._diagnostic_readout = {
        "protocol_unit": 0xD2,
        "covered_ranges": "0x0080-0x00CF",
        "register_count": 110,
    }
    wrap = BleWrapBMS("AA:BB:CC:DD:EE:FF", type="daly_full_bms", blebms_class=DalyFullBMS)
    wrap.ble_bms = plugin
    wrap._last_sample = {"voltage": 48.0}
    blob = json.dumps(
        {
            "diagnostic": plugin.diagnostic_readout,
            "debug": wrap.debug_data(),
            "decoded": dict(plugin.decoded_settings.values),
        }
    )
    assert "998877" not in blob
