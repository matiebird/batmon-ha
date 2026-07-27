"""Apply staged Daly D2 settings with read-before-write, echo, and verify."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol

from bmslib.bms_ble.plugins.daly_full_backup import load_backup, save_backup
from bmslib.bms_ble.plugins.daly_full_decode import DecodedDalySettings, decode_daly_settings_blocks
from bmslib.bms_ble.plugins.daly_full_staging import DalyStagingState
from bmslib.bms_ble.plugins.daly_full_write_registry import (
    DalyWriteField,
    FORCE_START_FIELD_KEY,
    FORCE_START_UNSUPPORTED_RAW,
    RESTART_FIELD_KEY,
    WRITE_FIELDS_BY_KEY,
    build_write_plan,
    encode_field,
    validate_prospective_configuration,
)


class DalyWire(Protocol):
    async def fetch_settings_blocks(self, *, force: bool = False) -> tuple[tuple[int, bytes], ...]: ...
    async def write_field(self, field_key: str, value: Any) -> None: ...
    async def restart_system(self) -> None: ...
    async def reconnect(self) -> None: ...


@dataclass(frozen=True)
class ApplyResult:
    ok: bool
    status: str
    failed_field: Optional[str] = None
    written_fields: tuple[str, ...] = ()
    skipped_fields: tuple[str, ...] = ()


def values_from_decoded(decoded: DecodedDalySettings) -> dict[str, Any]:
    return {k: decoded.values[k] for k in WRITE_FIELDS_BY_KEY if k in decoded.values}


def full_context_from_decoded(decoded: DecodedDalySettings) -> dict[str, Any]:
    return dict(decoded.values)


def _readback_matches(field: DalyWriteField, expected_raw: int, decoded_val: Any) -> bool:
    try:
        return encode_field(field, decoded_val) == expected_raw
    except Exception:
        return False


async def apply_staged_settings(
    wire: DalyWire,
    staging: DalyStagingState,
    *,
    device_id: str,
    data_dir=None,
    now: Optional[float] = None,
    preserve_backup: bool = False,
) -> ApplyResult:
    t = now if now is not None else time.time()
    armed = staging.is_armed(t)
    try:
        blocks = await wire.fetch_settings_blocks(force=True)
    except asyncio.CancelledError:
        staging.record_apply_result("apply_cancelled", when=t)
        raise
    except Exception:
        staging.record_apply_result("read_failed", when=t)
        return ApplyResult(False, "read_failed")

    decoded = decode_daly_settings_blocks(blocks)
    full_current = full_context_from_decoded(decoded)
    current = values_from_decoded(decoded)
    staging.update_current(current)

    try:
        plan = build_write_plan(full_current, staging.staged, armed=armed)
    except ValueError as exc:
        staging.record_apply_result("validation_failed", field=str(exc)[:64], when=t)
        return ApplyResult(False, "validation_failed")

    if not plan:
        staging.record_apply_result("no_changes", when=t)
        staging.discard()
        return ApplyResult(True, "no_changes", skipped_fields=tuple(staging.pending_keys()))

    planned_keys = tuple(field_def.key for field_def, _, _ in plan)
    pre_write_values = {k: current[k] for k in planned_keys if k in current}

    if not preserve_backup:
        try:
            save_backup(
                device_id,
                pre_write_values,
                changed_fields=planned_keys,
                timestamp=t,
                data_dir=data_dir,
            )
        except Exception:
            staging.record_apply_result("backup_failed", when=t)
            return ApplyResult(False, "backup_failed")

    written: list[str] = []
    tier3_transaction = any(field_def.requires_arm for field_def, _, _ in plan)
    try:
        for field_def, raw, staged_val in plan:
            if field_def.requires_arm and not staging.is_armed(time.time()):
                staging.record_apply_result("advanced_arm_expired", field=field_def.key, when=time.time())
                return ApplyResult(
                    False,
                    "advanced_arm_expired",
                    failed_field=field_def.key,
                    written_fields=tuple(written),
                )
            try:
                await wire.write_field(field_def.key, staged_val)
            except asyncio.CancelledError:
                staging.record_apply_result("apply_cancelled", when=t)
                raise
            except Exception:
                staging.record_apply_result("write_failed", field=field_def.key, when=t)
                return ApplyResult(False, "write_failed", failed_field=field_def.key, written_fields=tuple(written))

            try:
                blocks_after = await wire.fetch_settings_blocks(force=True)
                post = values_from_decoded(decode_daly_settings_blocks(blocks_after))
            except asyncio.CancelledError:
                staging.record_apply_result("apply_cancelled", when=t)
                raise
            except Exception:
                staging.record_apply_result("readback_failed", field=field_def.key, when=t)
                return ApplyResult(False, "readback_failed", failed_field=field_def.key, written_fields=tuple(written))

            rb_val = post.get(field_def.readback_key)
            if not _readback_matches(field_def, raw, rb_val):
                staging.record_apply_result("readback_mismatch", field=field_def.key, when=t)
                return ApplyResult(False, "readback_mismatch", failed_field=field_def.key, written_fields=tuple(written))

            written.append(field_def.key)
            current[field_def.key] = rb_val

        try:
            await wire.reconnect()
            blocks_final = await wire.fetch_settings_blocks(force=True)
            final = values_from_decoded(decode_daly_settings_blocks(blocks_final))
        except asyncio.CancelledError:
            staging.record_apply_result("apply_cancelled", when=t)
            raise
        except Exception:
            staging.record_apply_result("verify_failed", when=t)
            return ApplyResult(False, "verify_failed", written_fields=tuple(written))

        for key in written:
            fld = WRITE_FIELDS_BY_KEY[key]
            expected_raw = encode_field(fld, current.get(key))
            if not _readback_matches(fld, expected_raw, final.get(key)):
                staging.record_apply_result("persist_failed", field=key, when=t)
                return ApplyResult(False, "persist_failed", failed_field=key, written_fields=tuple(written))
    except asyncio.CancelledError:
        staging.record_apply_result("apply_cancelled", when=t)
        raise
    finally:
        if tier3_transaction:
            staging.disarm_advanced()

    staging.update_current(final)
    staging.discard()
    staging.record_apply_result("success", when=t)
    return ApplyResult(True, "success", written_fields=tuple(written))


async def restart_daly_system(
    wire: DalyWire,
    staging: DalyStagingState,
    *,
    now: Optional[float] = None,
) -> ApplyResult:
    t = now if now is not None else time.time()
    if not staging.is_armed(time.time()):
        staging.record_apply_result("restart_unarmed", when=t)
        return ApplyResult(False, "restart_unarmed")

    try:
        await wire.restart_system()
    except asyncio.CancelledError:
        staging.record_apply_result("restart_cancelled", when=t)
        raise
    except Exception:
        staging.record_apply_result("restart_failed", when=t)
        return ApplyResult(False, "restart_failed", failed_field=RESTART_FIELD_KEY)
    finally:
        staging.disarm_advanced()

    try:
        await wire.reconnect()
        await wire.fetch_settings_blocks(force=True)
    except asyncio.CancelledError:
        staging.record_apply_result("restart_cancelled", when=t)
        raise
    except Exception:
        staging.record_apply_result("restart_reconnect_failed", when=t)
        return ApplyResult(False, "restart_reconnect_failed", failed_field=RESTART_FIELD_KEY)

    staging.record_apply_result("restart_ok", when=t)
    return ApplyResult(True, "restart_ok", written_fields=(RESTART_FIELD_KEY,))


async def trigger_force_start(
    wire: DalyWire,
    staging: DalyStagingState,
    *,
    now: Optional[float] = None,
) -> ApplyResult:
    t = now if now is not None else time.time()
    if not staging.is_armed(time.time()):
        staging.record_apply_result("force_start_unarmed", when=t)
        return ApplyResult(False, "force_start_unarmed", failed_field=FORCE_START_FIELD_KEY)

    try:
        blocks = await wire.fetch_settings_blocks(force=True)
    except asyncio.CancelledError:
        staging.record_apply_result("force_start_cancelled", when=t)
        raise
    except Exception:
        staging.record_apply_result("force_start_read_failed", when=t)
        return ApplyResult(False, "force_start_read_failed", failed_field=FORCE_START_FIELD_KEY)

    decoded = decode_daly_settings_blocks(blocks)
    current_state = decoded.values.get("force_start_switch")
    if current_state == FORCE_START_UNSUPPORTED_RAW:
        staging.record_apply_result("force_start_unsupported", when=t)
        return ApplyResult(False, "force_start_unsupported", failed_field=FORCE_START_FIELD_KEY)

    if current_state not in (0, 0.0):
        staging.record_apply_result("force_start_noop", when=t)
        return ApplyResult(True, "force_start_noop", written_fields=())

    try:
        await wire.write_field(FORCE_START_FIELD_KEY, 1)
    except asyncio.CancelledError:
        staging.record_apply_result("force_start_cancelled", when=t)
        raise
    except Exception:
        staging.record_apply_result("force_start_failed", when=t)
        return ApplyResult(False, "force_start_failed", failed_field=FORCE_START_FIELD_KEY)
    finally:
        staging.disarm_advanced()

    try:
        await wire.reconnect()
        await wire.fetch_settings_blocks(force=True)
    except asyncio.CancelledError:
        staging.record_apply_result("force_start_cancelled", when=t)
        raise
    except Exception:
        staging.record_apply_result("force_start_reconnect_failed", when=t)
        return ApplyResult(False, "force_start_reconnect_failed", failed_field=FORCE_START_FIELD_KEY)

    staging.record_apply_result("force_start_ok", when=t)
    return ApplyResult(True, "force_start_ok", written_fields=(FORCE_START_FIELD_KEY,))


async def restore_last_settings(
    wire: DalyWire,
    staging: DalyStagingState,
    *,
    device_id: str,
    data_dir=None,
    now: Optional[float] = None,
) -> ApplyResult:
    t = now if now is not None else time.time()
    backup = load_backup(device_id, data_dir)
    if backup is None:
        staging.record_apply_result("no_backup", when=t)
        return ApplyResult(False, "no_backup")

    backup_values = backup.get("values", {})
    changed = tuple(backup.get("changed_fields") or ())
    if not changed:
        staging.record_apply_result("no_backup", when=t)
        return ApplyResult(False, "no_backup")

    try:
        blocks = await wire.fetch_settings_blocks(force=True)
    except asyncio.CancelledError:
        staging.record_apply_result("restore_cancelled", when=t)
        raise
    except Exception:
        staging.record_apply_result("read_failed", when=t)
        return ApplyResult(False, "read_failed")

    decoded = decode_daly_settings_blocks(blocks)
    current = values_from_decoded(decoded)
    full_current = full_context_from_decoded(decoded)
    restore_stage = {
        k: backup_values[k]
        for k in changed
        if k in backup_values and backup_values.get(k) != current.get(k)
    }
    if not restore_stage:
        staging.record_apply_result("restore_ok", when=t)
        return ApplyResult(True, "restore_ok")

    try:
        validate_prospective_configuration(full_current, restore_stage)
    except ValueError:
        staging.record_apply_result("restore_validation_failed", when=t)
        return ApplyResult(False, "restore_validation_failed")

    prev_staged = dict(staging.staged)
    staging.staged = restore_stage
    result: Optional[ApplyResult] = None
    try:
        result = await apply_staged_settings(
            wire,
            staging,
            device_id=device_id,
            data_dir=data_dir,
            now=t,
            preserve_backup=True,
        )
    except asyncio.CancelledError:
        staging.staged = prev_staged
        staging.record_apply_result("restore_cancelled", when=t)
        raise
    else:
        if result is not None and not result.ok:
            staging.staged = prev_staged

    if result is None:
        staging.record_apply_result("restore_failed", when=t)
        return ApplyResult(False, "restore_failed")

    if result.ok:
        staging.record_apply_result("restore_ok", when=t)
        return ApplyResult(True, "restore_ok", written_fields=result.written_fields)
    staging.record_apply_result("restore_failed", field=result.failed_field, when=t)
    return ApplyResult(False, "restore_failed", failed_field=result.failed_field, written_fields=result.written_fields)
