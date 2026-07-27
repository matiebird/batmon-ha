"""Stage desired Daly D2 settings without writing to hardware."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from bmslib.bms_ble.plugins.daly_full_write_registry import (
    WRITE_FIELDS_BY_KEY,
    EntityType,
    build_write_plan,
)

ARM_EXPIRY_SECONDS: float = 60.0


@dataclass
class DalyStagingState:
    current: dict[str, Any] = field(default_factory=dict)
    staged: dict[str, Any] = field(default_factory=dict)
    armed_until: float = 0.0
    last_apply_status: str = "idle"
    last_apply_field: Optional[str] = None
    last_apply_timestamp: Optional[float] = None

    def update_current(self, values: Mapping[str, Any]) -> None:
        self.current = {k: values[k] for k in WRITE_FIELDS_BY_KEY if k in values}

    def stage(self, key: str, value: Any) -> None:
        if key not in WRITE_FIELDS_BY_KEY:
            raise ValueError("unknown writable field %r" % key)
        field_def = WRITE_FIELDS_BY_KEY[key]
        if field_def.entity_type == EntityType.BUTTON:
            raise ValueError("button fields cannot be staged")
        field_def.validate(value)
        self.staged[key] = value

    def discard(self) -> None:
        self.staged.clear()

    def arm_advanced(self, now: Optional[float] = None) -> None:
        t = now if now is not None else time.time()
        self.armed_until = t + ARM_EXPIRY_SECONDS

    def disarm_advanced(self) -> None:
        self.armed_until = 0.0

    def is_armed(self, now: Optional[float] = None) -> bool:
        t = now if now is not None else time.time()
        return t < self.armed_until

    def pending_keys(self) -> tuple[str, ...]:
        return tuple(self.staged.keys())

    def build_apply_plan(self, *, now: Optional[float] = None):
        return build_write_plan(
            self.current,
            self.staged,
            armed=self.is_armed(now),
        )

    def record_apply_result(self, status: str, *, field: Optional[str] = None, when: Optional[float] = None) -> None:
        self.last_apply_status = status
        self.last_apply_field = field
        self.last_apply_timestamp = when if when is not None else time.time()

    def diagnostics(self, now: Optional[float] = None) -> dict[str, Any]:
        t = now if now is not None else time.time()
        return {
            "pending_count": len(self.staged),
            "pending_fields": list(self.staged.keys()),
            "armed": self.is_armed(t),
            "armed_seconds_remaining": max(0.0, self.armed_until - t) if self.is_armed(t) else 0.0,
            "last_apply_status": self.last_apply_status,
            "last_apply_field": self.last_apply_field,
            "last_apply_timestamp": self.last_apply_timestamp,
        }
