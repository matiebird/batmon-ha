# Daly Android-app Modbus capability probe (Stage 2)

## Goal

Detect whether a Daly BMS on the **native BLE** path (`type: daly`) responds to the
Modbus-style unit addresses used by the official DALY BMS Android application, without
changing device settings or opening a second BLE client.

## Source evidence

| Item | Value |
|------|-------|
| Application | DALY BMS `com.daly.bms.app` |
| Version | V3.3.0.3 |
| Decompile root | `/tmp/daly-bms-app/jadx-out` |
| BLE service | `FFF0` |
| Notify characteristic | `FFF1` |
| Write characteristic | `FFF2` |
| Primary Modbus unit | `0x81` |
| Fallback unit | `0xD2` (if `0x81` fails) |

### What the Android app actually does (V3.3.0.3)

Capability detection in the decompiled app does **not** use a function-03 read of
register `0x0038`. It performs a **mutating** RTC probe: Modbus writes to registers
`0x0123`–`0x0125` (real-time-clock fields) via unit `0x81`, with unit `0xD2` as
fallback when `0x81` does not answer.

BatMon **does not copy** that RTC write test. Stage 2 deliberately substitutes a
**non-mutating** function-03 read of register `0x0038` (count 1) on the same unit
addresses (`0x81`, then `0xD2` only after timeout or valid Modbus exception from
`0x81`). This is our safe read-only substitute, not observed app behavior for
capability detection.

**Not implemented in Stage 2:** RTC register writes (`0x0123`–`0x0125`), function
**0x06** or **0x10** writes, password register access, or persistent setting opcodes
(`0x10`–`0x21`).

## Opt-in configuration

Per device in the add-on configuration:

```yaml
enable_daly_android_protocol_probe: true
```

- Strict `bool` — string values such as `"false"` are rejected.
- Default `false` — the legacy A5 command sequence (`0x90` / `0x93` / `0x94` …) is
  unchanged when disabled.
- **BLE `daly` only.** `daly_uart` rejects `true`; the option is not threaded for UART
  devices.

## Probe sequence (when enabled)

Before final normal A5 telemetry (`0x93` status, `0x90` SOC), inside the existing
`DalyBt` session and under the same wire lock:

1. Send exactly one read to unit `0x81`:

   `81 03 00 38 00 01 1A 07`

   Expect success frame: `81 03 02 VV VV CRC_LO CRC_HI` (7 bytes, Modbus CRC16).

2. If `0x81` times out **with no response bytes** or returns a valid Modbus exception
   (`function 0x83`), send exactly one read to unit `0xD2`:

   `D2 03 00 38 00 01 16 64`

   Expect: `D2 03 02 VV VV CRC_LO CRC_HI`.

3. Do **not** probe `0xD2` when `0x81` succeeds.

4. **Fail closed** on `0x81` without probing `0xD2` when the response is malformed
   (CRC failure, wrong unit/function/count, trailing bytes, voltage out of range after
   frame validation, or incomplete data in the receive buffer at timeout).

Modbus frames are **not** routed through the 13-byte A5 parser. A narrow pending-read
demultiplexer collects probe responses from the same notify callback, including
fragmented or coalesced BLE notifications and `bytearray` payloads. While a Modbus
probe is pending, partial Modbus bytes are not fed to the A5 parser; after a complete
Modbus frame is consumed, any coalesced remainder is passed to A5 handling.

## Caching

Successful probe results and all failures (timeout pair, exception pair, parse
rejection, malformed response) are cached for **one hour** per device. An unsupported
device therefore incurs at most two read attempts (`0x81` + `0xD2`) per hour when both
units are silent. Probes run before telemetry so sample freshness matches the normal
polling path.

## MQTT / Home Assistant (read-only)

When a probe succeeds, diagnostic entities are published (no `command_topic`):

| Field | Entity | Notes |
|-------|--------|-------|
| `android_protocol_unit` | string sensor | `"81"` or `"D2"` |
| `android_protocol_voltage` | voltage sensor | decoded register raw ÷ 10 (volts) |

Entity category: `diagnostic`.

## Safety boundaries (Stage 2)

- No generic raw-command API.
- No Modbus function 0x06 / 0x10 builders or register writes.
- No password or settings register reads beyond the single `0x0038` capability probe.
- BatMon remains the sole BLE owner; no second GATT client.
- MOS controls (`0xD9` / `0xDA`) use the same wire lock as A5 and Modbus traffic.

Stage 1 read-only A5 diagnostics (`enable_daly_diagnostics`) remain separate and may be
enabled independently.
