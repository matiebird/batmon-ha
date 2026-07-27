# Daly D2 Standard Writable Settings (Farm Path)

This document lists every **standard D2** writable setting implemented for `daly_full_ble` on the farm BMS path. Writes use allowlisted function `06` frames only; there is no caller-supplied raw address/API.

## Boundaries

| Scope | Included | Excluded |
|-------|----------|----------|
| Protocol | D2 Modbus `06` on FFF0/FFF1/FFF2 | Protocol `81`, YC product path |
| Charge MOS | Read-only telemetry (`0xA5`) | No D2 `0xA5` write (no app evidence) |
| Discharge MOS | D2 `0xA6` write (tier 3, arm required) | Protocol `81` `0x0122` |
| Password | Never backed up or published | `0xC9–0xCB` |
| Destructive | — | Factory reset, history clear, OTA |

## Writable table

| ID | HA entity | Address | Type | Range / options | Danger | Readback key | Evidence |
|----|-----------|---------|------|-----------------|--------|--------------|----------|
| D2-BAT-CAP | Number | 0x0080 | Ah ×10 | 0.1–6553.5 Ah | 1 | rated_capacity_ah | BatteryParametersSettingFragment.java:1684-1764 |
| D2-BAT-TYPE | Select | 0x0089 | enum | LiFePO4, Ternary, LTO, Sodium-ion | 2 | battery_chemistry | BAT:1271-1317 |
| D2-HIB-WAIT | Number | 0x008A | seconds | 30–65535 (65535 = firmware hibernate special) | 1 | hibernate_wait_time_s | BAT:1843-1885 |
| D2-SOC | Number | 0x00A7 | % ×10 | 0–100 | 2 | soc_setting_percent | BAT:1800-1840 |
| D2-COMM | Select | 0x00D1 | enum | RS485, CAN | 2 | communication_method | BAT:1324-1344 |
| D2-INVERTER | Select | 0x00D2 | enum | NONE…YWTNBQ (0–23), Self-Identification (wire 255) | 2 | inverter_manufacturer | BAT:1414-1443 |
| D2-BOARD-CELLS | Number | 0x0083 | integer | 1–32 cells | 2 | collection_board_1_cell_count | VOLT:1413-1440 |
| D2-BOARD-TEMPS | Number | 0x0086 | integer | 0–16 sensors | 2 | collection_board_1_temp_sensor_count | TEMP:967-993 |
| D2-CELL-HI | Number | 0x008C | V ×1000 | >0–5.0 V | 2 | cell_voltage_high_level_2_alarm_v | VOLT:891-907 |
| D2-CELL-LO | Number | 0x008E | V ×1000 | >0–5.0 V | 2 | cell_voltage_low_level_2_alarm_v | VOLT:910-989 |
| D2-TOTAL-HI | Number | 0x0090 | V ×10 | >0 V | 2 | total_voltage_high_level_2_alarm_v | VOLT:1053-1064 |
| D2-TOTAL-LO | Number | 0x0092 | V ×10 | >0 V | 2 | total_voltage_low_level_2_alarm_v | VOLT:1126-1137 |
| D2-CHG-I-L1 | Number | 0x0093 | 30000−10×A | ≥0 A | 2 | charge_current_high_level_1_alarm_a | VOLT:1224-1251 |
| D2-CHG-I-L2 | Number | 0x0094 | 30000−10×A | ≥0 A | 2 | charge_current_high_level_2_alarm_a | VOLT:1253-1316 |
| D2-DCHG-I-L2 | Number | 0x0096 | 30000+10×A | encoded U16 | 2 | discharge_current_high_level_2_alarm_a | VOLT:1386-1397 |
| D2-CHG-T-HI | Number | 0x0098 | °C+40 | encoded U16 | 2 | charge_temperature_high_level_2_alarm_c | TEMP:953-964 |
| D2-CHG-T-LO | Number | 0x009A | °C+40 | encoded U16 | 2 | charge_temperature_low_level_2_alarm_c | TEMP:826-885 |
| D2-DCHG-T-HI | Number | 0x009C | °C+40 | encoded U16 | 2 | discharge_temperature_high_level_2_alarm_c | TEMP:755-823 |
| D2-DCHG-T-LO | Number | 0x009E | °C+40 | encoded U16 | 2 | discharge_temperature_low_level_2_alarm_c | TEMP:684-752 |
| D2-V-DIFF | Number | 0x00A0 | V ×1000 | >0–5.0 V | 2 | cell_voltage_difference_level_2_alarm_v | VOLT:1140-1221 |
| D2-T-DIFF | Number | 0x00A2 | °C | encoded U16 | 2 | temperature_difference_level_2_alarm_c | TEMP:613-681 |
| D2-BAL-START | Number | 0x00A3 | V ×1000 | >0–5.0 V | 2 | balance_start_voltage_v | EQ:475-507 |
| D2-BAL-DIFF | Number | 0x00A4 | V ×1000 | >0–5.0 V | 2 | balance_start_voltage_difference_v | EQ:510-537 |
| D2-DCHG-MOS | Select | 0x00A6 | off/on | tier 3; arm required | 3 | discharge_mos_switch_control | CTL:1177-1268 |
| D2-RESTART | Button | 0x00F0 | const 0 | tier 3; arm required | 3 | — | CTL:1047-1076 |

## Home Assistant controls

- **Number/Select**: stage desired value on `set`; no hardware write until Apply.
- **Apply Pending DALY Settings**: runs read-before-write transaction.
- **Discard Pending**: clears staged values.
- **Restore Last DALY Settings**: restores last verified backup from `/data` (mode 0600, no password fields).
- **Arm Advanced DALY Operations**: 60-second window required for discharge MOS and restart.

## Cross-field validation (beyond app)

- Cell/total high alarms must exceed low alarms.
- Balance delta must be positive and less than start voltage.
- Topology: cells 1–32, temp sensors 0–16.
- SOC 0–100; capacity >0; chemistry/enum membership; hibernate ≥30 or special enum.

## Protocol notes

- CRC: Modbus CRC16, **low byte first** on the wire (matches live aiobmsble transport).
- Write echo: exact 8-byte `D2 06` frame mirrored; no trailing bytes.
- Registry is designed so protocol `81` entries can be added later without raw caller APIs.
