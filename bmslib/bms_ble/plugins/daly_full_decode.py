"""Decode Daly official-app D2 settings blocks (0x80–0xCF, 0xD0–0xED).

Facts derived from clean-room APK analysis for interoperability only.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Final, Mapping, Optional

EXTRA_SENSOR_EXPIRY_SECONDS: Final[int] = 7200  # hourly readout + margin

# --- enum tables (proven unless noted) ---

BATTERY_CHEMISTRY: Final[dict[int, str]] = {
    0: "LiFePO4",
    1: "Ternary Lithium",
    2: "Lithium Titanate",
    3: "Sodium-ion",  # writer only; parser labels 0-2
}

MOS_SWITCH: Final[dict[int, str]] = {0: "off", 1: "on"}

ACTIVE_BALANCE_SWITCH: Final[dict[int, str]] = {0: "closed", 1: "open"}

COMMUNICATION_METHOD: Final[dict[int, str]] = {0: "RS485", 1: "CAN", 65535: "--"}

INVERTER_MANUFACTURER: Final[dict[int, str]] = {
    0: "NONE",
    1: "PYLON",
    2: "GROWAT",
    3: "SOFAR",
    4: "VOLTRONICPOWER",
    5: "GOODWE",
    6: "SRNE",
    7: "MUST",
    8: "VICTRONENERGY",
    9: "SMA",
    10: "DEYE",
    11: "AISWEI",
    12: "SACOLAR",
    13: "SOLARK",
    14: "XMT",
    15: "SOLIS",
    16: "LUXPOWERTEK",
    17: "STUDER",
    18: "SOROTEC",
    65535: "--",
}

INVERTER_MANUFACTURER_WRITE: Final[dict[int, str]] = {
    **{k: v for k, v in INVERTER_MANUFACTURER.items() if k != 65535},
    19: "Sunsynk",
    20: "MEGAREVO",
    21: "Techfine",
    22: "SCHNEIDER",
    23: "YWTNBQ",
}

INVERTER_SELF_IDENTIFY_RAW: Final[int] = 255
INVERTER_SELF_IDENTIFY_LABEL: Final[str] = "Self-Identification"

FACTORY_PARAMETER_PASSWORD: Final[str] = "123456"

# Registers excluded from MQTT/discovery (parameter password plaintext).
_PASSWORD_REGS: Final[frozenset[int]] = frozenset({0xC9, 0xCA, 0xCB})


@dataclass(frozen=True)
class DecodedDalySettings:
    values: Mapping[str, Any]
    desc: Mapping[str, dict]


def blocks_to_registers(blocks: tuple[tuple[int, bytes], ...]) -> dict[int, int]:
    regs: dict[int, int] = {}
    for base_addr, payload in blocks:
        for i in range(0, len(payload), 2):
            regs[base_addr + i // 2] = int.from_bytes(payload[i : i + 2], "big")
    return regs


def _reg(regs: dict[int, int], addr: int) -> int:
    return regs[addr]


def _scale_div10(raw: int) -> float:
    return raw / 10.0


def _scale_div1000(raw: int) -> float:
    return raw / 1000.0


def _scale_current_charge(raw: int) -> float:
    return abs((raw - 30000) / 10.0)


def _scale_current_discharge(raw: int) -> float:
    return (raw - 30000) / 10.0


def _scale_temp_bias(raw: int) -> float:
    return float(raw - 40)


def _scale_identity(raw: int) -> float:
    return float(raw)


def _enum_label(table: dict[int, str], raw: int) -> str:
    return table.get(raw, "invalid(%d)" % raw)


def _ascii_field(regs: dict[int, int], start: int, count: int, *, reverse: bool = False) -> str:
    raw = b"".join(_reg(regs, start + i).to_bytes(2, "big") for i in range(count))
    s = raw.replace(b"\x00", b"").decode("ascii", errors="ignore")
    return s[::-1] if reverse else s


def _sanitize_machine_code(regs: dict[int, int]) -> str:
    # Machine code spans 0xB9–0xCA in the app, but 0xC9–0xCB overlap the password.
    # Publish only the non-overlapping prefix (0xB9–0xC8).
    s = _ascii_field(regs, 0xB9, 0xC8 - 0xB9 + 1, reverse=False)
    s = re.sub(r"\x00", "", s)
    if "   " in s:
        s = s.split("   ")[0]
    s = s.strip()
    return s if s else "--"


def _parse_password_digits(regs: dict[int, int]) -> str:
    raw = _ascii_field(regs, 0xC9, 3, reverse=False)
    return "".join(ch for ch in raw if ch.isdigit())


def _password_flags(password: str) -> tuple[bool, bool]:
    configured = bool(password) and password != FACTORY_PARAMETER_PASSWORD
    factory_default = not password or password == FACTORY_PARAMETER_PASSWORD
    return configured, factory_default


def _production_date(regs: dict[int, int]) -> str:
    cc = _reg(regs, 0xCC)
    cd = _reg(regs, 0xCD)
    year = (cc >> 8) + 2000
    month = cc & 0xFF
    day = cd >> 8
    return "%04d-%02d-%02d" % (year, month, day)


def _rtc_datetime(regs: dict[int, int]) -> str:
    d4, d5, d6 = _reg(regs, 0xD4), _reg(regs, 0xD5), _reg(regs, 0xD6)
    year = (d4 >> 8) + 2000
    month = d4 & 0xFF
    day = d5 >> 8
    hour = d5 & 0xFF
    minute = d6 >> 8
    second = d6 & 0xFF
    return "%04d-%02d-%02d %02d:%02d:%02d" % (year, month, day, hour, minute, second)


def _extra_desc(
    key: str,
    *,
    name: str,
    unit: str | None = None,
    device_class: str | None = None,
    state_class: str | None = "measurement",
    precision: int | None = None,
    entity_category: str = "diagnostic",
    long_expiry: bool = True,
) -> dict:
    topic = "daly_config/%s" % key
    return {
        topic: {
            "field": key,
            "name": name,
            "device_class": device_class,
            "state_class": state_class,
            "unit_of_measurement": unit,
            "precision": precision,
            "entity_category": entity_category,
            "long_expiry": long_expiry,
        }
    }


def _add_numeric(
    values: dict[str, Any],
    desc: dict[str, dict],
    key: str,
    name: str,
    raw: int,
    scale: Callable[[int], float],
    *,
    unit: str | None,
    device_class: str | None = None,
    precision: int | None = None,
) -> None:
    val = scale(raw)
    if isinstance(val, float) and not math.isfinite(val):
        return
    values[key] = val
    desc.update(_extra_desc(key, name=name, unit=unit, device_class=device_class, precision=precision))


def _add_enum(
    values: dict[str, Any],
    desc: dict[str, dict],
    key: str,
    name: str,
    raw: int,
    table: dict[int, str],
) -> None:
    values[key] = _enum_label(table, raw)
    desc.update(
        _extra_desc(key, name=name, unit=None, device_class=None, state_class=None)
    )


def _add_string(values: dict[str, Any], desc: dict[str, dict], key: str, name: str, val: str) -> None:
    if not val:
        return
    values[key] = val
    desc.update(
        _extra_desc(key, name=name, unit=None, device_class=None, state_class=None)
    )


def _add_raw_register(values: dict[str, Any], desc: dict[str, dict], addr: int, raw: int, *, inferred: bool = False) -> None:
    key = "raw_0x%02X" % addr
    label = "Daly Raw Register 0x%02X" % addr
    if inferred and addr == 0xD3:
        label = "Daly Register 0xD3 (opaque, likely UART baud)"
    values[key] = raw
    desc.update(_extra_desc(key, name=label, unit=None, device_class=None, state_class="measurement"))


def decode_daly_settings_blocks(blocks: tuple[tuple[int, bytes], ...]) -> DecodedDalySettings:
    regs = blocks_to_registers(blocks)
    values: dict[str, Any] = {}
    desc: dict[str, dict] = {}

    # 0x80–0xA8 proven numerics
    numeric_map: tuple[tuple[int, str, str, Callable[[int], float], str | None, str | None, int | None], ...] = (
        (0x80, "rated_capacity_ah", "Rated Capacity", _scale_div10, "Ah", None, 1),
        (0x81, "cell_reference_voltage_v", "Cell Reference Voltage", _scale_div1000, "V", "voltage", 3),
        (0x82, "collection_board_count", "Collection Board Count", _scale_identity, None, None, 0),
        (0x83, "collection_board_1_cell_count", "Collection Board 1 Cell Count", _scale_identity, None, None, 0),
        (0x84, "collection_board_2_cell_count", "Collection Board 2 Cell Count", _scale_identity, None, None, 0),
        (0x85, "collection_board_3_cell_count", "Collection Board 3 Cell Count", _scale_identity, None, None, 0),
        (0x86, "collection_board_1_temp_sensor_count", "Collection Board 1 Temp Sensor Count", _scale_identity, None, None, 0),
        (0x87, "collection_board_2_temp_sensor_count", "Collection Board 2 Temp Sensor Count", _scale_identity, None, None, 0),
        (0x88, "collection_board_3_temp_sensor_count", "Collection Board 3 Temp Sensor Count", _scale_identity, None, None, 0),
        (0x8A, "hibernate_wait_time_s", "Hibernate Wait Time", _scale_identity, "s", "duration", 0),
        (0x8B, "cell_voltage_high_level_1_alarm_v", "Cell Voltage High Level 1 Alarm", _scale_div1000, "V", "voltage", 3),
        (0x8C, "cell_voltage_high_level_2_alarm_v", "Cell Voltage High Level 2 Alarm", _scale_div1000, "V", "voltage", 3),
        (0x8D, "cell_voltage_low_level_1_alarm_v", "Cell Voltage Low Level 1 Alarm", _scale_div1000, "V", "voltage", 3),
        (0x8E, "cell_voltage_low_level_2_alarm_v", "Cell Voltage Low Level 2 Alarm", _scale_div1000, "V", "voltage", 3),
        (0x8F, "total_voltage_high_level_1_alarm_v", "Total Voltage High Level 1 Alarm", _scale_div10, "V", "voltage", 1),
        (0x90, "total_voltage_high_level_2_alarm_v", "Total Voltage High Level 2 Alarm", _scale_div10, "V", "voltage", 1),
        (0x91, "total_voltage_low_level_1_alarm_v", "Total Voltage Low Level 1 Alarm", _scale_div10, "V", "voltage", 1),
        (0x92, "total_voltage_low_level_2_alarm_v", "Total Voltage Low Level 2 Alarm", _scale_div10, "V", "voltage", 1),
        (0x93, "charge_current_high_level_1_alarm_a", "Charge Current High Level 1 Alarm", _scale_current_charge, "A", "current", 1),
        (0x94, "charge_current_high_level_2_alarm_a", "Charge Current High Level 2 Alarm", _scale_current_charge, "A", "current", 1),
        (0x95, "discharge_current_high_level_1_alarm_a", "Discharge Current High Level 1 Alarm", _scale_current_discharge, "A", "current", 1),
        (0x96, "discharge_current_high_level_2_alarm_a", "Discharge Current High Level 2 Alarm", _scale_current_discharge, "A", "current", 1),
        (0x97, "charge_temperature_high_level_1_alarm_c", "Charge Temperature High Level 1 Alarm", _scale_temp_bias, "°C", "temperature", 1),
        (0x98, "charge_temperature_high_level_2_alarm_c", "Charge Temperature High Level 2 Alarm", _scale_temp_bias, "°C", "temperature", 1),
        (0x99, "charge_temperature_low_level_1_alarm_c", "Charge Temperature Low Level 1 Alarm", _scale_temp_bias, "°C", "temperature", 1),
        (0x9A, "charge_temperature_low_level_2_alarm_c", "Charge Temperature Low Level 2 Alarm", _scale_temp_bias, "°C", "temperature", 1),
        (0x9B, "discharge_temperature_high_level_1_alarm_c", "Discharge Temperature High Level 1 Alarm", _scale_temp_bias, "°C", "temperature", 1),
        (0x9C, "discharge_temperature_high_level_2_alarm_c", "Discharge Temperature High Level 2 Alarm", _scale_temp_bias, "°C", "temperature", 1),
        (0x9D, "discharge_temperature_low_level_1_alarm_c", "Discharge Temperature Low Level 1 Alarm", _scale_temp_bias, "°C", "temperature", 1),
        (0x9E, "discharge_temperature_low_level_2_alarm_c", "Discharge Temperature Low Level 2 Alarm", _scale_temp_bias, "°C", "temperature", 1),
        (0x9F, "cell_voltage_difference_level_1_alarm_v", "Cell Voltage Difference Level 1 Alarm", _scale_div1000, "V", "voltage", 3),
        (0xA0, "cell_voltage_difference_level_2_alarm_v", "Cell Voltage Difference Level 2 Alarm", _scale_div1000, "V", "voltage", 3),
        (0xA1, "temperature_difference_level_1_alarm_c", "Temperature Difference Level 1 Alarm", _scale_identity, "°C", "temperature", 1),
        (0xA2, "temperature_difference_level_2_alarm_c", "Temperature Difference Level 2 Alarm", _scale_identity, "°C", "temperature", 1),
        (0xA3, "balance_start_voltage_v", "Balance Start Voltage", _scale_div1000, "V", "voltage", 3),
        (0xA4, "balance_start_voltage_difference_v", "Balance Start Voltage Difference", _scale_div1000, "V", "voltage", 3),
        (0xA7, "soc_setting_percent", "SOC Setting", _scale_div10, "%", None, 1),
        (0xA8, "mos_temperature_protection_alarm_c", "MOS Temperature Protection Alarm", _scale_temp_bias, "°C", "temperature", 1),
        (0xCE, "battery_string_count", "Battery String Count", _scale_identity, None, None, 0),
        (0xD0, "active_balance_current_a", "Active Balance Current", _scale_div10, "A", "current", 1),
        (0xD7, "force_start_switch", "Force Start Switch", _scale_identity, None, None, 0),
        (0xD8, "heating_switch", "Heating Switch", _scale_identity, None, None, 0),
    )
    for addr, key, name, scale, unit, device_class, precision in numeric_map:
        if addr in regs:
            _add_numeric(values, desc, key, name, _reg(regs, addr), scale, unit=unit, device_class=device_class, precision=precision)

    if 0x89 in regs:
        _add_enum(values, desc, "battery_chemistry", "Battery Chemistry", _reg(regs, 0x89), BATTERY_CHEMISTRY)
    if 0xA5 in regs:
        _add_enum(values, desc, "charge_mos_switch_control", "Charge MOS", _reg(regs, 0xA5), MOS_SWITCH)
    if 0xA6 in regs:
        _add_enum(values, desc, "discharge_mos_switch_control", "Discharge MOS", _reg(regs, 0xA6), MOS_SWITCH)
    if 0xCF in regs:
        _add_enum(values, desc, "active_balance_switch", "Active Balance", _reg(regs, 0xCF), ACTIVE_BALANCE_SWITCH)
    if 0xD1 in regs:
        _add_enum(values, desc, "communication_method", "Communication Method", _reg(regs, 0xD1), COMMUNICATION_METHOD)
    if 0xD2 in regs:
        raw = _reg(regs, 0xD2)
        if raw == INVERTER_SELF_IDENTIFY_RAW:
            values["inverter_manufacturer"] = INVERTER_SELF_IDENTIFY_LABEL
            desc.update(_extra_desc("inverter_manufacturer", name="Inverter Manufacturer", unit=None, state_class=None))
        elif raw == 65535:
            _add_enum(values, desc, "inverter_manufacturer", "Inverter Manufacturer", raw, INVERTER_MANUFACTURER)
        else:
            _add_enum(values, desc, "inverter_manufacturer", "Inverter Manufacturer", raw, INVERTER_MANUFACTURER_WRITE)

    _add_string(values, desc, "software_version", "Software Version", _ascii_field(regs, 0xA9, 7, reverse=True))
    _add_string(values, desc, "hardware_version", "Hardware Version", _ascii_field(regs, 0xB1, 7, reverse=False))
    _add_string(values, desc, "machine_code", "Machine Code", _sanitize_machine_code(regs))

    if all(a in regs for a in (0xCC, 0xCD)):
        _add_string(values, desc, "production_date", "Production Date", _production_date(regs))
    if all(a in regs for a in (0xD4, 0xD5, 0xD6)):
        _add_string(values, desc, "rtc_datetime", "RTC Date/Time", _rtc_datetime(regs))

    password = _parse_password_digits(regs)
    configured, factory_default = _password_flags(password)
    values["parameter_password_configured"] = configured
    values["parameter_password_factory_default"] = factory_default
    desc.update(_extra_desc("parameter_password_configured", name="Parameter Password Configured", unit=None, state_class=None))
    desc.update(_extra_desc("parameter_password_factory_default", name="Parameter Password Factory Default", unit=None, state_class=None))

    for addr in (0xB0, 0xB8):
        if addr in regs:
            _add_raw_register(values, desc, addr, _reg(regs, addr))
    if 0xD3 in regs:
        _add_raw_register(values, desc, 0xD3, _reg(regs, 0xD3), inferred=True)
    for addr in range(0xD9, 0xEE):
        if addr in regs:
            _add_raw_register(values, desc, addr, _reg(regs, addr))

    return DecodedDalySettings(values=MappingProxyType(values), desc=MappingProxyType(desc))


def decoded_values_for_sample(decoded: Optional[DecodedDalySettings]) -> tuple[Optional[Mapping[str, Any]], Optional[Mapping[str, dict]]]:
    if decoded is None:
        return None, None
    return decoded.values, decoded.desc
