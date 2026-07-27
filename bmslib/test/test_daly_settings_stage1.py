"""Stage 1: read-only Daly legacy settings (0x50–0x63) with opt-in diagnostics.

Exercises frame parsing through public DalyBt / module parse helpers — not
copied formulas in the tests.
"""

import asyncio
import struct
import time
from unittest.mock import MagicMock

import pytest

from bmslib.bms import BmsSample, MIN_VALUE_EXPIRY
from bmslib.models import construct_bms
from bmslib.models.daly import (
    DalyBt,
    parse_production_date_payload,
    parse_rated_parameters_payload,
    parse_version_frames_payload,
)
from bmslib.mqtt_util import publish_hass_discovery, sample_desc
from bmslib.test.data import daly_fixtures

NORMAL_TELEMETRY_CMDS = frozenset({0x90, 0x93, 0x94})
EXTENDED_DIAGNOSTIC_CMDS = frozenset({0x50, 0x53, 0x62, 0x63})


def _make_daly(enable_diagnostics=False):
    return DalyBt(
        "00:11:22:33:44:55",
        name="daly",
        enable_daly_diagnostics=enable_diagnostics,
    )


def _normal_q_responses(cmd, num_responses=1):
    if cmd == 0x90:
        return daly_fixtures.SOC_SYNTHETIC_265V_5A["raw"]
    if cmd == 0x93:
        return daly_fixtures.STATUS_DSG_ON["raw"]
    if cmd == 0x94:
        return daly_fixtures.STATES_8CELL["raw"]
    raise AssertionError(f"unexpected cmd 0x{cmd:02x}")


@pytest.mark.parametrize("fx", daly_fixtures.ALL_RATED_PARAMS, ids=lambda fx: fx["name"])
def test_parse_rated_parameters_valid(fx):
    got = parse_rated_parameters_payload(fx["raw"])
    exp = fx["expected"]
    assert got["rated_capacity"] == pytest.approx(exp["rated_capacity"], abs=0.001)
    assert got["nominal_cell_voltage"] == pytest.approx(exp["nominal_cell_voltage"], abs=0.001)


@pytest.mark.parametrize("fx", daly_fixtures.ALL_PRODUCTION_DATES, ids=lambda fx: fx["name"])
def test_parse_production_date_valid(fx):
    assert parse_production_date_payload(fx["raw"]) == fx["expected"]["production_date"]


@pytest.mark.parametrize("fx", daly_fixtures.ALL_SOFTWARE_VERSIONS, ids=lambda fx: fx["name"])
def test_parse_software_version_valid(fx):
    assert parse_version_frames_payload(fx["frames"], "Daly software version") == \
        fx["expected"]["software_version"]


@pytest.mark.parametrize("fx", daly_fixtures.ALL_HARDWARE_VERSIONS, ids=lambda fx: fx["name"])
def test_parse_hardware_version_valid(fx):
    assert parse_version_frames_payload(fx["frames"], "Daly hardware version") == \
        fx["expected"]["hardware_version"]


def test_parse_rated_parameters_payload_length():
    with pytest.raises(ValueError, match="Daly rated parameters payload must be 8 bytes"):
        parse_rated_parameters_payload(b"\x00\x01")
    with pytest.raises(ValueError, match="Daly rated parameters payload must be 8 bytes"):
        parse_rated_parameters_payload(b"\x00" * 9)


def test_parse_rated_parameters_zero_capacity():
    raw = struct.pack(">L2xH", 0, 3200)
    with pytest.raises(ValueError, match="Daly rated capacity must be positive"):
        parse_rated_parameters_payload(raw)


def test_parse_rated_parameters_huge_capacity():
    raw = struct.pack(">L2xH", 2_000_001, 3200)
    with pytest.raises(ValueError, match="Daly rated capacity exceeds maximum"):
        parse_rated_parameters_payload(raw)


def test_parse_rated_parameters_voltage_too_low():
    raw = struct.pack(">L2xH", 100_000, 2499)
    with pytest.raises(ValueError, match="Daly nominal cell voltage out of range"):
        parse_rated_parameters_payload(raw)


def test_parse_rated_parameters_voltage_too_high():
    raw = struct.pack(">L2xH", 100_000, 4501)
    with pytest.raises(ValueError, match="Daly nominal cell voltage out of range"):
        parse_rated_parameters_payload(raw)


def test_parse_rated_parameters_rejects_non_bytes_like():
    with pytest.raises(TypeError, match="bytes-like"):
        parse_rated_parameters_payload(True)
    with pytest.raises(TypeError, match="bytes-like"):
        parse_rated_parameters_payload(0x50)
    with pytest.raises(TypeError, match="bytes-like"):
        parse_rated_parameters_payload("0004baf0")
    with pytest.raises(TypeError, match="bytes-like"):
        parse_rated_parameters_payload([0x00])


def test_parse_rated_parameters_accepts_bytearray():
    raw = bytearray(daly_fixtures.RATED_PARAMS_310AH_3200MV["raw"])
    got = parse_rated_parameters_payload(raw)
    assert got["rated_capacity"] == pytest.approx(310.0, abs=0.001)


def test_parse_rated_parameters_accepts_memoryview():
    raw = memoryview(daly_fixtures.RATED_PARAMS_310AH_3200MV["raw"])
    got = parse_rated_parameters_payload(raw)
    assert got["rated_capacity"] == pytest.approx(310.0, abs=0.001)


def test_parse_production_date_accepts_bytearray():
    raw = bytearray(daly_fixtures.PRODUCTION_DATE_2024_06_15["raw"])
    assert parse_production_date_payload(raw) == "2024-06-15"


def test_parse_version_frames_accepts_bytearray_frames():
    fx = daly_fixtures.SOFTWARE_VERSION_CAPTURED
    frames = tuple(bytearray(f) for f in fx["frames"])
    assert parse_version_frames_payload(frames, "Daly software version") == \
        fx["expected"]["software_version"]


def _notify_and_fetch(bms, cmd, payload_or_frames, fetch_coro):
    async def write_gatt_char(tx, msg):
        notify = bytearray()
        if isinstance(payload_or_frames, (list, tuple)):
            for fp in payload_or_frames:
                notify.extend(bytearray(
                    daly_fixtures.wrap_ble_response(cmd, bytes(fp))))
        else:
            notify.extend(bytearray(
                daly_fixtures.wrap_ble_response(cmd, bytes(payload_or_frames))))
        bms._notification_callback(None, notify)

    bms.UUID_TX = "fff2"
    bms.client = MagicMock()
    bms.client.write_gatt_char = write_gatt_char
    return asyncio.run(fetch_coro())


def test_fetch_rated_parameters_via_notification_bytearray():
    bms = _make_daly()
    fx = daly_fixtures.RATED_PARAMS_310AH_3200MV
    got = _notify_and_fetch(bms, 0x50, fx["raw"], bms.fetch_rated_parameters)
    assert got["rated_capacity"] == pytest.approx(310.0, abs=0.001)
    assert got["nominal_cell_voltage"] == pytest.approx(3.2, abs=0.001)
    assert type(bms._last_response) in (bytes, bytearray)


def test_fetch_software_version_via_notification_bytearray():
    bms = _make_daly()
    fx = daly_fixtures.SOFTWARE_VERSION_CAPTURED
    got = _notify_and_fetch(bms, 0x62, fx["frames"], bms.fetch_software_version)
    assert got == fx["expected"]["software_version"]


def test_fetch_hardware_version_via_notification_bytearray():
    bms = _make_daly()
    fx = daly_fixtures.HARDWARE_VERSION_CAPTURED
    got = _notify_and_fetch(bms, 0x63, fx["frames"], bms.fetch_hardware_version)
    assert got == fx["expected"]["hardware_version"]


def test_enable_daly_diagnostics_rejects_string_false():
    with pytest.raises(TypeError, match="enable_daly_diagnostics must be a bool"):
        DalyBt("00:11:22:33:44:55", name="daly", enable_daly_diagnostics="false")


def test_enable_daly_diagnostics_rejects_string_true():
    with pytest.raises(TypeError, match="enable_daly_diagnostics must be a bool"):
        DalyBt("00:11:22:33:44:55", name="daly", enable_daly_diagnostics="true")


def test_enable_daly_diagnostics_accepts_explicit_false():
    bms = DalyBt("00:11:22:33:44:55", name="daly", enable_daly_diagnostics=False)
    assert bms.enable_daly_diagnostics is False


def test_parse_production_date_payload_length():
    with pytest.raises(ValueError, match="Daly production date payload must be 8 bytes"):
        parse_production_date_payload(b"\x00\x00\x24")
    with pytest.raises(ValueError, match="Daly production date payload must be 8 bytes"):
        parse_production_date_payload(b"\x00" * 9)


def test_parse_production_date_invalid_month():
    raw = bytes.fromhex("0000240015000000")
    with pytest.raises(ValueError, match="Daly production date month invalid"):
        parse_production_date_payload(raw)


def test_parse_production_date_invalid_day():
    raw = bytes.fromhex("0000240632000000")
    with pytest.raises(ValueError, match="Daly production date day invalid"):
        parse_production_date_payload(raw)


def test_parse_production_date_invalid_leap_date():
    raw = bytes.fromhex("000017021d000000")  # 2023-02-29
    with pytest.raises(ValueError, match="Daly production date invalid calendar date"):
        parse_production_date_payload(raw)


def test_parse_production_date_rejects_non_bytes():
    with pytest.raises(TypeError):
        parse_production_date_payload(False)


def test_parse_version_rejects_bad_frame_count():
    fx = daly_fixtures.SOFTWARE_VERSION_CAPTURED
    with pytest.raises(ValueError, match="requires 2 frames"):
        parse_version_frames_payload([fx["frames"][0]], "Daly software version")


def test_parse_version_rejects_bad_sequence():
    fx = daly_fixtures.SOFTWARE_VERSION_CAPTURED
    frames = [fx["frames"][1], fx["frames"][0]]
    with pytest.raises(ValueError, match="frame sequence invalid"):
        parse_version_frames_payload(frames, "Daly software version")


def test_parse_version_rejects_non_printable():
    fx = daly_fixtures.SOFTWARE_VERSION_CAPTURED
    bad = bytearray(fx["frames"][0])
    bad[3] = 0
    frames = [bytes(bad), fx["frames"][1]]
    with pytest.raises(ValueError, match="non-printable ASCII"):
        parse_version_frames_payload(frames, "Daly software version")


@pytest.mark.parametrize("fx", daly_fixtures.ALL_RATED_PARAMS, ids=lambda fx: fx["name"])
def test_fetch_rated_parameters_via_public_method(fx):
    bms = _make_daly()

    async def fake_q(cmd, num_responses=1):
        assert cmd == 0x50
        return fx["raw"]

    bms._q = fake_q
    got = asyncio.run(bms.fetch_rated_parameters())
    exp = fx["expected"]
    assert got["rated_capacity"] == pytest.approx(exp["rated_capacity"], abs=0.001)
    assert got["nominal_cell_voltage"] == pytest.approx(exp["nominal_cell_voltage"], abs=0.001)


@pytest.mark.parametrize("fx", daly_fixtures.ALL_PRODUCTION_DATES, ids=lambda fx: fx["name"])
def test_fetch_production_date_via_public_method(fx):
    bms = _make_daly()

    async def fake_q(cmd, num_responses=1):
        assert cmd == 0x53
        return fx["raw"]

    bms._q = fake_q
    assert asyncio.run(bms.fetch_production_date()) == fx["expected"]["production_date"]


def test_fetch_default_skips_extended_diagnostics():
    bms = _make_daly()
    calls = []

    async def fake_q(cmd, num_responses=1):
        calls.append(cmd)
        return _normal_q_responses(cmd, num_responses)

    bms._q = fake_q
    asyncio.run(bms.fetch())
    assert set(calls) <= NORMAL_TELEMETRY_CMDS
    assert not set(calls) & EXTENDED_DIAGNOSTIC_CMDS


def test_fetch_disabled_diagnostics_on_second_sample():
    bms = _make_daly()
    calls = []

    async def fake_q(cmd, num_responses=1):
        calls.append(cmd)
        return _normal_q_responses(cmd, num_responses)

    bms._q = fake_q
    asyncio.run(bms.fetch())
    asyncio.run(bms.fetch())
    assert set(calls) <= NORMAL_TELEMETRY_CMDS


def test_fetch_applies_diagnostics_when_enabled():
    bms = _make_daly(enable_diagnostics=True)
    rated = daly_fixtures.RATED_PARAMS_310AH_3200MV
    prod = daly_fixtures.PRODUCTION_DATE_2024_06_15
    sw = daly_fixtures.SOFTWARE_VERSION_CAPTURED
    hw = daly_fixtures.HARDWARE_VERSION_CAPTURED

    async def fake_q(cmd, num_responses=1):
        if cmd == 0x50:
            return rated["raw"]
        if cmd == 0x53:
            return prod["raw"]
        if cmd == 0x62 and num_responses == 2:
            return sw["frames"]
        if cmd == 0x63 and num_responses == 2:
            return hw["frames"]
        return _normal_q_responses(cmd, num_responses)

    bms._q = fake_q
    sample = asyncio.run(bms.fetch())

    assert sample.rated_capacity == pytest.approx(310.0, abs=0.001)
    assert sample.nominal_cell_voltage == pytest.approx(3.2, abs=0.001)
    assert sample.production_date == "2024-06-15"
    assert sample.software_version == "20210222-1.01T"
    assert sample.hardware_version == "DL-BMS-R32-01E"
    assert sample.voltage == pytest.approx(26.4, abs=0.01)
    assert sample.switches == dict(charge=True, discharge=True)


def test_enabled_extended_commands_before_final_normal_telemetry():
    """Extended diagnostics must complete (or cache-fail) before fresh 0x93/0x90."""
    bms = _make_daly(enable_diagnostics=True)
    calls = []

    async def fake_q(cmd, num_responses=1):
        calls.append(cmd)
        if cmd in EXTENDED_DIAGNOSTIC_CMDS:
            raise TimeoutError(f"timeout cmd=0x{cmd:02x}")
        return _normal_q_responses(cmd, num_responses)

    bms._q = fake_q
    asyncio.run(bms.fetch())

    ext_positions = [i for i, c in enumerate(calls) if c in EXTENDED_DIAGNOSTIC_CMDS]
    tele_positions = [i for i, c in enumerate(calls) if c in (0x93, 0x90)]
    assert ext_positions
    assert tele_positions
    assert max(ext_positions) < min(tele_positions)


def test_enabled_slow_diagnostic_timeouts_keep_sample_fresh_for_expiry_policy():
    """Simulate slow unsupported diagnostics; sample must stay within default 20s policy."""
    expire_after_seconds = 20
    diag_delay = 0.25  # 4 timeouts ≈ 1.0s — reproduces stale-sample class without 12s waits
    bms = _make_daly(enable_diagnostics=True)

    async def slow_fake_q(cmd, num_responses=1):
        if cmd in EXTENDED_DIAGNOSTIC_CMDS:
            await asyncio.sleep(diag_delay)
            raise TimeoutError(f"timeout cmd=0x{cmd:02x}")
        return _normal_q_responses(cmd, num_responses)

    bms._q = slow_fake_q
    sample = asyncio.run(bms.fetch())
    t_now = time.time()
    age = t_now - sample.timestamp
    threshold = max(expire_after_seconds, MIN_VALUE_EXPIRY)
    assert sample.timestamp >= t_now - threshold
    # Telemetry is acquired after diagnostics; age must not include diagnostic wall time.
    assert age < diag_delay + 0.15


def test_apply_cached_diagnostics_performs_no_io_after_telemetry():
    """Second fetch uses cached diagnostic failures — no extended commands at all."""
    bms = _make_daly(enable_diagnostics=True)
    calls = []

    async def fake_q(cmd, num_responses=1):
        calls.append(cmd)
        if cmd in EXTENDED_DIAGNOSTIC_CMDS:
            raise TimeoutError(f"timeout cmd=0x{cmd:02x}")
        return _normal_q_responses(cmd, num_responses)

    bms._q = fake_q
    asyncio.run(bms.fetch())  # populate failure cache
    calls.clear()
    sample = asyncio.run(bms.fetch())
    assert not set(calls) & EXTENDED_DIAGNOSTIC_CMDS
    assert sample.voltage == pytest.approx(26.4, abs=0.01)


def test_failed_diagnostic_cached_no_repeat_on_fetch():
    bms = _make_daly(enable_diagnostics=True)
    calls = []

    async def fake_q(cmd, num_responses=1):
        calls.append(cmd)
        if cmd in EXTENDED_DIAGNOSTIC_CMDS:
            raise TimeoutError(f"timeout cmd=0x{cmd:02x}")
        return _normal_q_responses(cmd, num_responses)

    bms._q = fake_q
    asyncio.run(bms.fetch())
    first_extended = [c for c in calls if c in EXTENDED_DIAGNOSTIC_CMDS]
    assert first_extended == [0x50, 0x53, 0x62, 0x63]

    asyncio.run(bms.fetch())
    second_extended = [c for c in calls if c in EXTENDED_DIAGNOSTIC_CMDS]
    assert second_extended == first_extended


def test_diagnostics_cached_within_hour():
    bms = _make_daly(enable_diagnostics=True)
    rated = daly_fixtures.RATED_PARAMS_310AH_3200MV
    calls = []

    async def fake_q(cmd, num_responses=1):
        calls.append(cmd)
        if cmd == 0x50:
            return rated["raw"]
        raise AssertionError(f"unexpected cmd 0x{cmd:02x}")

    bms._q = fake_q
    asyncio.run(bms.fetch_rated_parameters_cached())
    asyncio.run(bms.fetch_rated_parameters_cached())
    assert calls == [0x50]


def test_construct_bms_threads_enable_daly_diagnostics():
    discovered = [MagicMock(address="00:11:22:33:44:55", name="daly")]
    bms = construct_bms(
        {
            "address": "00:11:22:33:44:55",
            "type": "daly",
            "alias": "pack1",
            "enable_daly_diagnostics": True,
        },
        verbose_log=False,
        bt_discovered_devices=discovered,
    )
    assert bms.enable_daly_diagnostics is True


def test_construct_bms_other_types_ignore_diagnostics_flag():
    discovered = [MagicMock(address="00:11:22:33:44:55", name="jbd")]
    bms = construct_bms(
        {
            "address": "00:11:22:33:44:55",
            "type": "jbd",
            "alias": "pack1",
            "enable_daly_diagnostics": True,
        },
        verbose_log=False,
        bt_discovered_devices=discovered,
    )
    assert not hasattr(bms, "enable_daly_diagnostics")


def test_bms_sample_exposes_diagnostic_fields():
    s = BmsSample(
        voltage=26.0,
        current=0.0,
        rated_capacity=310.0,
        nominal_cell_voltage=3.2,
        production_date="2024-06-15",
        software_version="20210222-1.01T",
        hardware_version="DL-BMS-R32-01E",
    )
    assert s.software_version == "20210222-1.01T"
    assert s.hardware_version == "DL-BMS-R32-01E"


def test_sample_desc_includes_daly_diagnostics_without_command_topics():
    keys = (
        "bms/rated_capacity",
        "bms/nominal_cell_voltage",
        "bms/production_date",
        "bms/software_version",
        "bms/hardware_version",
    )
    for key in keys:
        assert key in sample_desc
        assert "command_topic" not in sample_desc[key]


def test_hass_discovery_diagnostics_have_no_command_topic():
    sample = BmsSample(
        voltage=26.0,
        current=0.0,
        rated_capacity=310.0,
        nominal_cell_voltage=3.2,
        production_date="2024-06-15",
        software_version="20210222-1.01T",
        hardware_version="DL-BMS-R32-01E",
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
    diagnostic_fragments = (
        "rated_capacity",
        "nominal_cell_voltage",
        "production_date",
        "software_version",
        "hardware_version",
    )
    for topic, payload in captured.items():
        if any(frag in topic for frag in diagnostic_fragments):
            assert "command_topic" not in payload
            assert '"command_topic"' not in payload
