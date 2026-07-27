"""Home Assistant MQTT discovery and action routing for Daly D2 writable settings."""

from __future__ import annotations

import json
import queue
import traceback
from typing import Any, Awaitable, Callable, Optional

import paho.mqtt.client as paho

from bmslib.bms_ble.plugins.daly_full_staging import DalyStagingState
from bmslib.bms_ble.plugins.daly_full_write_registry import (
    WRITE_FIELDS,
    EntityType,
)
from bmslib.mqtt_util import capitalize_words, mqtt_single_out, remove_none_values
from bmslib.util import get_logger

logger = get_logger()

_daly_action_callbacks: dict[str, Callable[[str], Awaitable[None]]] = {}
_daly_message_queue: queue.Queue = queue.Queue()

ACTION_APPLY = "apply"
ACTION_DISCARD = "discard"
ACTION_RESTORE = "restore"
ACTION_ARM = "arm_advanced"
ACTION_RESTART = "restart"


def _entity_slug(key: str) -> str:
    return "daly_%s" % key


def _write_prefix(device_topic: str) -> str:
    return "%s/daly_write" % device_topic


def _field_state_topic(device_topic: str, key: str) -> str:
    return "%s/%s/state" % (_write_prefix(device_topic), key)


def _field_command_topic(device_topic: str, key: str) -> str:
    return "%s/%s/set" % (_write_prefix(device_topic), key)


def _diagnostics_topic(device_topic: str) -> str:
    return "%s/diagnostics" % _write_prefix(device_topic)


def _arm_state_topic(device_topic: str) -> str:
    return "%s/arm_advanced/state" % _write_prefix(device_topic)


def _arm_command_topic(device_topic: str) -> str:
    return "%s/arm_advanced/set" % _write_prefix(device_topic)


def _button_command_topic(device_topic: str, action: str) -> str:
    return "%s/%s" % (_write_prefix(device_topic), action)


def _tombstone_sensor_topic(node_id: str, topic_key: str) -> str:
    return "homeassistant/sensor/%s/_%s/config" % (node_id, topic_key.replace("/", "_"))


def build_daly_full_discovery(
    device_topic: str,
    device_json: dict,
    expire_after_seconds: int,
) -> dict[str, Optional[dict]]:
    node_id = device_topic.replace("/", "_")
    discovery: dict[str, Optional[dict]] = {}
    writable_keys = {f.readback_key for f in WRITE_FIELDS if f.entity_type != EntityType.BUTTON}

    for field in WRITE_FIELDS:
        if field.entity_type == EntityType.BUTTON:
            if field.key != "system_restart":
                continue
            discovery["homeassistant/button/%s/%s/config" % (node_id, _entity_slug(field.key))] = {
                "unique_id": "%s__%s" % (device_topic, field.key),
                "name": "Restart DALY BMS",
                "entity_category": "config",
                "device": device_json,
                "command_topic": _button_command_topic(device_topic, ACTION_RESTART),
            }
            continue

        slug = _entity_slug(field.key)
        base = {
            "unique_id": "%s__write_%s" % (device_topic, field.key),
            "name": capitalize_words(field.key.replace("_", " ")),
            "entity_category": "config",
            "device": device_json,
            "state_topic": _field_state_topic(device_topic, field.key),
            "command_topic": _field_command_topic(device_topic, field.key),
            "expire_after": expire_after_seconds,
        }
        if field.entity_type == EntityType.NUMBER:
            dm = dict(base)
            dm["mode"] = "box"
            if field.unit:
                dm["unit_of_measurement"] = field.unit
            if field.min_value is not None:
                dm["min"] = field.min_value
            if field.max_value is not None:
                dm["max"] = field.max_value
            dm["step"] = 10 ** (-field.precision) if field.precision else 1
            discovery["homeassistant/number/%s/%s/config" % (node_id, slug)] = dm
        elif field.entity_type == EntityType.SELECT:
            dm = dict(base)
            dm["options"] = list(field.options or ())
            discovery["homeassistant/select/%s/%s/config" % (node_id, slug)] = dm

    for action_name, label in (
        (ACTION_APPLY, "Apply Pending DALY Settings"),
        (ACTION_DISCARD, "Discard Pending DALY Settings"),
        (ACTION_RESTORE, "Restore Last DALY Settings"),
    ):
        discovery["homeassistant/button/%s/daly_%s/config" % (node_id, action_name)] = {
            "unique_id": "%s__daly_%s" % (device_topic, action_name),
            "name": label,
            "entity_category": "config",
            "device": device_json,
            "command_topic": _button_command_topic(device_topic, action_name),
        }

    discovery["homeassistant/switch/%s/daly_arm_advanced/config" % node_id] = {
        "unique_id": "%s__daly_arm_advanced" % device_topic,
        "name": "Arm Advanced DALY Operations",
        "entity_category": "config",
        "device": device_json,
        "state_topic": _arm_state_topic(device_topic),
        "command_topic": _arm_command_topic(device_topic),
        "expire_after": expire_after_seconds,
    }

    discovery["homeassistant/sensor/%s/daly_pending_diagnostics/config" % node_id] = {
        "unique_id": "%s__daly_pending_diagnostics" % device_topic,
        "name": "DALY Pending Settings Diagnostics",
        "entity_category": "diagnostic",
        "device": device_json,
        "state_topic": _diagnostics_topic(device_topic),
        "expire_after": expire_after_seconds,
        "icon": "mdi:clipboard-list-outline",
    }

    for key in writable_keys:
        topic_key = "daly_config/%s" % key
        discovery[_tombstone_sensor_topic(node_id, topic_key)] = None

    return discovery


def writable_discovery_config_topics(device_topic: str) -> tuple[str, ...]:
    msgs = build_daly_full_discovery(device_topic, {}, 60)
    return tuple(t for t, data in msgs.items() if data is not None)


def publish_daly_full_discovery(client, device_topic: str, device_json: dict, expire_after_seconds: int) -> None:
    msgs = build_daly_full_discovery(device_topic, device_json, expire_after_seconds)
    for topic, data in msgs.items():
        if data is None:
            mqtt_single_out(client, topic, "", retain=True)
            continue
        remove_none_values(data)
        remove_none_values(data.get("device", {}))
        mqtt_single_out(client, topic, json.dumps(data), retain=True)


def publish_daly_full_tombstones(client, device_topic: str) -> None:
    for topic in writable_discovery_config_topics(device_topic):
        mqtt_single_out(client, topic, "", retain=True)
    node_id = device_topic.replace("/", "_")
    writable_keys = {f.readback_key for f in WRITE_FIELDS if f.entity_type != EntityType.BUTTON}
    for key in writable_keys:
        mqtt_single_out(client, _tombstone_sensor_topic(node_id, "daly_config/%s" % key), "", retain=True)


def publish_daly_full_state(
    client,
    device_topic: str,
    staging: DalyStagingState,
    current_values: dict[str, Any],
) -> None:
    for field in WRITE_FIELDS:
        if field.entity_type == EntityType.BUTTON:
            continue
        val = staging.staged.get(field.key, current_values.get(field.key))
        if val is None:
            continue
        if isinstance(val, float):
            topic_val = ("%." + str(field.precision) + "f") % val if field.precision else str(val)
        else:
            topic_val = str(val)
        mqtt_single_out(client, _field_state_topic(device_topic, field.key), topic_val)

    diag = staging.diagnostics()
    mqtt_single_out(client, _diagnostics_topic(device_topic), json.dumps(diag))
    mqtt_single_out(
        client,
        _arm_state_topic(device_topic),
        "ON" if diag.get("armed") else "OFF",
    )


def subscribe_daly_full_controls(
    mqtt_client: paho.Client,
    device_topic: str,
    handlers: dict[str, Callable[[str], Awaitable[None]]],
    staging: Optional[DalyStagingState] = None,
) -> None:
    node_id = device_topic.replace("/", "_")

    def _wrap(action: str, handler: Callable[[str], Awaitable[None]]) -> Callable[[str], Awaitable[None]]:
        async def _safe(payload: str) -> None:
            try:
                await handler(payload)
            except Exception as exc:
                logger.error("daly action %s failed: %s", action, type(exc).__name__)
                if staging is not None:
                    staging.record_apply_result("action_failed", field=action)
        return _safe

    def _bind(topic: str, action: str) -> None:
        handler = handlers.get(action)
        if handler is None:
            return
        mqtt_client.subscribe(topic, qos=2)
        _daly_action_callbacks[topic] = _wrap(action, handler)

    for field in WRITE_FIELDS:
        if field.entity_type in (EntityType.NUMBER, EntityType.SELECT):
            _bind(_field_command_topic(device_topic, field.key), field.key)

    _bind(_arm_command_topic(device_topic), ACTION_ARM)
    for action in (ACTION_APPLY, ACTION_DISCARD, ACTION_RESTORE, ACTION_RESTART):
        _bind(_button_command_topic(device_topic, action), action)

    logger.debug("subscribed daly_full controls for %s", node_id)


def is_daly_command_topic(topic: str) -> bool:
    return topic in _daly_action_callbacks


async def mqtt_process_daly_action_queue() -> None:
    while not _daly_message_queue.empty():
        callback, arg = _daly_message_queue.get(block=False)
        try:
            await callback(arg)
        except Exception as exc:
            logger.error("daly action queue handler failed: %s", type(exc).__name__)
            logger.debug("daly action queue traceback: %s", traceback.format_exc())


def enqueue_daly_action(topic: str, payload: bytes, *, retain: bool = False) -> bool:
    if topic not in _daly_action_callbacks:
        return False
    if retain:
        logger.info("ignored retained daly command on topic %s", topic)
        return True
    try:
        text = payload.decode("utf-8")
    except UnicodeError:
        logger.warning("daly command payload not utf-8 on topic %s", topic)
        return True
    _daly_message_queue.put((_daly_action_callbacks[topic], text))
    return True


def daly_action_topics_for_device(device_topic: str) -> set[str]:
    topics = {
        _arm_command_topic(device_topic),
        _button_command_topic(device_topic, ACTION_APPLY),
        _button_command_topic(device_topic, ACTION_DISCARD),
        _button_command_topic(device_topic, ACTION_RESTORE),
        _button_command_topic(device_topic, ACTION_RESTART),
    }
    for field in WRITE_FIELDS:
        if field.entity_type in (EntityType.NUMBER, EntityType.SELECT):
            topics.add(_field_command_topic(device_topic, field.key))
    return topics
