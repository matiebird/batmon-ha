# Daly D2 Standard Writable Settings (Farm Path)

This document lists every **standard D2** writable setting and the **limited protocol-81** advanced controls implemented for `daly_full_ble` on the farm BMS path. Writes use allowlisted function `06` frames only; there is no caller-supplied raw address/API.

## Boundaries

| Scope | Included | Excluded |
|-------|----------|----------|
| Protocol | D2 Modbus `06` on FFF0/FFF1/FFF2; protocol `81` `06` for three advanced controls below | Other protocol `81` addresses, YC product path |
| Charge MOS | Protocol `81` `0x0121` write (tier 3, arm required); D2 `0xA5` readback | No D2 `0xA5` write |
| Discharge MOS | D2 `0xA6` write (tier 3, arm required) | Protocol `81` `0x0122` |
| Active balance | Protocol `81` `0x0119` write (tier 3, arm required); D2 `0xCF` readback | Other balance/RTC/OTA `81` settings |
| Force Start | Protocol `81` `0x012E` one-shot (tier 3, arm required); D2 `0xD7` precondition read | Unsupported when `0xD7` raw `65535` |
| Password | Never backed up or published | `0xC9–0xCB` |
| Destructive | — | Factory reset, history clear, OTA, full protocol-81 map |

## Writable table (D2)

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
| D2-CHG-I-L1 | Number | 0x0093 | 30000−10×A | 0–3000 A | 2 | charge_current_high_level_1_alarm_a | VOLT:1224-1251 |
| D2-CHG-I-L2 | Number | 0x0094 | 30000−10×A | 0–3000 A | 2 | charge_current_high_level_2_alarm_a | VOLT:1253-1316 |
| D2-DCHG-I-L2 | Number | 0x0096 | 30000+10×A | 0–3553.5 A | 2 | discharge_current_high_level_2_alarm_a | VOLT:1386-1397 |
| D2-CHG-T-HI | Number | 0x0098 | °C+40 | −40..125 °C | 2 | charge_temperature_high_level_2_alarm_c | TEMP:953-964 |
| D2-CHG-T-LO | Number | 0x009A | °C+40 | −40..125 °C | 2 | charge_temperature_low_level_2_alarm_c | TEMP:826-885 |
| D2-DCHG-T-HI | Number | 0x009C | °C+40 | −40..125 °C | 2 | discharge_temperature_high_level_2_alarm_c | TEMP:755-823 |
| D2-DCHG-T-LO | Number | 0x009E | °C+40 | −40..125 °C | 2 | discharge_temperature_low_level_2_alarm_c | TEMP:684-752 |
| D2-V-DIFF | Number | 0x00A0 | V ×1000 | >0–5.0 V | 2 | cell_voltage_difference_level_2_alarm_v | VOLT:1140-1221 |
| D2-T-DIFF | Number | 0x00A2 | °C | encoded U16 | 2 | temperature_difference_level_2_alarm_c | TEMP:613-681 |
| D2-BAL-START | Number | 0x00A3 | V ×1000 | >0–5.0 V | 2 | balance_start_voltage_v | EQ:475-507 |
| D2-BAL-DIFF | Number | 0x00A4 | V ×1000 | >0–5.0 V | 2 | balance_start_voltage_difference_v | EQ:510-537 |
| D2-DCHG-MOS | Select | 0x00A6 | off/on | tier 3; arm required | 3 | discharge_mos_switch_control | CTL:1177-1268 |
| D2-RESTART | Button | 0x00F0 | const 0 | tier 3; arm required | 3 | — | CTL:1047-1076 |

## Protocol-81 advanced controls (implemented)

| ID | HA entity | Unit | Address | Raw | Readback (D2) | Danger | Notes |
|----|-----------|------|---------|-----|---------------|--------|-------|
| 81-CHG-MOS | Select “Charge MOS” | 81 | 0x0121 | 0=off, 1=on | `charge_mos_switch_control` (0xA5) | 3 | Staged; Apply transaction |
| 81-ACT-BAL | Select “Active Balance” | 81 | 0x0119 | 0=closed, 1=open | `active_balance_switch` (0xCF) | 3 | Staged; Apply transaction |
| 81-FORCE-START | Button “Force Start” | 81 | 0x012E | 1 | `force_start_switch` (0xD7) | 3 | Separate action; rejects raw 65535 |

**Future scope:** No other protocol-81 settings (RTC, OTA, discharge MOS `0x0122`, YC, full `81` map) are implemented. Only the three rows above use unit `81`.

## Home Assistant controls

- **Number/Select**: stage desired value on `set`; no hardware write until Apply.
- **Apply Pending DALY Settings**: runs read-before-write transaction.
- **Discard Pending**: clears staged values.
- **Restore Last DALY Settings**: restores last verified backup from `/data` (mode 0600, no password fields).
- **Arm Advanced DALY Operations**: 60-second window required for tier-3 fields (charge MOS, discharge MOS, active balance, restart, force start). One arm authorizes a multi-field Apply; arm is rechecked before each tier-3 write and consumed when the transaction ends.
- **Force Start**: separate button; never applies other pending fields.

## Cross-field validation (beyond app)

- Cell/total high alarms must exceed low alarms.
- Balance delta must be positive and less than start voltage.
- Topology: cells 1–32, temp sensors 0–16.
- SOC 0–100; capacity >0; chemistry/enum membership; hibernate ≥30 or special enum.

## Protocol notes

- CRC: Modbus CRC16, **low byte first** on the wire (matches live aiobmsble transport).
- Write echo: exact 8-byte `D2 06` or `81 06` frame mirrored; no trailing bytes.
- Reads remain D2 function `03` only.
