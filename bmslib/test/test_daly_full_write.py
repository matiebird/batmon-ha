"""Tests for Daly D2 writable registry, protocol, staging, backup, apply, and MQTT."""

from __future__ import annotations

import asyncio
import math
import os
import stat
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bmslib.bms_ble.plugins.daly_full_backup import (
    backup_contains_password_material,
    load_backup,
    save_backup,
)
from bmslib.bms_ble.plugins.daly_full_protocol import (
    WRITE_FRAME_LEN,
    assert_allowlisted_write_address,
    assert_valid_registry_write_frame,
    assert_valid_restart_write_frame,
    build_d2_write_frame,
    build_field_write_frame,
    build_restart_write_frame,
    check_crc,
    modbus_crc_append,
    validate_d2_write_echo,
)
from bmslib.bms_ble.plugins.daly_full_staging import DalyStagingState, ARM_EXPIRY_SECONDS
from bmslib.bms_ble.plugins.daly_full_apply import (
    apply_staged_settings,
    restart_daly_system,
    restore_last_settings,
    values_from_decoded,
)
from bmslib.bms_ble.plugins.daly_full_decode import (
    INVERTER_SELF_IDENTIFY_LABEL,
    INVERTER_SELF_IDENTIFY_RAW,
    decode_daly_settings_blocks,
)
from bmslib.bms_ble.plugins.daly_full_bms import BMS as DalyFullBMS
from bmslib.bms_ble.plugins.daly_full_write_registry import (
    HIBERNATE_SPECIAL_RAW,
    WRITE_FIELDS,
    WRITE_FIELDS_BY_KEY,
    build_write_plan,
    encode_field,
    validate_cross_fields,
    validate_prospective_configuration,
)
from bmslib.bms_ble.plugins.daly_full_mqtt_controls import (
    ACTION_APPLY,
    ACTION_DISCARD,
    build_daly_full_discovery,
    enqueue_daly_action,
    mqtt_process_daly_action_queue,
    publish_daly_full_tombstones,
    writable_discovery_config_topics,
    _daly_action_callbacks,
    _daly_message_queue,
)
from bmslib.test.test_daly_full_decode import build_fixture_blocks, _set_u16


def test_build_d2_write_frame_rated_capacity():
    frame = build_d2_write_frame(0x0080, 3100)
    assert len(frame) == WRITE_FRAME_LEN
    assert frame[:6] == bytes.fromhex("d20600800c1c")
    assert check_crc(frame)


def test_write_crc_is_low_byte_first():
    body = bytes.fromhex("d20600800c1c")
    framed = modbus_crc_append(body)
    assert framed[:-2] == body
    assert framed[-2:] == bytes.fromhex("9f48")


def test_yc_factory_address_rejected():
    with pytest.raises(ValueError, match="excluded"):
        build_d2_write_frame(0x0580, 1)


def test_bms_has_no_public_raw_write_api():
    assert not hasattr(DalyFullBMS, "send_write_frame")
    assert hasattr(DalyFullBMS, "write_field")
    assert hasattr(DalyFullBMS, "restart_system")


def test_soc_zero_encodes_raw_zero():
    field = WRITE_FIELDS_BY_KEY["soc_setting_percent"]
    assert encode_field(field, 0) == 0


def test_capacity_rejects_zero():
    field = WRITE_FIELDS_BY_KEY["rated_capacity_ah"]
    with pytest.raises(ValueError):
        encode_field(field, 0)


def test_hibernate_accepts_arbitrary_seconds_in_range():
    field = WRITE_FIELDS_BY_KEY["hibernate_wait_time_s"]
    assert encode_field(field, 45) == 45
    assert encode_field(field, HIBERNATE_SPECIAL_RAW) == HIBERNATE_SPECIAL_RAW
    assert WRITE_FIELDS_BY_KEY["hibernate_wait_time_s"].entity_type.value == "number"


def test_inverter_official_options_round_trip():
    field = WRITE_FIELDS_BY_KEY["inverter_manufacturer"]
    for raw, label in ((19, "Sunsynk"), (23, "YWTNBQ"), (INVERTER_SELF_IDENTIFY_RAW, INVERTER_SELF_IDENTIFY_LABEL)):
        assert encode_field(field, label) == raw
        assert encode_field(field, raw) == raw
    options = set(field.options or ())
    assert "Sunsynk" in options and INVERTER_SELF_IDENTIFY_LABEL in options


def test_cross_field_rejects_inverted_charge_temperatures():
    baseline = {
        "charge_temperature_high_level_1_alarm_c": 10.0,
        "charge_temperature_low_level_1_alarm_c": 0.0,
    }
    with pytest.raises(ValueError):
        validate_prospective_configuration(
            baseline,
            {
                "charge_temperature_high_level_2_alarm_c": 0.0,
                "charge_temperature_low_level_2_alarm_c": 20.0,
            },
        )


def test_cross_field_rejects_adversarial_cell_high_zero():
    with pytest.raises(ValueError):
        validate_prospective_configuration(
            {},
            {
                "cell_voltage_high_level_2_alarm_v": 0.0,
                "cell_voltage_low_level_2_alarm_v": 2.0,
            },
        )


class _FakeDalyWire:
    def __init__(self, blocks):
        self.blocks = [tuple((a, bytearray(d))) for a, d in blocks]
        self.writes: list[tuple[str, object]] = []
        self.reconnect_count = 0

    async def fetch_settings_blocks(self, *, force: bool = False):
        return tuple((a, bytes(d)) for a, d in self.blocks)

    async def write_field(self, field_key: str, value) -> None:
        field = WRITE_FIELDS_BY_KEY[field_key]
        raw = encode_field(field, value)
        frame = build_d2_write_frame(field.address, raw)
        self.writes.append((field_key, value))
        addr = field.address
        for base, data in self.blocks:
            if base <= addr < base + len(data) // 2:
                off = (addr - base) * 2
                data[off : off + 2] = raw.to_bytes(2, "big")
                return

    async def restart_system(self) -> None:
        frame = build_d2_write_frame(0x00F0, 0)
        self.writes.append(("system_restart", None))
        assert frame[:6] == bytes.fromhex("d20600f00000")

    async def reconnect(self) -> None:
        self.reconnect_count += 1


def test_apply_writes_only_diffs_in_order(tmp_path):
    blocks = build_fixture_blocks()
    current = values_from_decoded(decode_daly_settings_blocks(blocks))
    wire = _FakeDalyWire(blocks)
    staging = DalyStagingState(current=current)
    staging.stage("soc_setting_percent", current["soc_setting_percent"])
    staging.stage("rated_capacity_ah", current["rated_capacity_ah"] + 1)

    result = asyncio.run(apply_staged_settings(wire, staging, device_id="dev1", data_dir=tmp_path))
    assert result.ok
    assert result.written_fields == ("rated_capacity_ah",)
    assert len(wire.writes) == 1


def test_backup_stores_pre_write_values_not_post_write(tmp_path):
    blocks = build_fixture_blocks()
    b1, b2 = blocks
    buf = bytearray(b1[1])
    _set_u16(buf, 0x80, 0x80, 3100)
    blocks = ((0x80, bytes(buf)), b2)
    current = values_from_decoded(decode_daly_settings_blocks(blocks))
    wire = _FakeDalyWire(blocks)
    staging = DalyStagingState(current=current)
    staging.stage("rated_capacity_ah", 311.0)

    result = asyncio.run(apply_staged_settings(wire, staging, device_id="cap", data_dir=tmp_path))
    assert result.ok
    backup = load_backup("cap", tmp_path)
    assert backup["values"]["rated_capacity_ah"] == 310.0


def test_restore_writes_original_backup_values(tmp_path):
    blocks = build_fixture_blocks()
    b1, b2 = blocks
    buf = bytearray(b1[1])
    _set_u16(buf, 0x80, 0x80, 3100)
    blocks_orig = ((0x80, bytes(buf)), b2)
    current = values_from_decoded(decode_daly_settings_blocks(blocks_orig))
    wire = _FakeDalyWire(blocks_orig)
    staging = DalyStagingState(current=current)
    staging.stage("rated_capacity_ah", 311.0)
    asyncio.run(apply_staged_settings(wire, staging, device_id="cap2", data_dir=tmp_path))

    buf2 = bytearray(buf)
    _set_u16(buf2, 0x80, 0x80, 3110)
    wire.blocks = [(0x80, buf2), (b2[0], bytearray(b2[1]))]
    staging2 = DalyStagingState()
    result = asyncio.run(restore_last_settings(wire, staging2, device_id="cap2", data_dir=tmp_path))
    assert result.ok
    assert wire.writes and wire.writes[-1][0] == "rated_capacity_ah"
    assert backup_contains_password_material(load_backup("cap2", tmp_path)["values"].__repr__()) is False
    backup_after = load_backup("cap2", tmp_path)
    assert backup_after["values"]["rated_capacity_ah"] == 310.0


def test_apply_partial_failure_retains_pre_write_backup(tmp_path):
    blocks = build_fixture_blocks()
    current = values_from_decoded(decode_daly_settings_blocks(blocks))

    class _FailWire(_FakeDalyWire):
        async def write_field(self, field_key: str, value) -> None:
            if len(self.writes) >= 1:
                raise RuntimeError("fail")
            await super().write_field(field_key, value)

    wire = _FailWire(blocks)
    staging = DalyStagingState(current=current)
    staging.stage("soc_setting_percent", 10.0)
    staging.stage("rated_capacity_ah", current["rated_capacity_ah"] + 5)

    result = asyncio.run(apply_staged_settings(wire, staging, device_id="dev1", data_dir=tmp_path))
    assert not result.ok
    backup = load_backup("dev1", tmp_path)
    assert backup["values"]["soc_setting_percent"] == current["soc_setting_percent"]
    assert backup["values"]["rated_capacity_ah"] == current["rated_capacity_ah"]


def test_apply_aborts_with_zero_writes_if_backup_fails(tmp_path):
    blocks = build_fixture_blocks()
    current = values_from_decoded(decode_daly_settings_blocks(blocks))
    wire = _FakeDalyWire(blocks)
    staging = DalyStagingState(current=current)
    staging.stage("rated_capacity_ah", current["rated_capacity_ah"] + 1)

    with patch("bmslib.bms_ble.plugins.daly_full_apply.save_backup", side_effect=OSError("disk")):
        result = asyncio.run(apply_staged_settings(wire, staging, device_id="dev1", data_dir=tmp_path))
    assert result.status == "backup_failed"
    assert len(wire.writes) == 0


def test_restart_requires_arm_and_does_not_apply_pending(tmp_path):
    blocks = build_fixture_blocks()
    current = values_from_decoded(decode_daly_settings_blocks(blocks))
    wire = _FakeDalyWire(blocks)
    staging = DalyStagingState(current=current)
    staging.stage("soc_setting_percent", 99.0)

    unarmed = asyncio.run(restart_daly_system(wire, staging))
    assert not unarmed.ok
    assert unarmed.status == "restart_unarmed"
    assert wire.writes == []
    assert "soc_setting_percent" in staging.staged

    staging.arm_advanced()
    armed = asyncio.run(restart_daly_system(wire, staging))
    assert armed.ok
    assert wire.writes == [("system_restart", None)]
    assert staging.staged["soc_setting_percent"] == 99.0


def test_backup_permissions_and_no_password(tmp_path):
    values = {"rated_capacity_ah": 100.0, "soc_setting_percent": 50.0}
    path = save_backup("dev1", values, changed_fields=("rated_capacity_ah",), timestamp=1.0, data_dir=tmp_path)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600
    text = path.read_text()
    assert not backup_contains_password_material(text)
    assert "123456" not in text


def test_mqtt_tombstones_cover_all_writable_discovery_topics():
    topics = writable_discovery_config_topics("farm/bms1")
    assert any("number" in t for t in topics)
    assert any("select/" in t for t in topics)
    assert any("daly_apply" in t for t in topics)
    assert any("daly_arm_advanced" in t for t in topics)
    assert any("daly_pending_diagnostics" in t for t in topics)


def test_mqtt_invalid_action_then_apply_continues():
    calls = []

    async def bad(_payload):
        raise ValueError("invalid staged field")

    async def apply_ok(_payload):
        calls.append("apply")

    topic_bad = "farm/daly_write/soc_setting_percent/set"
    topic_apply = "farm/daly_write/apply"
    _daly_action_callbacks.clear()
    while not _daly_message_queue.empty():
        _daly_message_queue.get()
    _daly_action_callbacks[topic_bad] = bad
    _daly_action_callbacks[topic_apply] = apply_ok
    enqueue_daly_action(topic_bad, b"not-a-number")
    enqueue_daly_action(topic_apply, b"PRESS")
    asyncio.run(mqtt_process_daly_action_queue())
    assert calls == ["apply"]


def test_no_charge_mos_write_in_registry():
    assert "charge_mos_switch_control" not in WRITE_FIELDS_BY_KEY


def test_restart_frame_constant():
    frame = build_restart_write_frame()
    assert frame[2:6] == bytes.fromhex("00f00000")


def _make_restart_bms():
    bms = DalyFullBMS.__new__(DalyFullBMS)
    bms._wire_lock = asyncio.Lock()
    bms._expected_write_echo = None
    bms._msg_event = asyncio.Event()
    bms._msg = None
    bms.TIMEOUT = 1.0
    bms._log = MagicMock()
    bms._client = MagicMock()
    bms.uuid_tx = lambda: "tx-char"
    return bms


def test_restart_system_semantic_definition_end_to_end():
    bms = _make_restart_bms()
    captured: list[bytes] = []

    async def _write(_char, frame, response=False):
        captured.append(bytes(frame))
        bms._msg = bytes(frame)
        bms._msg_event.set()

    bms._client.write_gatt_char = AsyncMock(side_effect=_write)
    asyncio.run(bms.restart_system())
    assert len(captured) == 1
    assert captured[0] == build_restart_write_frame()
    assert_valid_restart_write_frame(captured[0])


def test_bms_rejects_invalid_semantic_write_before_client():
    bms = _make_restart_bms()
    bms._client.write_gatt_char = AsyncMock()

    async def _run():
        with pytest.raises(ValueError):
            await bms.write_field("rated_capacity_ah", 0)
        bms._client.write_gatt_char.assert_not_called()

    asyncio.run(_run())


def test_bms_has_no_arbitrary_bytes_write_api():
    forbidden = {"_transmit_registry_frame", "send_write_frame"}
    assert forbidden.isdisjoint(set(dir(DalyFullBMS)))
    import inspect

    for name, method in inspect.getmembers(DalyFullBMS, predicate=inspect.isfunction):
        if method.__module__ != "bmslib.bms_ble.plugins.daly_full_bms":
            continue
        if name in ("_notification_handler",):
            continue
        sig = inspect.signature(method)
        for param in sig.parameters.values():
            ann = param.annotation
            if ann in (bytes, bytearray):
                pytest.fail("DalyFullBMS.%s accepts raw bytes" % name)


@pytest.mark.parametrize(
    "field_key,bad_value",
    [
        ("rated_capacity_ah", True),
        ("rated_capacity_ah", float("nan")),
        ("rated_capacity_ah", float("inf")),
        ("collection_board_1_cell_count", 1.5),
        ("collection_board_1_cell_count", True),
        ("battery_chemistry", 1.9),
        ("battery_chemistry", True),
        ("charge_current_high_level_1_alarm_a", -5),
        ("charge_current_high_level_2_alarm_a", -1),
        ("discharge_current_high_level_2_alarm_a", -2),
        ("soc_setting_percent", 100.5),
    ],
)
def test_adversarial_field_values_rejected(field_key, bad_value):
    field = WRITE_FIELDS_BY_KEY[field_key]
    with pytest.raises(ValueError):
        encode_field(field, bad_value)


def test_integer_valued_float_accepted_for_ha_number():
    field = WRITE_FIELDS_BY_KEY["collection_board_1_cell_count"]
    assert encode_field(field, 16.0) == 16


def test_charge_current_negative_not_abs_normalized():
    field = WRITE_FIELDS_BY_KEY["charge_current_high_level_1_alarm_a"]
    with pytest.raises(ValueError, match=">= 0"):
        encode_field(field, -5)


def test_frame_validation_rejects_malformed_protocol_unit():
    good = build_field_write_frame("rated_capacity_ah", 3100)
    bad = bytes([0x81]) + good[1:]
    with pytest.raises(ValueError, match="header"):
        assert_valid_registry_write_frame(bad, field_key="rated_capacity_ah", raw_value=3100)


def test_frame_validation_rejects_bad_crc():
    good = build_field_write_frame("rated_capacity_ah", 3100)
    bad = good[:-2] + b"\xff\xff"
    with pytest.raises(ValueError, match="CRC"):
        assert_valid_registry_write_frame(bad, field_key="rated_capacity_ah", raw_value=3100)


def test_frame_validation_rejects_wrong_raw_for_key():
    good = build_field_write_frame("rated_capacity_ah", 3100)
    body = good[:4] + (3101).to_bytes(2, "big")
    wrong_raw = modbus_crc_append(body)
    with pytest.raises(ValueError, match="raw value mismatch"):
        assert_valid_registry_write_frame(wrong_raw, field_key="rated_capacity_ah", raw_value=3100)


def test_yc_address_cannot_build_write_frame():
    with pytest.raises(ValueError, match="excluded"):
        build_d2_write_frame(0x0580, 1)


def test_restore_cancelled_preserves_exception_identity(tmp_path):
    blocks = build_fixture_blocks()
    current = values_from_decoded(decode_daly_settings_blocks(blocks))
    wire = _FakeDalyWire(blocks)
    staging = DalyStagingState(current=current)
    staging.stage("rated_capacity_ah", current["rated_capacity_ah"] + 1)
    applied = asyncio.run(apply_staged_settings(wire, staging, device_id="cancel", data_dir=tmp_path))
    assert applied.ok

    class _CancelWire(_FakeDalyWire):
        async def write_field(self, field_key: str, value) -> None:
            raise asyncio.CancelledError()

    cancel_wire = _CancelWire(list(wire.blocks))
    restore_staging = DalyStagingState()
    prev_staged = {"soc_setting_percent": 12.0}
    restore_staging.staged = dict(prev_staged)

    async def _run_restore():
        with pytest.raises(asyncio.CancelledError) as exc:
            await restore_last_settings(
                cancel_wire, restore_staging, device_id="cancel", data_dir=tmp_path
            )
        assert type(exc.value) is asyncio.CancelledError

    asyncio.run(_run_restore())
    assert restore_staging.staged == prev_staged
    assert restore_staging.last_apply_status == "restore_cancelled"


def test_concurrent_apply_peak_one_operation_lock(tmp_path):
    blocks = build_fixture_blocks()
    current = values_from_decoded(decode_daly_settings_blocks(blocks))
    peak = 0
    active = 0
    op_lock = asyncio.Lock()

    class _TrackedWire(_FakeDalyWire):
        async def fetch_settings_blocks(self, *, force: bool = False):
            nonlocal peak, active
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1
            return await super().fetch_settings_blocks(force=force)

    wire = _TrackedWire(blocks)
    bms = DalyFullBMS.__new__(DalyFullBMS)
    bms._operation_lock = op_lock

    staging_a = DalyStagingState(current=current)
    staging_a.stage("rated_capacity_ah", current["rated_capacity_ah"] + 1)
    staging_b = DalyStagingState(current=current)
    staging_b.stage("soc_setting_percent", 11.0)

    async def _wrapped_apply(staging):
        async with bms._operation_lock:
            return await apply_staged_settings(wire, staging, device_id="peak", data_dir=tmp_path)

    async def _run():
        await asyncio.gather(_wrapped_apply(staging_a), _wrapped_apply(staging_b))

    asyncio.run(_run())
    assert peak == 1


def test_sampling_tombstones_without_device_info():
    mqtt = MagicMock()
    mock_info = MagicMock()
    mock_info.rc = 0
    captured: list[tuple[str, str, bool]] = []

    def _publish(topic, data, retain=False):
        captured.append((topic, data, bool(retain)))
        return mock_info

    mqtt.publish = _publish

    bms = MagicMock()
    bms.daly_full_capable = True
    bms.daly_writable = False
    device_info = None
    topic = "farm/bms1"

    if getattr(bms, "daly_full_capable", False):
        if getattr(bms, "daly_writable", False):
            if device_info is not None:
                pytest.fail("discovery should not run without device_info")
        else:
            publish_daly_full_tombstones(mqtt, topic)

    topics = {t for t, _, _ in captured}
    expected = set(writable_discovery_config_topics(topic))
    assert expected.issubset(topics)
    assert all(retain for _, _, retain in captured)
    assert all(payload == "" for _, payload, _ in captured)


def test_all_write_fields_validate_before_encode():
    for field in WRITE_FIELDS:
        if field.entity_type.value == "button":
            continue
        with pytest.raises(ValueError):
            encode_field(field, True)


INVALID_REGISTRY_BOUNDARIES: tuple[tuple[str, object], ...] = (
    ("rated_capacity_ah", 0),
    ("battery_chemistry", "invalid"),
    ("hibernate_wait_time_s", 29),
    ("soc_setting_percent", 101),
    ("communication_method", "invalid"),
    ("inverter_manufacturer", "invalid"),
    ("collection_board_1_cell_count", 0),
    ("collection_board_1_temp_sensor_count", 17),
    ("cell_voltage_high_level_2_alarm_v", 0),
    ("cell_voltage_low_level_2_alarm_v", 6.0),
    ("total_voltage_high_level_2_alarm_v", 0),
    ("total_voltage_low_level_2_alarm_v", 0),
    ("charge_current_high_level_1_alarm_a", -1),
    ("charge_current_high_level_2_alarm_a", -1),
    ("discharge_current_high_level_2_alarm_a", -1),
    ("charge_temperature_high_level_2_alarm_c", True),
    ("charge_temperature_low_level_2_alarm_c", True),
    ("discharge_temperature_high_level_2_alarm_c", True),
    ("discharge_temperature_low_level_2_alarm_c", True),
    ("cell_voltage_difference_level_2_alarm_v", 0),
    ("temperature_difference_level_2_alarm_c", True),
    ("balance_start_voltage_v", 0),
    ("balance_start_voltage_difference_v", 0),
    ("discharge_mos_switch_control", "invalid"),
)


@pytest.mark.parametrize("field_key,bad_value", INVALID_REGISTRY_BOUNDARIES)
def test_registry_field_rejects_invalid_boundary(field_key, bad_value):
    field = WRITE_FIELDS_BY_KEY[field_key]
    with pytest.raises(ValueError):
        encode_field(field, bad_value)


def test_registry_validators_never_return_bool():
    from bmslib.bms_ble.plugins.daly_full_write_registry import EntityType

    for field in WRITE_FIELDS:
        if field.entity_type == EntityType.BUTTON:
            continue
        try:
            result = field.validate(0)
        except ValueError:
            continue
        assert result is None


def test_total_voltage_zero_rejected_before_transport():
    for key in ("total_voltage_high_level_2_alarm_v", "total_voltage_low_level_2_alarm_v"):
        with pytest.raises(ValueError, match="> 0"):
            encode_field(WRITE_FIELDS_BY_KEY[key], 0)


def test_retained_daly_commands_not_queued():
    calls: list[str] = []

    async def _handler(payload: str) -> None:
        calls.append(payload)

    device = "farm/bms1"
    arm_topic = "%s/daly_write/arm_advanced/set" % device
    restart_topic = "%s/daly_write/restart" % device
    stage_topic = "%s/daly_write/soc_setting_percent/set" % device
    apply_topic = "%s/daly_write/apply" % device

    _daly_action_callbacks.clear()
    while not _daly_message_queue.empty():
        _daly_message_queue.get()
    for topic in (arm_topic, restart_topic, stage_topic, apply_topic):
        _daly_action_callbacks[topic] = _handler

    assert enqueue_daly_action(arm_topic, b"ON", retain=True)
    assert enqueue_daly_action(restart_topic, b"PRESS", retain=True)
    assert enqueue_daly_action(stage_topic, b"50", retain=True)
    assert enqueue_daly_action(apply_topic, b"PRESS", retain=True)
    asyncio.run(mqtt_process_daly_action_queue())
    assert calls == []


def test_retained_daly_commands_rejected_in_production_callback():
    from bmslib.mqtt_util import mqtt_message_handler

    calls: list[str] = []

    async def _handler(payload: str) -> None:
        calls.append(payload)

    device = "farm/bms1"
    arm_topic = "%s/daly_write/arm_advanced/set" % device
    apply_topic = "%s/daly_write/apply" % device
    _daly_action_callbacks.clear()
    while not _daly_message_queue.empty():
        _daly_message_queue.get()
    _daly_action_callbacks[arm_topic] = _handler
    _daly_action_callbacks[apply_topic] = _handler

    for topic, payload in ((arm_topic, b"ON"), (apply_topic, b"PRESS")):
        msg = MagicMock()
        msg.topic = topic
        msg.payload = payload
        msg.retain = True
        mqtt_message_handler(None, None, msg)

    asyncio.run(mqtt_process_daly_action_queue())
    assert calls == []


def test_mqtt_handler_non_utf8_daly_payload_does_not_raise():
    from bmslib.mqtt_util import mqtt_message_handler

    calls: list[str] = []

    async def _handler(_payload: str) -> None:
        calls.append("called")

    topic = "farm/daly_write/apply"
    _daly_action_callbacks.clear()
    while not _daly_message_queue.empty():
        _daly_message_queue.get()
    _daly_action_callbacks[topic] = _handler

    msg = MagicMock()
    msg.topic = topic
    msg.payload = b"\xff"
    msg.retain = False
    mqtt_message_handler(None, None, msg)
    asyncio.run(mqtt_process_daly_action_queue())
    assert calls == []


def test_mqtt_handler_non_utf8_switch_payload_does_not_raise():
    from bmslib.mqtt_util import mqtt_message_handler, _switch_callbacks, _message_queue

    _switch_callbacks.clear()
    while not _message_queue.empty():
        _message_queue.get()
    topic = "farm/switch/charge/set"
    _switch_callbacks[topic] = AsyncMock()

    msg = MagicMock()
    msg.topic = topic
    msg.payload = b"\xff"
    msg.retain = False
    mqtt_message_handler(None, None, msg)
    assert _message_queue.empty()


def test_apply_advanced_arm_expired_before_tier3_write(tmp_path, monkeypatch):
    blocks = build_fixture_blocks()
    current = values_from_decoded(decode_daly_settings_blocks(blocks))
    wire = _FakeDalyWire(blocks)
    staging = DalyStagingState(current=current)
    staging.stage("discharge_mos_switch_control", "off")
    staging.arm_advanced(now=1000.0)
    monkeypatch.setattr("bmslib.bms_ble.plugins.daly_full_apply.time.time", lambda: 1061.0)

    result = asyncio.run(
        apply_staged_settings(wire, staging, device_id="armexp", data_dir=tmp_path, now=1000.0)
    )
    assert result.status == "advanced_arm_expired"
    assert result.failed_field == "discharge_mos_switch_control"
    assert wire.writes == []


def test_restart_rechecks_arm_and_disarms_after_attempt(monkeypatch):
    blocks = build_fixture_blocks()
    current = values_from_decoded(decode_daly_settings_blocks(blocks))
    wire = _FakeDalyWire(blocks)
    staging = DalyStagingState(current=current)
    staging.arm_advanced(now=1000.0)
    monkeypatch.setattr("bmslib.bms_ble.plugins.daly_full_apply.time.time", lambda: 1061.0)

    unarmed = asyncio.run(restart_daly_system(wire, staging, now=1000.0))
    assert not unarmed.ok
    assert unarmed.status == "restart_unarmed"
    assert wire.writes == []

    staging.arm_advanced(now=2000.0)
    monkeypatch.setattr("bmslib.bms_ble.plugins.daly_full_apply.time.time", lambda: 2000.0)
    armed = asyncio.run(restart_daly_system(wire, staging, now=2000.0))
    assert armed.ok
    assert wire.writes == [("system_restart", None)]
    assert not staging.is_armed(2000.0)


def test_tier3_write_disarms_after_attempt(tmp_path, monkeypatch):
    blocks = build_fixture_blocks()
    current = values_from_decoded(decode_daly_settings_blocks(blocks))
    wire = _FakeDalyWire(blocks)
    staging = DalyStagingState(current=current)
    staging.stage("discharge_mos_switch_control", "off")
    staging.arm_advanced(now=1000.0)
    monkeypatch.setattr("bmslib.bms_ble.plugins.daly_full_apply.time.time", lambda: 1000.0)

    result = asyncio.run(
        apply_staged_settings(wire, staging, device_id="disarm", data_dir=tmp_path, now=1000.0)
    )
    assert result.ok
    assert wire.writes
    assert not staging.is_armed(1000.0)
