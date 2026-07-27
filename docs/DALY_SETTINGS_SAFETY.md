# Daly settings safety

## Goal

Support **guarded, official-program-like Daly BMS settings** in batmon-ha: persistent
configuration changes only when protocol behavior is proven on the live device,
with backup, read-before-write, readback verification, and explicit user opt-in.

## Stage 1 (current): read-only identity and capability discovery

Stage 1 adds **read-only** access to a subset of the legacy A5 13-byte protocol
over native `type: daly` (BLE). **All extended diagnostics (opcodes `0x50`–`0x63`)
are opt-in** per device via `enable_daly_diagnostics: true` in the add-on
configuration. Default is `false` so normal telemetry is unchanged on firmware
that rejects undocumented extended reads.

| Opcode | Data | Purpose |
|--------|------|---------|
| `0x50` | BE u32 rated capacity (mAh), 2 reserved bytes, BE u16 nominal cell voltage (mV) | Pack rated parameters |
| `0x53` | Production date (bytes 2–4: year since 2000, month, day) | Manufacturing identity |
| `0x62` | Two frames: seq 1+2, 7 printable ASCII bytes each (14 chars total) | Software version — **compatibility gate** |
| `0x63` | Same framing as `0x62` | Hardware version — **compatibility gate** |

Captured examples: software `20210222-1.01T`, hardware `DL-BMS-R32-01E`.

When enabled, values are exposed as diagnostic MQTT/HA sensors and polled at most
once per hour per diagnostic key (success and failure outcomes are both cached).

**Compatibility gate for future writes:** any Stage 2+ settings write must read
and validate `0x62`/`0x63` identity on the live device before accepting write
opcodes. Writes must not proceed when version discovery fails or firmware is
unknown.

**Not in Stage 1:**

- Opcode `0x57` (multi-frame battery code) — frame encoding not sufficiently
  proven for the live BLE path.
- Opcodes `0x10`–`0x21` and any other **persistent write** — not in the official
  V1.0 read protocol artifact; treated as unsafe without live capture proof.
- Generic command APIs, MQTT command topics, number/button/switch/select controls
  for settings, or changes to existing `0xD9`/`0xDA` MOS controls.

## Requirements before any settings write stage

1. **Explicit opt-in** — `enable_daly_diagnostics: true` for reads; separate
   write opt-in for any persistent change (not implemented in Stage 1).
2. **Live artifact / capture proof** on the exact firmware and transport (BLE vs
   UART) in use.
3. **Firmware compatibility matrix** — `0x62`/`0x63` identity matched against
   proven firmware; reject unknown combinations.
4. **Read-before-write and readback** — never blind writes; verify post-write
   reads match intent.
5. **Backup** — export or snapshot current settings before modification.

## Residual limits (Stage 1)

- `0x53` payload bytes outside the production date triplet are ignored.
- Rated capacity maximum is capped at 2000 Ah for validation; larger packs need
  a deliberate bound review.
- Nominal cell voltage is validated to 2.5–4.5 V per cell; outliers are rejected.
- `0x50` reserved bytes are not validated (nonzero reserved bytes are accepted).
- When diagnostics are disabled or unsupported, extended opcodes are not issued and
  normal `0x90`–`0x98` telemetry is unaffected.
