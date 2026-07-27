"""

HA mdi: icons https://pictogrammers.com/library/mdi/


"""
import asyncio
import json
import math
import queue
import re
import statistics
import time
import traceback
from unittest.mock import patch

import paho.mqtt.client as paho

from bmslib.bms import BmsSample, DeviceInfo, MIN_VALUE_EXPIRY
from bmslib.bms_ble.plugins.daly_full_decode import EXTRA_SENSOR_EXPIRY_SECONDS
from bmslib.bt import BtBms
from bmslib.util import get_logger

logger = get_logger()

no_publish_fail_warn = False


# We need to ensure that c encoder will not be launched
@patch('json.encoder.c_make_encoder', None)
def json_dumps_with_round_n(some_object, n=7):
    # saving original method
    of = json.encoder._make_iterencode

    def inner(*args, **kwargs):
        args = list(args)
        # fifth argument is float formater which will we replace
        args[4] = lambda o: str(round_to_n(o, n))
        return of(*args, **kwargs)

    with patch('json.encoder._make_iterencode', wraps=inner):
        return json.dumps(some_object)


def round_to_n(x, n):
    # todo compare to np.format_float_positional
    if isinstance(x, str) or not math.isfinite(x) or not x:
        return x

    if n == 0:
        return str(round(x, None))

    digits = -int(math.floor(math.log10(abs(x)))) + (n - 1)

    try:
        # return ('%.*f' % (digits, x))
        return str(round(x, digits or None))  # digits=0 will output 12.0, digits=None => 12
    except ValueError as e:
        print('error', x, n, e)
        raise e


def format_extra_numeric(val, precision: int | None = None) -> str:
    """Format extra sensor values with fixed decimal places (not significant digits)."""
    if isinstance(val, str):
        return val
    fval = float(val)
    if precision is None:
        if fval.is_integer():
            return str(int(fval))
        return str(val)
    p = int(precision)
    if p == 0:
        return str(int(round(fval)))
    return f"{fval:.{p}f}"


def capitalize_words(s):
    return ' '.join(word[0].upper() + word[1:] for word in s.split())


def disable_warnings():
    global no_publish_fail_warn
    no_publish_fail_warn = True


def remove_none_values(fields: dict):
    for k in list(fields.keys()):
        v = fields[k]
        if v is None:
            del fields[k]
        elif isinstance(v, float):
            if math.isnan(v) or not math.isfinite(v):
                del fields[k]
        elif isinstance(v, str):
            if not v:
                del fields[k]


def remove_equal_values(fields: dict, other: dict):
    if not other:
        return
    for k in list(fields.keys()):
        if k in other and fields[k] == other[k]:
            del fields[k]


_last_values = {}
_last_publish_time = 0.


def mqtt_single_out(client: paho.Client, topic, data, retain=False):
    # logger.debug(f'Send data: {data} on topic: {topic}, retain flag: {retain}')
    # print('mqtt: ' + topic, data)
    # return

    if client is None:
        # print('mqtt: ' + topic, data)
        return

    lv = _last_values.get(topic, None)
    if lv and lv[1] == data and (time.time() - lv[0]) < (MIN_VALUE_EXPIRY / 2):
        logger.debug('topic %s data not changed', topic)
        return False

    mqi: paho.MQTTMessageInfo = client.publish(topic, data, retain=retain)
    if mqi.rc != paho.MQTT_ERR_SUCCESS:
        if not no_publish_fail_warn:
            logger.warning('mqtt publish %s failed: %s %s', topic, mqi.rc, mqi)
        return False

    now = time.time()
    _last_values[topic] = now, data
    global _last_publish_time
    _last_publish_time = now


def mqtt_last_publish_time():
    global _last_publish_time
    return _last_publish_time


def is_none_or_nan(val):
    if val is None:
        return True
    if isinstance(val, float) and (math.isnan(val) or not math.isfinite(val)):
        return True
    return False


def is_valid_extra_topic_key(k: str) -> bool:
    if not k or "//" in k:
        return False
    parts = k.split("/")
    if len(parts) != 2 or parts[0] != "daly_config":
        return False
    return all(re.fullmatch(r"[A-Za-z0-9_.-]+", p) for p in parts)
    if val is None:
        return True
    if isinstance(val, float) and (math.isnan(val) or not math.isfinite(val)):
        return True
    return False


# units: https://github.com/home-assistant/core/blob/d7ac4bd65379e11461c7ce0893d3533d8d8b8cbf/homeassistant/const.py#L384
sample_desc = {
    "soc/total_voltage": {
        "field": "voltage",
        "device_class": "voltage",
        "state_class": "measurement",
        "unit_of_measurement": "V",
        "precision": 2,
        "significant_digits": 4,  # round_to_n
        "icon": "meter-electric"},
    "soc/current": {
        "field": "current",
        "device_class": "current",
        "state_class": "measurement",
        "unit_of_measurement": "A",
        "precision": 2,
        "significant_digits": 4,
    },
    "soc/balance_current": {
        "field": "balance_current",
        "device_class": "current",
        "state_class": "measurement",
        "unit_of_measurement": "A",
        "precision": 2,
        "significant_digits": 4,
        "icon": "scale-unbalanced"},
    "soc/soc_percent": {
        "field": "soc",
        "device_class": "battery",
        "state_class": "measurement",
        "unit_of_measurement": "%",
        "precision": 2,
        "significant_digits": 4,
        "icon": "battery"},
    "soc/power": {
        "field": "power",
        "device_class": "power",
        "state_class": "measurement",
        "unit_of_measurement": "W",
        "precision": 1,
        "significant_digits": 4,
        "icon": "flash"},
    "soc/capacity": {
        "field": "capacity",
        "device_class": None,
        "state_class": None,
        "unit_of_measurement": "Ah"
    },
    "soc/aged_capacity": {
        "field": "aged_capacity",
        "device_class": None,
        "state_class": None,
        "unit_of_measurement": "Ah",
        "precision": 2,
        "icon": "battery-heart-variant"},
    "soc/soh": {
        "field": "soh",
        "device_class": None,
        "state_class": "measurement",
        "unit_of_measurement": "%",
        "precision": 1,
        "icon": "battery-heart-variant"},
    # Topic key kept as ``soc/cycle_capacity`` (and therefore HA's unique_id /
    # entity_id) so existing user automations and long-term statistics keep
    # working across the rename. The HA display name is auto-derived from
    # ``field`` and will refresh to "Total Charge Throughput".
    "soc/cycle_capacity": {
        "field": "total_charge_throughput",
        "device_class": None,
        "state_class": None,
        "unit_of_measurement": "Ah"},
    "soc/num_cycles": {
        "field": "num_cycles",
        "device_class": None,
        "state_class": "measurement",
        "unit_of_measurement": "N",
        "icon": "battery-sync"},
    "mosfet_status/capacity_ah": {
        "field": "charge",
        "device_class": None,
        "state_class": None,
        "unit_of_measurement": "Ah"},
    "mosfet_status/temperature": {
        "field": "mos_temperature",
        "device_class": "temperature",
        "state_class": "measurement",
        "unit_of_measurement": "°C",
        "icon": "thermometer"},
    "bms/uptime": {
        "field": "uptime",
        "device_class": "duration",
        "state_class": "measurement",
        "unit_of_measurement": "s",
        "precision": 0,
        "icon": "clock"},
    "bms/runtime": {
        "field": "runtime",
        "device_class": "duration",
        "state_class": "measurement",
        "unit_of_measurement": "s",
        "precision": 0,
        "icon": "timer-sand"},
    "soc/total_charge_net": {
        "field": "total_charge_net",
        "device_class": None,
        "state_class": "total_increasing",
        "unit_of_measurement": "Ah",
        "icon": "battery-arrow-down"},
    "meter/sample_count": {
        "field": "num_samples",
        "device_class": None,
        "state_class": "measurement",
        "unit_of_measurement": "N",
        "icon": "counter"},
}


def publish_extra_values(client, device_topic, sample: BmsSample):
    if not sample.extra_values or not sample.extra_desc:
        return
    for topic_key, meta in sample.extra_desc.items():
        if not is_valid_extra_topic_key(topic_key):
            logger.warning("skip invalid extra sensor topic key %r", topic_key)
            continue
        field = meta.get("field")
        if not field:
            continue
        val = sample.extra_values.get(field)
        if val is None:
            continue
        if isinstance(val, float) and not math.isfinite(val):
            continue
        topic = f"{device_topic}/{topic_key}"
        if isinstance(val, bool):
            mqtt_single_out(client, topic, "ON" if val else "OFF")
        else:
            precision = meta.get("precision")
            if isinstance(val, (int, float)):
                val = format_extra_numeric(val, precision)
            mqtt_single_out(client, topic, val)


def publish_sample(client, device_topic, sample: BmsSample):
    for k, v in sample_desc.items():
        topic = f"{device_topic}/{k}"
        s = round_to_n(getattr(sample, v['field']), v.get('significant_digits', 5))
        if not is_none_or_nan(s):
            mqtt_single_out(client, topic, s)

    publish_extra_values(client, device_topic, sample)

    if sample.switches:
        for switch_name, switch_state in sample.switches.items():
            assert isinstance(switch_state, bool)
            topic = f"{device_topic}/switch/{switch_name}"
            mqtt_single_out(client, topic, 'ON' if switch_state else 'OFF')

    if sample.problem is not None:
        mqtt_single_out(client, f"{device_topic}/problem",
                        'ON' if sample.problem else 'OFF')
    if sample.problem_code is not None:
        mqtt_single_out(client, f"{device_topic}/problem_code", sample.problem_code)

    if sample.battery_charging is not None:
        mqtt_single_out(client, f"{device_topic}/battery_charging",
                        'ON' if sample.battery_charging else 'OFF')
    if sample.battery_mode is not None:
        mqtt_single_out(client, f"{device_topic}/battery_mode", sample.battery_mode)


def publish_cell_voltages(client, device_topic, voltages):
    # "highest_voltage": parts[0] / 1000,
    # "highest_cell": parts[1],
    # "lowest_voltage": parts[2] / 1000,
    # "lowest_cell": parts[3],

    if not voltages:
        return

    for i in range(0, len(voltages)):
        topic = f"{device_topic}/cell_voltages/{i + 1}"
        mqtt_single_out(client, topic, voltages[i] / 1000)

    if len(voltages) > 1:
        x = range(len(voltages))
        high_i = max(x, key=lambda i: voltages[i])
        low_i = min(x, key=lambda i: voltages[i])
        mqtt_single_out(client, f"{device_topic}/cell_voltages/min", voltages[low_i] / 1000)
        mqtt_single_out(client, f"{device_topic}/cell_voltages/min_index", low_i + 1)
        mqtt_single_out(client, f"{device_topic}/cell_voltages/max", voltages[high_i] / 1000)
        mqtt_single_out(client, f"{device_topic}/cell_voltages/max_index", high_i + 1)
        mqtt_single_out(client, f"{device_topic}/cell_voltages/delta", (voltages[high_i] - voltages[low_i]) / 1000)
        mqtt_single_out(client, f"{device_topic}/cell_voltages/average", round(sum(voltages) / len(voltages)) / 1000)
        mqtt_single_out(client, f"{device_topic}/cell_voltages/median", statistics.median(voltages) / 1000)


def publish_temperatures(client, device_topic, temperatures):
    if not temperatures:
        return
    for i in range(0, len(temperatures)):
        topic = f"{device_topic}/temperatures/{i + 1}"
        if not is_none_or_nan(temperatures[i]):
            mqtt_single_out(client, topic, round_to_n(temperatures[i], 4))


def publish_hass_discovery(client, device_topic, expire_after_seconds: int, sample: BmsSample, num_cells,
                           temperatures,
                           device_info: DeviceInfo = None):
    discovery_msg = {}

    # HA discovery node_id must match [a-zA-Z0-9_-] (no slashes), so flatten
    # any '/' in the alias. State topics below keep the original slashes.
    node_id = device_topic.replace('/', '_')

    device_json = {
        "identifiers": [(device_info and device_info.sn) or device_topic],
        "manufacturer": (device_info and device_info.mnf) or None,
        "name": f"{device_info.name} ({device_topic})" if (device_info and device_info.name) else device_topic,
        "model": (device_info and device_info.model) or None,
        "sw_version": (device_info and device_info.sw_version) or None,
        "hw_version": (device_info and device_info.hw_version) or None,
    }

    def _hass_discovery(k, device_class, unit, state_class=None, icon=None, name=None, long_expiry=False,
                        precision=None, entity_category=None):
        dm = {
            "unique_id": f"{device_topic}__{k.replace('/', '_')}",
            "name": name or capitalize_words(k.replace('/', ' ')),
            "device_class": device_class or None,
            "state_class": state_class or None,
            "unit_of_measurement": unit,
            "native_unit_of_measurement": unit,
            "suggested_unit_of_measurement": unit,
            "suggested_display_precision": precision,
            # "json_attributes_topic": f"{device_topic}/{k}",
            "state_topic": f"{device_topic}/{k}",
            "expire_after": max(expire_after_seconds, EXTRA_SENSOR_EXPIRY_SECONDS) if long_expiry else expire_after_seconds,
            "device": device_json,
        }
        if entity_category:
            dm["entity_category"] = entity_category
        if icon:
            dm['icon'] = 'mdi:' + icon
        remove_none_values(dm)
        remove_none_values(dm['device'])
        discovery_msg[f"homeassistant/sensor/{node_id}/_{k.replace('/', '_')}/config"] = dm

    def _hass_extra_discovery(topic_key: str, meta: dict):
        if not is_valid_extra_topic_key(topic_key):
            return
        field = meta.get("field")
        val = sample.extra_values.get(field) if sample.extra_values else None
        if val is None:
            return
        if isinstance(val, float) and not math.isfinite(val):
            return
        long_expiry = meta.get("long_expiry", True)
        expire = max(expire_after_seconds, EXTRA_SENSOR_EXPIRY_SECONDS) if long_expiry else expire_after_seconds
        entity_category = meta.get("entity_category", "diagnostic")
        name = meta.get("name") or capitalize_words(field.replace("_", " "))
        if isinstance(val, bool):
            discovery_msg[f"homeassistant/binary_sensor/{node_id}/_{topic_key.replace('/', '_')}/config"] = {
                "unique_id": f"{device_topic}__{topic_key.replace('/', '_')}",
                "name": name,
                "entity_category": entity_category,
                "state_topic": f"{device_topic}/{topic_key}",
                "expire_after": expire,
                "device": device_json,
            }
            return
        dm = {
            "unique_id": f"{device_topic}__{topic_key.replace('/', '_')}",
            "name": name,
            "device_class": meta.get("device_class"),
            "state_class": meta.get("state_class"),
            "unit_of_measurement": meta.get("unit_of_measurement"),
            "native_unit_of_measurement": meta.get("unit_of_measurement"),
            "suggested_display_precision": meta.get("precision"),
            "entity_category": entity_category,
            "state_topic": f"{device_topic}/{topic_key}",
            "expire_after": expire,
            "device": device_json,
        }
        remove_none_values(dm)
        remove_none_values(dm['device'])
        discovery_msg[f"homeassistant/sensor/{node_id}/_{topic_key.replace('/', '_')}/config"] = dm

    for k, d in sample_desc.items():
        if not is_none_or_nan(getattr(sample, d["field"])):
            _hass_discovery(k, d["device_class"],
                            state_class=d["state_class"],
                            unit=d["unit_of_measurement"],
                            icon=d.get('icon', None),
                            name=capitalize_words(d["field"]),
                            precision=d.get("precision", None)
                            )

    for i in range(0, num_cells):
        k = 'cell_voltages/%d' % (i + 1)
        n = 'Cell Volt %0*d' % (1 + int(math.log10(num_cells)), i + 1)
        _hass_discovery(k, "voltage", name=n, unit="V", precision=3)

    if num_cells > 1:
        statistic_fields = ["min", "max", "average", "median", "delta"]
        for f in statistic_fields:
            k = 'cell_voltages/%s' % f
            _hass_discovery(k, name="Cell Volt %s" % f, device_class="voltage", unit="V", precision=3)

        for f in ["min_index", "max_index"]:
            k = 'cell_voltages/%s' % f
            _hass_discovery(k, name="Cell Index %s" % f[:3], device_class=None, unit="")

    for i in range(0, len(temperatures or [])):
        k = 'temperatures/%d' % (i + 1)
        if not is_none_or_nan(temperatures[i]):
            _hass_discovery(k, "temperature", unit="°C", precision=1)

    meters = {
        # state_class see https://developers.home-assistant.io/docs/core/entity/sensor/#long-term-statistics
        # this enables the meters to appear in HA Energy Grid
        'total_energy': dict(device_class="energy", unit="kWh", icon="meter-electric", name="total energy netted"),
        # state_class="total",
        'total_energy_charge': dict(device_class="energy", state_class="total_increasing", unit="kWh",
                                    icon="meter-electric", name="total energy input"),
        'total_energy_discharge': dict(device_class="energy", state_class="total_increasing", unit="kWh",
                                       icon="meter-electric", name="total energy output"),
        'total_charge': dict(device_class=None, unit="Ah", name="total charge netted"),
        'total_cycles': dict(device_class=None, unit="N", icon="battery-sync", name="total cycle count"),
    }
    for name, m in meters.items():
        _hass_discovery('meter/%s' % name, **m, long_expiry=True, precision=2)

    if sample.extra_desc and sample.extra_values:
        for topic_key, meta in sample.extra_desc.items():
            _hass_extra_discovery(topic_key, meta)

    if sample.problem is not None:
        discovery_msg[f"homeassistant/binary_sensor/{node_id}/problem/config"] = {
            "unique_id": f"{device_topic}__problem",
            "name": "problem",
            "device_class": "problem",
            "entity_category": "diagnostic",
            "state_topic": f"{device_topic}/problem",
            "expire_after": expire_after_seconds,
            "device": device_json,
        }
    if sample.problem_code is not None:
        discovery_msg[f"homeassistant/sensor/{node_id}/problem_code/config"] = {
            "unique_id": f"{device_topic}__problem_code",
            "name": "problem code",
            "entity_category": "diagnostic",
            "state_topic": f"{device_topic}/problem_code",
            "expire_after": expire_after_seconds,
            "device": device_json,
            "icon": "mdi:alert-circle-outline",
        }

    if sample.battery_charging is not None:
        discovery_msg[f"homeassistant/binary_sensor/{node_id}/battery_charging/config"] = {
            "unique_id": f"{device_topic}__battery_charging",
            "name": "battery charging",
            "device_class": "battery_charging",
            "state_topic": f"{device_topic}/battery_charging",
            "expire_after": expire_after_seconds,
            "device": device_json,
        }
    if sample.battery_mode is not None:
        discovery_msg[f"homeassistant/sensor/{node_id}/battery_mode/config"] = {
            "unique_id": f"{device_topic}__battery_mode",
            "name": "battery mode",
            "device_class": "enum",
            "options": ["UNKNOWN", "BULK", "ABSORPTION", "FLOAT"],
            "state_topic": f"{device_topic}/battery_mode",
            "expire_after": expire_after_seconds,
            "device": device_json,
            "icon": "mdi:battery-charging-medium",
        }

    switches = (sample.switches and sample.switches.keys())
    switch_tombstones: list[str] = []
    if switches:
        for switch_name in switches:
            if sample.switches_writable:
                discovery_msg[f"homeassistant/switch/{node_id}/{switch_name}/config"] = {
                    "unique_id": f"{device_topic}__switch_{switch_name}",
                    "name": f"{switch_name}",
                    "device_class": 'outlet',
                    "state_topic": f"{device_topic}/switch/{switch_name}",
                    "expire_after": expire_after_seconds,
                    "device": device_json,
                    "command_topic": f"homeassistant/switch/{node_id}/{switch_name}/set",
                }

                discovery_msg[f"homeassistant/binary_sensor/{node_id}/{switch_name}/config"] = {
                    "unique_id": f"{device_topic}__switch_{switch_name}",
                    "name": f"{switch_name} switch",
                    "device_class": 'power',
                    "expire_after": expire_after_seconds,
                    "device": device_json,
                    "state_topic": f"{device_topic}/switch/{switch_name}",
                    "command_topic": f"homeassistant/switch/{node_id}/{switch_name}/set",
                }
            else:
                switch_tombstones.append(f"homeassistant/switch/{node_id}/{switch_name}/config")
                discovery_msg[f"homeassistant/binary_sensor/{node_id}/{switch_name}_state/config"] = {
                    "unique_id": f"{device_topic}__switch_{switch_name}_readonly",
                    "name": f"{switch_name} switch",
                    "device_class": 'power',
                    "entity_category": "diagnostic",
                    "expire_after": expire_after_seconds,
                    "device": device_json,
                    "state_topic": f"{device_topic}/switch/{switch_name}",
                }

    for topic, data in discovery_msg.items():
        j = json.dumps(data)
        logger.debug('discovery msg %s: %s', topic, j)
        mqtt_single_out(client, topic, j)

    for topic in switch_tombstones:
        logger.debug('discovery tombstone %s', topic)
        mqtt_single_out(client, topic, "", retain=True)


_switch_callbacks = {}
_message_queue = queue.Queue()


async def mqtt_process_action_queue():
    while not _message_queue.empty():
        callback, arg = _message_queue.get(block=False)
        try:
            await callback(arg)
        except Exception as e:
            logger.error('exception in action callback: %s', e)
            logger.error('Stack: %s', traceback.format_exc())
            await asyncio.sleep(1)


def subscribe_switches(mqtt_client: paho.Client, device_topic, bms: BtBms, switches):
    async def set_switch(switch_name: str, state: bool):
        assert isinstance(state, bool)
        logger.info('Set %s %s switch %s', bms.name, switch_name, state)
        await bms.set_switch(switch_name, state)
        topic = f"{device_topic}/switch/{switch_name}"
        mqtt_single_out(mqtt_client, topic, 'ON' if state else 'OFF')

    node_id = device_topic.replace('/', '_')
    for switch_name in switches:
        state_topic = f"homeassistant/switch/{node_id}/{switch_name}/set"
        logger.debug("subscribe %s", state_topic)
        mqtt_client.subscribe(state_topic, qos=2)
        _switch_callbacks[state_topic] = \
            lambda msg, sn=switch_name: set_switch(sn, msg.lower() == "on")


def mqtt_message_handler(client, userdata, message: paho.MQTTMessage):
    payload = message.payload.decode("utf-8")
    logger.info("received msg %s: %s", message.topic, payload)
    callback = _switch_callbacks.get(message.topic, None)
    if callback:
        _message_queue.put((callback, payload))
    else:
        logger.warning("No callback for topic %s (payload %s)", message.topic, payload)


def paho_monkey_patch():
    def _handle_pingresp(self):
        if self._in_packet['remaining_length'] != 0:
            return paho.MQTT_ERR_PROTOCOL

        # No longer waiting for a PINGRESP.
        # self._ping_t = 0
        self._easy_log(paho.MQTT_LOG_DEBUG, "Received PINGRESP (patched)")
        return paho.MQTT_ERR_SUCCESS

    paho.Client._handle_pingresp = _handle_pingresp

    logger.debug("applied paho monkey patch _handle_pingresp")
