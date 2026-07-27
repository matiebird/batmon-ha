# Daly official-app D2 settings decode (`daly_full_ble`)

Read-only interoperability notes for the two Modbus-like D2 read windows used by the
official Daly app over BLE GATT `FFF0/FFF1/FFF2`:

| Read command | Address range | Register count |
|---|---|---|
| `D2 03 00 80 00 50` | `0x0080`–`0x00CF` | 80 |
| `D2 03 00 D0 00 1E` | `0x00D0`–`0x00ED` | 30 |

All multi-byte numeric fields are **big-endian unsigned 16-bit registers**. Negative
currents and temperatures use explicit biases (not two's-complement). Facts below are
**proven** from clean-room APK analysis unless marked **inferred**.

`R` / `W` indicate official-app read/write usage, not a claim about firmware write
permissions. BatMon exposes these fields read-only via MQTT/HA diagnostic sensors.

## Block `0x0080`–`0x00A8` (proven numerics)

| Addr | Semantic | Scale / bias | Unit | Enum / notes | App R/W | Status |
|---|---|---|---|---|---|---|
| 0x80 | Rated capacity | ÷10 | Ah | | R/W | proven |
| 0x81 | Cell reference voltage | ÷1000 | V | | R | proven |
| 0x82 | Collection-board count | integer | boards | | R | proven |
| 0x83 | Collection board 1 cell count | integer | cells | | R/W | proven |
| 0x84 | Collection board 2 cell count | integer | cells | | R | proven |
| 0x85 | Collection board 3 cell count | integer | cells | | R | proven |
| 0x86 | Collection board 1 temp-sensor count | integer | sensors | | R/W | proven |
| 0x87 | Collection board 2 temp-sensor count | integer | sensors | | R | proven |
| 0x88 | Collection board 3 temp-sensor count | integer | sensors | | R | proven |
| 0x89 | Battery chemistry | enum | | 0 LiFePO₄, 1 ternary, 2 LTO, 3 sodium (writer) | R/W | proven |
| 0x8A | Hibernate wait time | integer | s | UI rejects &lt;30; 65535 special | R/W | proven |
| 0x8B | Cell voltage high level-1 alarm | ÷1000 | V | | R | proven |
| 0x8C | Cell voltage high level-2 alarm | ÷1000 | V | UI rejects &gt;5.0 V | R/W | proven |
| 0x8D | Cell voltage low level-1 alarm | ÷1000 | V | | R | proven |
| 0x8E | Cell voltage low level-2 alarm | ÷1000 | V | UI rejects &gt;5.0 V | R/W | proven |
| 0x8F | Total voltage high level-1 alarm | ÷10 | V | | R | proven |
| 0x90 | Total voltage high level-2 alarm | ÷10 | V | | R/W | proven |
| 0x91 | Total voltage low level-1 alarm | ÷10 | V | | R | proven |
| 0x92 | Total voltage low level-2 alarm | ÷10 | V | | R/W | proven |
| 0x93 | Charge current high level-1 alarm | abs((raw−30000)÷10) | A | biased unsigned | R/W | proven |
| 0x94 | Charge current high level-2 alarm | abs((raw−30000)÷10) | A | biased unsigned | R/W | proven |
| 0x95 | Discharge current high level-1 alarm | (raw−30000)÷10 | A | biased unsigned | R | proven |
| 0x96 | Discharge current high level-2 alarm | (raw−30000)÷10 | A | biased unsigned | R/W | proven |
| 0x97 | Charge temp high level-1 alarm | raw−40 | °C | | R | proven |
| 0x98 | Charge temp high level-2 alarm | raw−40 | °C | | R/W | proven |
| 0x99 | Charge temp low level-1 alarm | raw−40 | °C | | R | proven |
| 0x9A | Charge temp low level-2 alarm | raw−40 | °C | | R/W | proven |
| 0x9B | Discharge temp high level-1 alarm | raw−40 | °C | | R | proven |
| 0x9C | Discharge temp high level-2 alarm | raw−40 | °C | | R/W | proven |
| 0x9D | Discharge temp low level-1 alarm | raw−40 | °C | | R | proven |
| 0x9E | Discharge temp low level-2 alarm | raw−40 | °C | | R/W | proven |
| 0x9F | Cell-voltage difference level-1 alarm | ÷1000 | V | | R | proven |
| 0xA0 | Cell-voltage difference level-2 alarm | ÷1000 | V | UI rejects &gt;5.0 V | R/W | proven |
| 0xA1 | Temperature difference level-1 alarm | integer | °C | no −40 bias | R | proven |
| 0xA2 | Temperature difference level-2 alarm | integer | °C | no −40 bias | R/W | proven |
| 0xA3 | Balance start voltage | ÷1000 | V | | R/W | proven |
| 0xA4 | Balance start voltage difference | ÷1000 | V | | R/W | proven |
| 0xA5 | Charge MOS switch control | enum | | 0 off, 1 on | R | proven |
| 0xA6 | Discharge MOS switch control | enum | | 0 off, 1 on | R/W | proven |
| 0xA7 | SOC setting | ÷10 | % | UI rejects &gt;100% | R/W | proven |
| 0xA8 | MOS temperature protection alarm | raw−40 | °C | | R | proven |

## Identity / manufacturing `0x00A9`–`0x00CF`

| Addr span | Semantic | Encoding | App R/W | Status |
|---|---|---|---|---|
| 0xA9–0xAF | Software version | 14-byte ASCII, NUL stripped, **string reversed** | R | proven |
| 0xB0 | Opaque | 1×U16 | included in read, unused by app | proven opaque |
| 0xB1–0xB7 | Hardware version | 14-byte ASCII forward, NUL stripped | R | proven |
| 0xB8 | Opaque | 1×U16 | included in read, unused by app | proven opaque |
| 0xB9–0xC8 | Machine / device code (sanitized) | ASCII forward; BatMon publishes **0xB9–0xC8 only** to avoid password overlap | R | proven (overlap noted) |
| 0xC9–0xCB | Parameter-setting password | 6 decimal ASCII digits | R | proven secret — **never published** |
| 0xCC–0xCD | Production date | year−2000 / month / day bytes | R | proven |
| 0xCE | Battery string count | integer | R | proven |
| 0xCF | Active balance switch | 0 closed, 1 open | R | proven |

**Proven overlap:** official app machine-code slice `0xB9–0xCA` overlaps password
`0xC9–0xCB`. BatMon removes overlapping password bytes from published machine code and
never logs or MQTT-publishes password plaintext. Only booleans
`parameter_password_configured` and `parameter_password_factory_default` are exposed.

## Advanced block `0x00D0`–`0x00ED`

| Addr span | Semantic | Scale / encoding | Enum / notes | App R/W | Status |
|---|---|---|---|---|---|
| 0xD0 | Active balance current | ÷10 | A | R | proven |
| 0xD1 | Communication method | enum | 0 RS485, 1 CAN, 65535 `--` | R/W | proven |
| 0xD2 | Inverter manufacturer | enum | reader 0–18 + 65535 `--`; writer adds 19–23, 255 | R/W | proven |
| 0xD3 | Opaque (likely UART baud) | raw U16 | **inferred** from parameter-order hole only | not parsed by app D2 path | inferred |
| 0xD4 | RTC year/month | high=year−2000, low=month | | R | proven |
| 0xD5 | RTC day/hour | high=day, low=hour | | R | proven |
| 0xD6 | RTC minute/second | high=minute, low=second | | R | proven |
| 0xD7 | Force-start switch | integer | 65535 unavailable elsewhere | R | proven |
| 0xD8 | Heating switch | integer | raw scalar | R | proven |
| 0xD9–0xED | Opaque registers | raw U16 each | app reads but does not decode | R | proven opaque |

## Home Assistant exposure

When `type: daly_full_ble` and `enable_daly_full_readout: true`, decoded fields attach to
each telemetry sample as `extra_values` / `extra_desc` and are published as read-only MQTT
discovery sensors under `daly_config/…` with `expire_after` ≥ 7200 s. No `command_topic`
is ever emitted for these entities.
