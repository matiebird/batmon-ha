"""BatMon read-only extension of aiobmsble's Daly BMS (D2 Modbus over FFF0/FFF1/FFF2).

Stage: discovery-only. Issues official-app read-only D2 function-03 block reads
before normal telemetry when ``enable_daly_full_readout`` is true. No writes.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Mapping, Optional

from aiobmsble.basebms import crc_modbus
from aiobmsble.bms.daly_bms import BMS as DalyBMS
from bleak.backends.characteristic import BleakGATTCharacteristic

from bmslib.bms_ble.plugins.daly_full_decode import (
    DecodedDalySettings,
    decode_daly_settings_blocks,
)
from bmslib.util import get_logger

logger = get_logger()

READOUT_CACHE_SECONDS: Final[int] = 3600
_PROTOCOL_UNIT: Final[int] = 0xD2
_READ_FUNCTION: Final[int] = 0x03
_EXCEPTION_FUNCTION: Final[int] = 0x83


@dataclass(frozen=True)
class DalyReadoutBlock:
    addr: int
    count: int

    @property
    def end_addr(self) -> int:
        return self.addr + self.count - 1


READOUT_BLOCKS: Final[tuple[DalyReadoutBlock, ...]] = (
    DalyReadoutBlock(0x80, 0x50),
    DalyReadoutBlock(0xD0, 0x1E),
)


class DalyReadoutUnsupported(Exception):
    """BMS returned a Modbus exception for a readout block."""


def parse_enable_daly_full_readout(value: Any) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    raise ValueError("enable_daly_full_readout must be a boolean true or false")


def build_d2_read_frame(addr: int, count: int) -> bytes:
    return DalyBMS._cmd_modbus(dev_id=_PROTOCOL_UNIT, fct=_READ_FUNCTION, addr=addr, count=count)


def validate_d2_read_response(frame: bytes, *, addr: int, count: int) -> bytes:
    if len(frame) < 5 or frame[0] != _PROTOCOL_UNIT:
        raise ValueError("invalid D2 readout protocol unit")

    func = frame[1]
    if func == _EXCEPTION_FUNCTION:
        if len(frame) != 5:
            raise ValueError("invalid D2 readout frame length")
        if not _check_crc(frame):
            raise ValueError("invalid D2 readout CRC")
        raise DalyReadoutUnsupported(
            "D2 readout Modbus exception 0x%02x for 0x%04x count %d"
            % (frame[2], addr, count)
        )
    if func != _READ_FUNCTION:
        raise ValueError("invalid D2 readout function code")

    expected_data = count * 2
    expected_len = 3 + expected_data + 2
    if len(frame) != expected_len:
        raise ValueError("invalid D2 readout frame length")

    if frame[2] != expected_data:
        raise ValueError("invalid D2 readout byte count")

    if not _check_crc(frame):
        raise ValueError("invalid D2 readout CRC")

    return frame[3 : 3 + expected_data]


def _check_crc(frame: bytes) -> bool:
    calc = crc_modbus(frame[:-2])
    expected = int.from_bytes(frame[-2:], byteorder="little")
    return calc == expected


class BMS(DalyBMS):
    """aiobmsble Daly BMS with optional hourly read-only extended register map."""

    def __init__(
        self,
        ble_device,
        keep_alive: bool = True,
        secret: str = "",
        logger_name: str = "",
        enable_daly_full_readout: bool = False,
    ) -> None:
        super().__init__(ble_device, keep_alive, secret, logger_name)
        self._enable_daly_full_readout = parse_enable_daly_full_readout(enable_daly_full_readout)
        self._readout_cache_until: float = 0.0
        self._diagnostic_readout: Optional[Mapping[str, Any]] = None
        self._decoded_settings: Optional[DecodedDalySettings] = None

    @property
    def diagnostic_readout(self) -> Optional[Mapping[str, Any]]:
        return self._diagnostic_readout

    @property
    def decoded_settings(self) -> Optional[DecodedDalySettings]:
        return self._decoded_settings

    def _clear_readout_state(self) -> None:
        self._decoded_settings = None
        self._diagnostic_readout = None

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        unit = data[0] if len(data) >= 1 else 0
        func = data[1] if len(data) >= 2 else 0
        self._log.debug(
            "RX BLE data: len=%d unit=0x%02X function=0x%02X",
            len(data),
            unit,
            func,
        )

        if (
            len(data) < DalyBMS._HEAD_LEN
            or data[0:2] != DalyBMS._HEAD_READ
            or data[2] + 1 != len(data) - len(DalyBMS._HEAD_READ) - DalyBMS._CRC_LEN
        ):
            self._log.debug("response data is invalid")
            return

        if not self._check_integrity(
            data,
            crc_modbus,
            slice(None, -2),
            slice(-2, None),
            "little",
        ):
            return

        self._msg = bytes(data)
        self._msg_event.set()

    async def _maybe_full_readout(self) -> None:
        if not self._enable_daly_full_readout:
            return
        now = time.time()
        if now < self._readout_cache_until:
            return

        self._clear_readout_state()

        try:
            blocks: list[tuple[int, bytes]] = []
            for block in READOUT_BLOCKS:
                await self._await_msg(build_d2_read_frame(block.addr, block.count))
                payload = validate_d2_read_response(
                    self._msg, addr=block.addr, count=block.count
                )
                blocks.append((block.addr, payload))

            register_count = sum(block.count for block in READOUT_BLOCKS)
            immutable_blocks = tuple((addr, bytes(data)) for addr, data in blocks)
            covered = ",".join(
                "0x%04X-0x%04X" % (block.addr, block.end_addr) for block in READOUT_BLOCKS
            )
            self._decoded_settings = decode_daly_settings_blocks(immutable_blocks)
            self._diagnostic_readout = MappingProxyType(
                {
                    "protocol_unit": _PROTOCOL_UNIT,
                    "covered_ranges": covered,
                    "register_count": register_count,
                }
            )
            logger.info(
                "Daly full readout unit=0x%02X ranges=%s registers=%d",
                _PROTOCOL_UNIT,
                covered,
                register_count,
            )
            self._readout_cache_until = now + READOUT_CACHE_SECONDS
        except asyncio.CancelledError:
            self._clear_readout_state()
            raise
        except Exception as exc:
            self._clear_readout_state()
            self._log.debug("Daly full readout failed (fail-soft): %s", exc)
            self._readout_cache_until = now + READOUT_CACHE_SECONDS

    async def _async_update(self):
        if self._enable_daly_full_readout:
            await self._maybe_full_readout()
        return await super()._async_update()
