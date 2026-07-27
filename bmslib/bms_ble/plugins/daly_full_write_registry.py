"""Allowlisted Daly D2 and limited protocol-81 write registry."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Final, Mapping, Optional

from bmslib.bms_ble.plugins.daly_full_decode import (
    ACTIVE_BALANCE_SWITCH,
    BATTERY_CHEMISTRY,
    COMMUNICATION_METHOD,
    INVERTER_MANUFACTURER_WRITE,
    INVERTER_SELF_IDENTIFY_LABEL,
    INVERTER_SELF_IDENTIFY_RAW,
    MOS_SWITCH,
)
from bmslib.bms_ble.plugins.daly_full_protocol import PROTOCOL_UNIT_81, PROTOCOL_UNIT_D2

HIBERNATE_SPECIAL_RAW: Final[int] = 65535
RESTART_ADDRESS: Final[int] = 0x00F0
RESTART_FIELD_KEY: Final[str] = "system_restart"
FORCE_START_FIELD_KEY: Final[str] = "force_start"
FORCE_START_UNSUPPORTED_RAW: Final[int] = 65535
CHARGE_MOS_81_ADDRESS: Final[int] = 0x0121
ACTIVE_BALANCE_81_ADDRESS: Final[int] = 0x0119
FORCE_START_81_ADDRESS: Final[int] = 0x012E
CHARGE_CURRENT_ALARM_MAX_A: Final[float] = 3000.0
DISCHARGE_CURRENT_ALARM_MAX_A: Final[float] = 3553.5
TEMPERATURE_ALARM_MIN_C: Final[float] = -40.0
TEMPERATURE_ALARM_MAX_C: Final[float] = 125.0


class EntityType(str, Enum):
    NUMBER = "number"
    SELECT = "select"
    BUTTON = "button"


@dataclass(frozen=True)
class DalyWriteField:
    field_id: str
    key: str
    address: int
    entity_type: EntityType
    danger_tier: int
    readback_key: str
    evidence: str
    unit: Optional[str] = None
    precision: int = 0
    options: Optional[tuple[str, ...]] = None
    display_name: Optional[str] = None
    requires_arm: bool = False
    encode: Callable[[Any], int] = lambda v: int(v)
    validate: Callable[[Any], None] = lambda v: None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    protocol_unit: int = PROTOCOL_UNIT_D2
    readback_d2_address: Optional[int] = None


def _reject_non_numeric(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean not allowed for numeric field")
    if isinstance(value, (int, float)):
        f = float(value)
        if not math.isfinite(f):
            raise ValueError("non-finite numeric value")
        return f
    raise ValueError("numeric value required")


def _as_integer_numeric(value: Any) -> int:
    f = _reject_non_numeric(value)
    if f != int(f):
        raise ValueError("integer value required")
    return int(f)


def _scaled_integer(value: Any, scale: int) -> int:
    f = _reject_non_numeric(value)
    product = f * scale
    if abs(product - round(product)) > 1e-9:
        raise ValueError("value not representable at required precision")
    return int(round(product))


def _encode_u16_div10(value: Any) -> int:
    raw = _scaled_integer(value, 10)
    if not (0 <= raw <= 0xFFFF):
        raise ValueError("value out of representable range")
    return raw


def _encode_u16_div1000(value: Any) -> int:
    raw = _scaled_integer(value, 1000)
    if not (0 <= raw <= 0xFFFF):
        raise ValueError("value out of representable range")
    return raw


def _encode_u16_identity(value: Any) -> int:
    raw = _as_integer_numeric(value)
    if not (0 <= raw <= 0xFFFF):
        raise ValueError("value out of representable range")
    return raw


def _validate_charge_current_alarm(value: Any) -> None:
    v = _reject_non_numeric(value)
    if v < 0:
        raise ValueError("charge current must be >= 0")
    if v > CHARGE_CURRENT_ALARM_MAX_A:
        raise ValueError("charge current out of range")


def _validate_discharge_current_alarm(value: Any) -> None:
    v = _reject_non_numeric(value)
    if v < 0:
        raise ValueError("discharge current must be >= 0")
    if v > DISCHARGE_CURRENT_ALARM_MAX_A:
        raise ValueError("discharge current out of range")


def _validate_temperature_alarm(value: Any) -> None:
    v = _reject_non_numeric(value)
    if not (TEMPERATURE_ALARM_MIN_C <= v <= TEMPERATURE_ALARM_MAX_C):
        raise ValueError("temperature alarm must be -40..125 C")


def _encode_charge_current(value: Any) -> int:
    v = _reject_non_numeric(value)
    if v < 0:
        raise ValueError("charge current must be >= 0")
    raw = 30000 - _scaled_integer(value, 10)
    if not (0 <= raw <= 0xFFFF):
        raise ValueError("charge current out of range")
    return raw


def _encode_discharge_current(value: Any) -> int:
    v = _reject_non_numeric(value)
    if v < 0:
        raise ValueError("discharge current must be >= 0")
    raw = 30000 + _scaled_integer(value, 10)
    if not (0 <= raw <= 0xFFFF):
        raise ValueError("discharge current out of range")
    return raw


def _encode_temp_bias(value: Any) -> int:
    raw = _scaled_integer(value, 1) + 40
    if not (0 <= raw <= 0xFFFF):
        raise ValueError("temperature out of range")
    return raw


def _validate_soc(value: Any) -> None:
    v = _reject_non_numeric(value)
    if not (0 <= v <= 100):
        raise ValueError("SOC must be 0..100")


def _validate_capacity(value: Any) -> None:
    v = _reject_non_numeric(value)
    if v <= 0:
        raise ValueError("capacity must be > 0")


def _validate_total_voltage(value: Any) -> None:
    v = _reject_non_numeric(value)
    if v <= 0:
        raise ValueError("total voltage must be > 0")
    raw = _scaled_integer(value, 10)
    if raw > 0xFFFF:
        raise ValueError("total voltage out of range")


def _validate_cell_voltage(value: Any) -> None:
    v = _reject_non_numeric(value)
    if not (0 < v <= 5.0):
        raise ValueError("cell voltage must be >0 and <=5.0 V")


def _validate_topology_cells(value: Any) -> None:
    v = _as_integer_numeric(value)
    if not (1 <= v <= 32):
        raise ValueError("cell count must be 1..32")


def _validate_topology_temps(value: Any) -> None:
    v = _as_integer_numeric(value)
    if not (0 <= v <= 16):
        raise ValueError("temperature sensor count must be 0..16")


def _validate_hibernate(value: Any) -> None:
    v = _as_integer_numeric(value)
    if not (30 <= v <= HIBERNATE_SPECIAL_RAW):
        raise ValueError("hibernate wait must be 30..65535 seconds")


def _encode_hibernate(value: Any) -> int:
    return _encode_u16_identity(value)


def _validate_enum_option(value: Any, table: Mapping[int, str]) -> None:
    if isinstance(value, str):
        if value not in table.values():
            raise ValueError("invalid enum option")
        return
    if isinstance(value, bool):
        raise ValueError("boolean not allowed for enum field")
    if isinstance(value, float) and value != int(value):
        raise ValueError("invalid enum raw value")
    raw = int(value)
    if raw not in table:
        raise ValueError("invalid enum raw value")


def _encode_enum(value: Any, table: Mapping[int, str], *, label_to_raw: Optional[dict[str, int]] = None) -> int:
    if isinstance(value, str):
        if label_to_raw and value in label_to_raw:
            return label_to_raw[value]
        rev = {v: k for k, v in table.items()}
        if value not in rev:
            raise ValueError("invalid enum option")
        return rev[value]
    if isinstance(value, bool):
        raise ValueError("boolean not allowed for enum field")
    if isinstance(value, float):
        if value != int(value):
            raise ValueError("invalid enum raw value")
        raw = int(value)
    else:
        raw = int(value)
    if raw not in table:
        raise ValueError("invalid enum raw value")
    return raw


_CHEM_OPTIONS = tuple(BATTERY_CHEMISTRY.values())
_COMM_OPTIONS = tuple(v for k, v in COMMUNICATION_METHOD.items() if k != 65535)
_INVERTER_OPTIONS = tuple(INVERTER_MANUFACTURER_WRITE.values()) + (INVERTER_SELF_IDENTIFY_LABEL,)
_INVERTER_LABEL_TO_RAW = {v: k for k, v in INVERTER_MANUFACTURER_WRITE.items()}
_INVERTER_LABEL_TO_RAW[INVERTER_SELF_IDENTIFY_LABEL] = INVERTER_SELF_IDENTIFY_RAW
_INVERTER_VALIDATE_TABLE = dict(INVERTER_MANUFACTURER_WRITE)
_INVERTER_VALIDATE_TABLE[INVERTER_SELF_IDENTIFY_RAW] = INVERTER_SELF_IDENTIFY_LABEL
_MOS_OPTIONS = tuple(MOS_SWITCH.values())
_BALANCE_OPTIONS = tuple(ACTIVE_BALANCE_SWITCH.values())


WRITE_FIELDS: Final[tuple[DalyWriteField, ...]] = (
    DalyWriteField("D2-BAT-CAP", "rated_capacity_ah", 0x0080, EntityType.NUMBER, 1, "rated_capacity_ah",
                   "BatteryParametersSettingFragment.java:1684-1764", "Ah", 1,
                   encode=_encode_u16_div10, validate=_validate_capacity, min_value=0.1, max_value=6553.5),
    DalyWriteField("D2-BAT-TYPE", "battery_chemistry", 0x0089, EntityType.SELECT, 2, "battery_chemistry",
                   "BatteryParametersSettingFragment.java:1271-1317", options=_CHEM_OPTIONS,
                   encode=lambda v: _encode_enum(v, BATTERY_CHEMISTRY),
                   validate=lambda v: _validate_enum_option(v, BATTERY_CHEMISTRY)),
    DalyWriteField("D2-HIB-WAIT", "hibernate_wait_time_s", 0x008A, EntityType.NUMBER, 1, "hibernate_wait_time_s",
                   "BatteryParametersSettingFragment.java:1843-1885", "s", 0,
                   encode=_encode_hibernate, validate=_validate_hibernate,
                   min_value=30, max_value=float(HIBERNATE_SPECIAL_RAW)),
    DalyWriteField("D2-SOC", "soc_setting_percent", 0x00A7, EntityType.NUMBER, 2, "soc_setting_percent",
                   "BatteryParametersSettingFragment.java:1800-1840", "%", 1,
                   encode=_encode_u16_div10, validate=_validate_soc, min_value=0, max_value=100),
    DalyWriteField("D2-COMM", "communication_method", 0x00D1, EntityType.SELECT, 2, "communication_method",
                   "BatteryParametersSettingFragment.java:1324-1344", options=_COMM_OPTIONS,
                   encode=lambda v: _encode_enum(v, COMMUNICATION_METHOD),
                   validate=lambda v: _validate_enum_option(v, {0: "RS485", 1: "CAN"})),
    DalyWriteField("D2-INVERTER", "inverter_manufacturer", 0x00D2, EntityType.SELECT, 2, "inverter_manufacturer",
                   "BatteryParametersSettingFragment.java:1414-1443", options=_INVERTER_OPTIONS,
                   encode=lambda v: _encode_enum(v, _INVERTER_VALIDATE_TABLE, label_to_raw=_INVERTER_LABEL_TO_RAW),
                   validate=lambda v: _validate_enum_option(v, _INVERTER_VALIDATE_TABLE)),
    DalyWriteField("D2-BOARD-CELLS", "collection_board_1_cell_count", 0x0083, EntityType.NUMBER, 2,
                   "collection_board_1_cell_count", "VoltageSettingFragment.java:1413-1440", None, 0,
                   encode=_encode_u16_identity, validate=_validate_topology_cells, min_value=1, max_value=32),
    DalyWriteField("D2-BOARD-TEMPS", "collection_board_1_temp_sensor_count", 0x0086, EntityType.NUMBER, 2,
                   "collection_board_1_temp_sensor_count", "TemperatureSettingFragment.java:967-993", None, 0,
                   encode=_encode_u16_identity, validate=_validate_topology_temps, min_value=0, max_value=16),
    DalyWriteField("D2-CELL-HI", "cell_voltage_high_level_2_alarm_v", 0x008C, EntityType.NUMBER, 2,
                   "cell_voltage_high_level_2_alarm_v", "VoltageSettingFragment.java:891-907", "V", 3,
                   encode=_encode_u16_div1000, validate=_validate_cell_voltage),
    DalyWriteField("D2-CELL-LO", "cell_voltage_low_level_2_alarm_v", 0x008E, EntityType.NUMBER, 2,
                   "cell_voltage_low_level_2_alarm_v", "VoltageSettingFragment.java:910-989", "V", 3,
                   encode=_encode_u16_div1000, validate=_validate_cell_voltage),
    DalyWriteField("D2-TOTAL-HI", "total_voltage_high_level_2_alarm_v", 0x0090, EntityType.NUMBER, 2,
                   "total_voltage_high_level_2_alarm_v", "VoltageSettingFragment.java:1053-1064", "V", 1,
                   encode=_encode_u16_div10, validate=_validate_total_voltage),
    DalyWriteField("D2-TOTAL-LO", "total_voltage_low_level_2_alarm_v", 0x0092, EntityType.NUMBER, 2,
                   "total_voltage_low_level_2_alarm_v", "VoltageSettingFragment.java:1126-1137", "V", 1,
                   encode=_encode_u16_div10, validate=_validate_total_voltage),
    DalyWriteField("D2-CHG-I-L1", "charge_current_high_level_1_alarm_a", 0x0093, EntityType.NUMBER, 2,
                   "charge_current_high_level_1_alarm_a", "VoltageSettingFragment.java:1224-1251", "A", 1,
                   encode=_encode_charge_current, validate=_validate_charge_current_alarm,
                   min_value=0, max_value=CHARGE_CURRENT_ALARM_MAX_A),
    DalyWriteField("D2-CHG-I-L2", "charge_current_high_level_2_alarm_a", 0x0094, EntityType.NUMBER, 2,
                   "charge_current_high_level_2_alarm_a", "VoltageSettingFragment.java:1253-1316", "A", 1,
                   encode=_encode_charge_current, validate=_validate_charge_current_alarm,
                   min_value=0, max_value=CHARGE_CURRENT_ALARM_MAX_A),
    DalyWriteField("D2-DCHG-I-L2", "discharge_current_high_level_2_alarm_a", 0x0096, EntityType.NUMBER, 2,
                   "discharge_current_high_level_2_alarm_a", "VoltageSettingFragment.java:1386-1397", "A", 1,
                   encode=_encode_discharge_current, validate=_validate_discharge_current_alarm,
                   min_value=0, max_value=DISCHARGE_CURRENT_ALARM_MAX_A),
    DalyWriteField("D2-CHG-T-HI", "charge_temperature_high_level_2_alarm_c", 0x0098, EntityType.NUMBER, 2,
                   "charge_temperature_high_level_2_alarm_c", "TemperatureSettingFragment.java:953-964", "°C", 1,
                   encode=_encode_temp_bias, validate=_validate_temperature_alarm,
                   min_value=TEMPERATURE_ALARM_MIN_C, max_value=TEMPERATURE_ALARM_MAX_C),
    DalyWriteField("D2-CHG-T-LO", "charge_temperature_low_level_2_alarm_c", 0x009A, EntityType.NUMBER, 2,
                   "charge_temperature_low_level_2_alarm_c", "TemperatureSettingFragment.java:826-885", "°C", 1,
                   encode=_encode_temp_bias, validate=_validate_temperature_alarm,
                   min_value=TEMPERATURE_ALARM_MIN_C, max_value=TEMPERATURE_ALARM_MAX_C),
    DalyWriteField("D2-DCHG-T-HI", "discharge_temperature_high_level_2_alarm_c", 0x009C, EntityType.NUMBER, 2,
                   "discharge_temperature_high_level_2_alarm_c", "TemperatureSettingFragment.java:755-823", "°C", 1,
                   encode=_encode_temp_bias, validate=_validate_temperature_alarm,
                   min_value=TEMPERATURE_ALARM_MIN_C, max_value=TEMPERATURE_ALARM_MAX_C),
    DalyWriteField("D2-DCHG-T-LO", "discharge_temperature_low_level_2_alarm_c", 0x009E, EntityType.NUMBER, 2,
                   "discharge_temperature_low_level_2_alarm_c", "TemperatureSettingFragment.java:684-752", "°C", 1,
                   encode=_encode_temp_bias, validate=_validate_temperature_alarm,
                   min_value=TEMPERATURE_ALARM_MIN_C, max_value=TEMPERATURE_ALARM_MAX_C),
    DalyWriteField("D2-V-DIFF", "cell_voltage_difference_level_2_alarm_v", 0x00A0, EntityType.NUMBER, 2,
                   "cell_voltage_difference_level_2_alarm_v", "VoltageSettingFragment.java:1140-1221", "V", 3,
                   encode=_encode_u16_div1000, validate=_validate_cell_voltage),
    DalyWriteField("D2-T-DIFF", "temperature_difference_level_2_alarm_c", 0x00A2, EntityType.NUMBER, 2,
                   "temperature_difference_level_2_alarm_c", "TemperatureSettingFragment.java:613-681", "°C", 1,
                   encode=_encode_u16_identity),
    DalyWriteField("D2-BAL-START", "balance_start_voltage_v", 0x00A3, EntityType.NUMBER, 2,
                   "balance_start_voltage_v", "EqualizationSettingFragment.java:475-507", "V", 3,
                   encode=_encode_u16_div1000, validate=_validate_cell_voltage),
    DalyWriteField("D2-BAL-DIFF", "balance_start_voltage_difference_v", 0x00A4, EntityType.NUMBER, 2,
                   "balance_start_voltage_difference_v", "EqualizationSettingFragment.java:510-537", "V", 3,
                   encode=_encode_u16_div1000, validate=_validate_cell_voltage),
    DalyWriteField("81-CHG-MOS", "charge_mos_switch_control", CHARGE_MOS_81_ADDRESS, EntityType.SELECT, 3,
                   "charge_mos_switch_control", "ControlSettingFragment.java:1177-1268",
                   protocol_unit=PROTOCOL_UNIT_81, readback_d2_address=0x00A5, options=_MOS_OPTIONS,
                   display_name="Charge MOS", requires_arm=True,
                   encode=lambda v: _encode_enum(v, MOS_SWITCH),
                   validate=lambda v: _validate_enum_option(v, MOS_SWITCH)),
    DalyWriteField("81-ACT-BAL", "active_balance_switch", ACTIVE_BALANCE_81_ADDRESS, EntityType.SELECT, 3,
                   "active_balance_switch", "ControlSettingFragment.java:1177-1268",
                   protocol_unit=PROTOCOL_UNIT_81, readback_d2_address=0x00CF, options=_BALANCE_OPTIONS,
                   display_name="Active Balance", requires_arm=True,
                   encode=lambda v: _encode_enum(v, ACTIVE_BALANCE_SWITCH),
                   validate=lambda v: _validate_enum_option(v, ACTIVE_BALANCE_SWITCH)),
    DalyWriteField("D2-DCHG-MOS", "discharge_mos_switch_control", 0x00A6, EntityType.SELECT, 3,
                   "discharge_mos_switch_control", "ControlSettingFragment.java:1177-1268",
                   options=_MOS_OPTIONS, display_name="Discharge MOS",
                   requires_arm=True,
                   encode=lambda v: _encode_enum(v, MOS_SWITCH),
                   validate=lambda v: _validate_enum_option(v, MOS_SWITCH)),
    DalyWriteField("D2-RESTART", RESTART_FIELD_KEY, RESTART_ADDRESS, EntityType.BUTTON, 3,
                   RESTART_FIELD_KEY, "ControlSettingFragment.java:1047-1076",
                   requires_arm=True, encode=lambda _v: 0),
    DalyWriteField("81-FORCE-START", FORCE_START_FIELD_KEY, FORCE_START_81_ADDRESS, EntityType.BUTTON, 3,
                   "force_start_switch", "ControlSettingFragment.java:1177-1268",
                   protocol_unit=PROTOCOL_UNIT_81, readback_d2_address=0x00D7, display_name="Force Start",
                   requires_arm=True, encode=lambda _v: 1),
)

def readback_register_address(field: DalyWriteField) -> int:
    if field.readback_d2_address is not None:
        return field.readback_d2_address
    return field.address

WRITE_FIELDS_BY_KEY: Final[dict[str, DalyWriteField]] = {f.key: f for f in WRITE_FIELDS}
WRITE_FIELDS_BY_ID: Final[dict[str, DalyWriteField]] = {f.field_id: f for f in WRITE_FIELDS}
ALLOWED_D2_WRITE_ADDRESSES: Final[frozenset[int]] = frozenset(
    f.address for f in WRITE_FIELDS if f.protocol_unit == PROTOCOL_UNIT_D2
)
ALLOWED_81_WRITE_ADDRESSES: Final[frozenset[int]] = frozenset(
    f.address for f in WRITE_FIELDS if f.protocol_unit == PROTOCOL_UNIT_81
)
ALLOWED_WRITE_ADDRESSES: Final[frozenset[int]] = ALLOWED_D2_WRITE_ADDRESSES
EXCLUDED_WRITE_ADDRESSES: Final[frozenset[int]] = frozenset({0x0580, 0x0508, 0x050A, 0x050C, 0x0512, 0x0513, 0x0515, 0x0517, 0x050F})


def _num(ctx: Mapping[str, Any], key: str) -> Optional[float]:
    if key not in ctx:
        return None
    val = ctx[key]
    if isinstance(val, bool):
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


def _require_hi_gt_lo(hi: Optional[float], lo: Optional[float], label: str) -> None:
    if hi is None or lo is None:
        return
    if hi <= lo:
        raise ValueError("%s high alarm must exceed low alarm" % label)


def _require_l2_beyond_l1_high(l2: Optional[float], l1: Optional[float], label: str) -> None:
    if l2 is None or l1 is None:
        return
    if l2 <= l1:
        raise ValueError("%s level-2 high alarm must exceed level-1 high alarm" % label)


def _require_l2_beyond_l1_low(l2: Optional[float], l1: Optional[float], label: str) -> None:
    if l2 is None or l1 is None:
        return
    if l2 >= l1:
        raise ValueError("%s level-2 low alarm must be below level-1 low alarm" % label)


def _require_l2_gte_l1_mag(l2: Optional[float], l1: Optional[float], label: str) -> None:
    if l2 is None or l1 is None:
        return
    if abs(l2) < abs(l1):
        raise ValueError("%s level-2 alarm magnitude must be >= level-1" % label)


def validate_cross_fields(prospective: Mapping[str, Any], *, baseline: Optional[Mapping[str, Any]] = None) -> None:
    ctx = dict(baseline or {})
    ctx.update(prospective)

    _require_hi_gt_lo(
        _num(ctx, "cell_voltage_high_level_2_alarm_v"),
        _num(ctx, "cell_voltage_low_level_2_alarm_v"),
        "cell",
    )
    _require_hi_gt_lo(
        _num(ctx, "total_voltage_high_level_2_alarm_v"),
        _num(ctx, "total_voltage_low_level_2_alarm_v"),
        "total",
    )
    _require_hi_gt_lo(
        _num(ctx, "charge_temperature_high_level_2_alarm_c"),
        _num(ctx, "charge_temperature_low_level_2_alarm_c"),
        "charge temperature",
    )
    _require_hi_gt_lo(
        _num(ctx, "discharge_temperature_high_level_2_alarm_c"),
        _num(ctx, "discharge_temperature_low_level_2_alarm_c"),
        "discharge temperature",
    )

    _require_l2_beyond_l1_high(
        _num(ctx, "cell_voltage_high_level_2_alarm_v"),
        _num(ctx, "cell_voltage_high_level_1_alarm_v"),
        "cell voltage",
    )
    _require_l2_beyond_l1_low(
        _num(ctx, "cell_voltage_low_level_2_alarm_v"),
        _num(ctx, "cell_voltage_low_level_1_alarm_v"),
        "cell voltage",
    )
    _require_l2_beyond_l1_high(
        _num(ctx, "total_voltage_high_level_2_alarm_v"),
        _num(ctx, "total_voltage_high_level_1_alarm_v"),
        "total voltage",
    )
    _require_l2_beyond_l1_low(
        _num(ctx, "total_voltage_low_level_2_alarm_v"),
        _num(ctx, "total_voltage_low_level_1_alarm_v"),
        "total voltage",
    )

    _require_l2_gte_l1_mag(
        _num(ctx, "charge_current_high_level_2_alarm_a"),
        _num(ctx, "charge_current_high_level_1_alarm_a"),
        "charge current",
    )
    _require_l2_gte_l1_mag(
        _num(ctx, "discharge_current_high_level_2_alarm_a"),
        _num(ctx, "discharge_current_high_level_1_alarm_a"),
        "discharge current",
    )

    _require_l2_beyond_l1_high(
        _num(ctx, "charge_temperature_high_level_2_alarm_c"),
        _num(ctx, "charge_temperature_high_level_1_alarm_c"),
        "charge temperature",
    )
    _require_l2_beyond_l1_low(
        _num(ctx, "charge_temperature_low_level_2_alarm_c"),
        _num(ctx, "charge_temperature_low_level_1_alarm_c"),
        "charge temperature",
    )
    _require_l2_beyond_l1_high(
        _num(ctx, "discharge_temperature_high_level_2_alarm_c"),
        _num(ctx, "discharge_temperature_high_level_1_alarm_c"),
        "discharge temperature",
    )
    _require_l2_beyond_l1_low(
        _num(ctx, "discharge_temperature_low_level_2_alarm_c"),
        _num(ctx, "discharge_temperature_low_level_1_alarm_c"),
        "discharge temperature",
    )

    vdiff_l2 = _num(ctx, "cell_voltage_difference_level_2_alarm_v")
    vdiff_l1 = _num(ctx, "cell_voltage_difference_level_1_alarm_v")
    if vdiff_l2 is not None and vdiff_l1 is not None and vdiff_l2 < vdiff_l1:
        raise ValueError("cell voltage difference level-2 must be >= level-1")

    tdiff_l2 = _num(ctx, "temperature_difference_level_2_alarm_c")
    tdiff_l1 = _num(ctx, "temperature_difference_level_1_alarm_c")
    if tdiff_l2 is not None and tdiff_l1 is not None and tdiff_l2 < tdiff_l1:
        raise ValueError("temperature difference level-2 must be >= level-1")

    start = _num(ctx, "balance_start_voltage_v")
    diff = _num(ctx, "balance_start_voltage_difference_v")
    if start is not None and diff is not None:
        if diff <= 0 or diff >= start:
            raise ValueError("balance delta must be positive and less than start voltage")
        if start > 5.0 or diff > 5.0:
            raise ValueError("balance voltage thresholds must be within cell voltage bounds")


def validate_prospective_configuration(
    baseline: Mapping[str, Any],
    staged: Mapping[str, Any],
) -> None:
    prospective = dict(baseline)
    prospective.update(staged)
    validate_cross_fields(prospective, baseline=baseline)


def encode_field(field: DalyWriteField, value: Any) -> int:
    field.validate(value)
    return field.encode(value)


def build_write_plan(
    current: Mapping[str, Any],
    staged: Mapping[str, Any],
    *,
    armed: bool,
) -> list[tuple[DalyWriteField, int, Any]]:
    validate_prospective_configuration(current, staged)
    plan: list[tuple[DalyWriteField, int, Any]] = []
    for key, value in staged.items():
        field = WRITE_FIELDS_BY_KEY.get(key)
        if field is None:
            raise ValueError("unknown staged field %r" % key)
        if field.entity_type == EntityType.BUTTON:
            raise ValueError("button fields cannot be staged")
        if field.requires_arm and not armed:
            raise ValueError("advanced operations require arm")
        raw = encode_field(field, value)
        cur_raw = None
        if key in current:
            cur_raw = encode_field(field, current[key])
        if cur_raw == raw:
            continue
        plan.append((field, raw, value))
    return plan
