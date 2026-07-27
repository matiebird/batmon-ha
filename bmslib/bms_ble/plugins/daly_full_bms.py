"""BatMon Daly BMS extension (D2 Modbus over FFF0/FFF1/FFF2).

Supports hourly read-only extended register map and allowlisted D2 writes.
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
from bmslib.bms_ble.plugins.daly_full_protocol import (
    PROTOCOL_UNIT,
    READ_FUNCTION,
    WRITE_FUNCTION,
    WRITE_FRAME_LEN,
    build_d2_read_frame,
    build_field_write_frame,
    build_restart_write_frame,
    validate_d2_write_echo,
)
from bmslib.bms_ble.plugins.daly_full_write_registry import WRITE_FIELDS_BY_KEY, encode_field
from bmslib.util import get_logger

logger = get_logger()

READOUT_CACHE_SECONDS: Final[int] = 3600
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


def validate_d2_read_response(frame: bytes, *, addr: int, count: int) -> bytes:
    if len(frame) < 5 or frame[0] != PROTOCOL_UNIT:
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
    if func != READ_FUNCTION:
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


def _accept_d2_read_notification(data: bytes) -> bool:
    if len(data) < 5 or data[0] != PROTOCOL_UNIT or data[1] != READ_FUNCTION:
        return False
    expected_len = 3 + data[2] + 2
    return len(data) == expected_len and _check_crc(data)


class BMS(DalyBMS):
    """aiobmsble Daly BMS with optional extended register map and allowlisted writes."""

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
        self._wire_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._expected_write_echo: Optional[bytes] = None
        self._cached_blocks: Optional[tuple[tuple[int, bytes], ...]] = None

    @property
    def diagnostic_readout(self) -> Optional[Mapping[str, Any]]:
        return self._diagnostic_readout

    @property
    def decoded_settings(self) -> Optional[DecodedDalySettings]:
        return self._decoded_settings

    def _clear_readout_state(self) -> None:
        self._decoded_settings = None
        self._diagnostic_readout = None
        self._cached_blocks = None

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        raw = bytes(data)
        unit = raw[0] if len(raw) >= 1 else 0
        func = raw[1] if len(raw) >= 2 else 0
        self._log.debug(
            "RX BLE data: len=%d unit=0x%02X function=0x%02X",
            len(raw),
            unit,
            func,
        )

        if self._expected_write_echo is not None:
            try:
                validate_d2_write_echo(raw, expected_frame=self._expected_write_echo)
            except ValueError:
                self._log.debug("ignored non-matching write echo")
                return
            self._msg = raw
            self._msg_event.set()
            return

        if not _accept_d2_read_notification(raw):
            self._log.debug("response data is invalid")
            return

        self._msg = raw
        self._msg_event.set()

    async def _await_msg(self, data: bytes, char=None, wait_for_notify: bool = True, max_size: int = 0) -> None:
        async with self._wire_lock:
            await super()._await_msg(data, char, wait_for_notify, max_size)

    async def fetch_settings_blocks(self, *, force: bool = False) -> tuple[tuple[int, bytes], ...]:
        if not self._enable_daly_full_readout:
            raise RuntimeError("daly full readout not enabled")
        now = time.time()
        if not force and self._cached_blocks and now < self._readout_cache_until:
            return self._cached_blocks

        self._clear_readout_state()
        blocks: list[tuple[int, bytes]] = []
        for block in READOUT_BLOCKS:
            await self._await_msg(build_d2_read_frame(block.addr, block.count))
            payload = validate_d2_read_response(self._msg, addr=block.addr, count=block.count)
            blocks.append((block.addr, payload))
        immutable_blocks = tuple((addr, bytes(data)) for addr, data in blocks)
        self._decoded_settings = decode_daly_settings_blocks(immutable_blocks)
        self._cached_blocks = immutable_blocks
        register_count = sum(block.count for block in READOUT_BLOCKS)
        covered = ",".join(
            "0x%04X-0x%04X" % (block.addr, block.end_addr) for block in READOUT_BLOCKS
        )
        self._diagnostic_readout = MappingProxyType(
            {
                "protocol_unit": PROTOCOL_UNIT,
                "covered_ranges": covered,
                "register_count": register_count,
            }
        )
        self._readout_cache_until = now + READOUT_CACHE_SECONDS
        return immutable_blocks

    async def write_field(self, field_key: str, value: Any) -> None:
        field = WRITE_FIELDS_BY_KEY.get(field_key)
        if field is None:
            raise ValueError("unknown writable field %r" % field_key)
        raw = encode_field(field, value)
        frame = build_field_write_frame(field_key, raw)
        async with self._wire_lock:
            self._expected_write_echo = frame
            try:
                self._msg_event.clear()
                await self._client.write_gatt_char(self.uuid_tx(), frame, response=False)
                await asyncio.wait_for(self._msg_event.wait(), timeout=self.TIMEOUT)
                validate_d2_write_echo(self._msg, expected_frame=frame)
            finally:
                self._expected_write_echo = None

    async def restart_system(self) -> None:
        frame = build_restart_write_frame()
        async with self._wire_lock:
            self._expected_write_echo = frame
            try:
                self._msg_event.clear()
                await self._client.write_gatt_char(self.uuid_tx(), frame, response=False)
                await asyncio.wait_for(self._msg_event.wait(), timeout=self.TIMEOUT)
                validate_d2_write_echo(self._msg, expected_frame=frame)
            finally:
                self._expected_write_echo = None

    async def reconnect(self) -> None:
        async with self._wire_lock:
            await self.disconnect(reset=True)
            await self._connect()

    async def _maybe_full_readout(self) -> None:
        if not self._enable_daly_full_readout:
            return
        now = time.time()
        if now < self._readout_cache_until:
            return

        self._clear_readout_state()

        try:
            blocks = await self.fetch_settings_blocks(force=True)
            register_count = sum(block.count for block in READOUT_BLOCKS)
            covered = ",".join(
                "0x%04X-0x%04X" % (block.addr, block.end_addr) for block in READOUT_BLOCKS
            )
            logger.info(
                "Daly full readout unit=0x%02X ranges=%s registers=%d",
                PROTOCOL_UNIT,
                covered,
                register_count,
            )
        except asyncio.CancelledError:
            self._clear_readout_state()
            raise
        except Exception as exc:
            self._clear_readout_state()
            self._log.debug("Daly full readout failed (fail-soft): %s", exc)
            self._readout_cache_until = now + READOUT_CACHE_SECONDS

    async def _async_update(self):
        async with self._operation_lock:
            if self._enable_daly_full_readout:
                await self._maybe_full_readout()
            return await super()._async_update()
