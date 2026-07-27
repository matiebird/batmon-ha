"""Server-side backup of verified Daly D2 settings (no password registers)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional

from bmslib.bms_ble.plugins.daly_full_decode import FACTORY_PARAMETER_PASSWORD
from bmslib.bms_ble.plugins.daly_full_write_registry import WRITE_FIELDS_BY_KEY

PASSWORD_KEYS: frozenset[str] = frozenset({
    "parameter_password_configured",
    "parameter_password_factory_default",
})
DEFAULT_DATA_DIR = Path("/data")


def backup_path(device_id: str, data_dir: Path | None = None) -> Path:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in device_id)
    root = data_dir if data_dir is not None else DEFAULT_DATA_DIR
    return root / ("daly_full_backup_%s.json" % safe)


def _filter_backup_values(values: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in WRITE_FIELDS_BY_KEY:
        if key in values:
            out[key] = values[key]
    return out


def save_backup(
    device_id: str,
    values: Mapping[str, Any],
    *,
    changed_fields: tuple[str, ...],
    timestamp: float,
    data_dir: Path | None = None,
) -> Path:
    path = backup_path(device_id, data_dir)
    payload = {
        "timestamp": timestamp,
        "changed_fields": list(changed_fields),
        "values": _filter_backup_values(values),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".daly_backup_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    os.chmod(path, 0o600)
    return path


def load_backup(device_id: str, data_dir: Path | None = None) -> Optional[dict[str, Any]]:
    path = backup_path(device_id, data_dir)
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("invalid backup format")
    values = data.get("values")
    if not isinstance(values, dict):
        raise ValueError("invalid backup values")
    for forbidden in PASSWORD_KEYS:
        values.pop(forbidden, None)
    if FACTORY_PARAMETER_PASSWORD in json.dumps(values):
        raise ValueError("backup must not contain password material")
    return data


def backup_contains_password_material(text: str) -> bool:
    return FACTORY_PARAMETER_PASSWORD in text or "parameter_password" in text
