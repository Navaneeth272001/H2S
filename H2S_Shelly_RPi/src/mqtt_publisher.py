"""
MQTT Publisher — local broker
=================================================
Topics (per-agent ACL namespace):
    h2s/agents/<uuid>/telemetrie       Pi → cloud   QoS 1
    h2s/agents/<uuid>/comptes-rendus   Pi → cloud   QoS 1
    h2s/agents/<uuid>/etat             Pi → cloud   QoS 1
    h2s/agents/<uuid>/ordres           cloud → Pi   QoS 1   subscribe only

Payload format (JSON):
    Raw JSON. h2s_agent will sign, buffer, and forward to the cloud.
"""

import json
import logging
import time
import os
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from .config import (
    MQTT_BROKER_HOST, MQTT_BROKER_PORT, MQTT_USER, MQTT_PASSWORD,
    MQTT_CLIENT_ID, AGENT_UUID,
    MQTT_TOPIC_TELEMETRIE, MQTT_TOPIC_COMPTES_RENDUS,
    MQTT_TOPIC_ETAT, MQTT_TOPIC_ORDRES,
    MQTT_TOPIC_BATTERY_STATUS
)

logger = logging.getLogger(__name__)

_client = None
_ordres_callback = None
QOS = 1

# ---------------------------------------------------------------------------
#  MQTT client lifecycle
# ---------------------------------------------------------------------------

def _on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        logger.info(f"Connected to MQTT broker at {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}")
        client.subscribe(MQTT_TOPIC_ORDRES, qos=QOS)
        logger.info(f"Subscribed to {MQTT_TOPIC_ORDRES}")
    else:
        logger.error(f"MQTT connection failed with code {rc}")

def _on_disconnect(client, userdata, flags, rc, properties=None):
    if rc != 0:
        logger.warning(f"Unexpected MQTT disconnect (rc={rc}). Will auto-reconnect.")

def _on_message(client, userdata, msg):
    logger.info(f"Received order on {msg.topic}")
    if _ordres_callback is not None:
        try:
            # Pass the raw payload to the callback
            data = json.loads(msg.payload)
            _ordres_callback(data)
        except Exception as e:
            logger.error(f"Error processing incoming order: {e}")

def get_mqtt_client():
    global _client
    if _client is not None and _client.is_connected():
        return _client

    try:
        _client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=MQTT_CLIENT_ID,
            protocol=mqtt.MQTTv5
        )
        if MQTT_USER and MQTT_PASSWORD:
            _client.username_pw_set(MQTT_USER, MQTT_PASSWORD)

        _client.on_connect = _on_connect
        _client.on_disconnect = _on_disconnect
        _client.on_message = _on_message

        _client.reconnect_delay_set(min_delay=1, max_delay=60)
        _client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, keepalive=60)
        _client.loop_start()
        logger.info("MQTT client initialised and loop started.")
        return _client
    except Exception as e:
        logger.error(f"Failed to initialise MQTT client: {e}")
        _client = None
        return None

def set_ordres_callback(callback):
    global _ordres_callback
    _ordres_callback = callback

# ---------------------------------------------------------------------------
#  Internal publish helper
# ---------------------------------------------------------------------------

def _publish_raw(topic: str, payload_dict: dict):
    payload_json = json.dumps(payload_dict)
    if _client is not None and _client.is_connected():
        try:
            res = _client.publish(topic, payload_json, qos=QOS)
            res.wait_for_publish(timeout=5)
        except Exception as e:
            logger.error(f"Failed to publish to {topic}: {e}")
    else:
        logger.error(f"Cannot publish to {topic}, MQTT client not connected")

def _iso_utc(timestamp=None) -> str:
    """Instant ISO-8601 en UTC, suffixe « Z ».

    ``main_collector`` passe ``datetime.now(tz).replace(tzinfo=None)`` : un datetime NAÏF, à l'heure
    locale du boîtier. Le formater tel quel avec un « Z » annonce une mesure décalée du fuseau — cinq
    heures et demie dans le futur pour un Pi en UTC+5:30. h2s_agent ramène alors l'horodatage à l'heure
    de réception (marquée ESTIMATED) et le cloud refuse purement et simplement une mesure datée dans le
    futur. Un datetime naïf est donc interprété dans le fuseau local puis converti en UTC.
    """
    if timestamp is None:
        moment = datetime.now(timezone.utc)
    elif isinstance(timestamp, datetime):
        # datetime naïf : astimezone() suppose le fuseau local, puis convertit vers UTC.
        moment = timestamp.astimezone(timezone.utc)
    else:
        text = str(timestamp)
        return text if text.endswith("Z") else text + "Z"
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_payload_dict(valeur, unite: str, capteur_id: str, timestamp=None) -> dict:
    ts = _iso_utc(timestamp)

    return {
        "timestamp": ts,
        "valeur": valeur,
        "unite": unite,
        "capteur_id": capteur_id
    }

# ---------------------------------------------------------------------------
#  Public publish functions
# ---------------------------------------------------------------------------

def publish_energy_minute(device_id, device_name, current_a, voltage_v,
                          power_w, energy_minute_wh, timestamp):
    capteur_prefix = f"shelly-{device_id}"
    readings = [
        (power_w,          "W",  f"{capteur_prefix}-power"),
        (current_a,        "A",  f"{capteur_prefix}-current"),
        (voltage_v,        "V",  f"{capteur_prefix}-voltage"),
        (energy_minute_wh, "Wh", f"{capteur_prefix}-energy-minute"),
    ]

    for valeur, unite, capteur_id in readings:
        if valeur is None:
            continue
        payload = _make_payload_dict(valeur, unite, capteur_id, timestamp)
        _publish_raw(MQTT_TOPIC_TELEMETRIE, payload)

    logger.debug(f"Telemetrie published for {device_name} ({capteur_prefix})")

def publish_device_status_h2s(device_id: int, current_a: float, voltage_v: float, power_w: float, energy_wh: float, timestamp=None):
    """
    Publish device status in H2S JSON gateway format consumed by h2s_agent (parsers.h2s_device).
    """
    ts = _iso_utc(timestamp)

    device_payload = {
        "timestamp": ts,
        "device_id": device_id,
    }
    if power_w is not None:
        device_payload["power_w"] = round(float(power_w), 2)
    if current_a is not None:
        device_payload["current_a"] = round(float(current_a), 3)
    if voltage_v is not None:
        device_payload["voltage_v"] = round(float(voltage_v), 1)
    if energy_wh is not None:
        device_payload["energy_wh"] = round(float(energy_wh), 3)
    if len(device_payload) == 2:
        logger.warning("Skipping empty Shelly status for device %s", device_id)
        return
    topic = f"h2s/devices/{device_id}/status"
    _publish_raw(topic, device_payload)
    logger.debug(f"Device status published to {topic}")

def publish_weather_hourly(temperature, humidity, wind_speed, precipitation,
                           condition, ghi, timestamp):
    readings = [
        (temperature,   "°C",   "weather-temperature"),
        (humidity,       "%",   "weather-humidity"),
        (wind_speed,    "m/s",  "weather-wind-speed"),
        (precipitation, "mm",   "weather-precipitation"),
        (ghi,           "W/m²", "weather-ghi"),
    ]

    for valeur, unite, capteur_id in readings:
        payload = _make_payload_dict(valeur, unite, capteur_id, timestamp)
        _publish_raw(MQTT_TOPIC_TELEMETRIE, payload)

    condition_payload = _make_payload_dict(condition, "", "weather-condition", timestamp)
    _publish_raw(MQTT_TOPIC_COMPTES_RENDUS, condition_payload)
    logger.debug("Weather telemetrie published")

def publish_energy_hourly(device_id, device_name, avg_current_a, avg_voltage_v,
                          avg_power_w, total_energy_wh, start_time, end_time):
    capteur_prefix = f"shelly-{device_id}"
    readings = [
        (avg_power_w,       "W",  f"{capteur_prefix}-avg-power"),
        (avg_current_a,     "A",  f"{capteur_prefix}-avg-current"),
        (avg_voltage_v,     "V",  f"{capteur_prefix}-avg-voltage"),
        (total_energy_wh,   "Wh", f"{capteur_prefix}-total-energy"),
    ]

    for valeur, unite, capteur_id in readings:
        if valeur is None:
            continue
        payload = _make_payload_dict(valeur, unite, capteur_id, start_time)
        _publish_raw(MQTT_TOPIC_COMPTES_RENDUS, payload)

    logger.debug(f"Compte-rendu published for {device_name} ({capteur_prefix})")

def publish_battery_minute(stock_id: int, battery_name: str, soc_pct: float,
                           power_charge_w: float, power_discharge_w: float,
                           voltage_v: float, current_a: float, temperature_c: float,
                           timestamp=None):
    """
    Publish granular battery readings to telemetry topic in standard H2S sensor packet format.
    """
    capteur_prefix = f"battery-{stock_id}"
    readings = [
        (round(soc_pct, 2),             "%",   f"{capteur_prefix}-soc"),
        (round(power_charge_w, 1),      "W",   f"{capteur_prefix}-charge-power"),
        (round(power_discharge_w, 1),   "W",   f"{capteur_prefix}-discharge-power"),
        (round(voltage_v, 1),           "V",   f"{capteur_prefix}-voltage"),
        (round(current_a, 3),           "A",   f"{capteur_prefix}-current"),
        (round(temperature_c, 1),       "°C",  f"{capteur_prefix}-temperature"),
    ]

    for valeur, unite, capteur_id in readings:
        payload = _make_payload_dict(valeur, unite, capteur_id, timestamp)
        _publish_raw(MQTT_TOPIC_TELEMETRIE, payload)

    logger.debug(f"Battery telemetrie published for {battery_name} ({capteur_prefix})")

def publish_battery_status_h2s(stock_id: int, soc_pct: float,
                               power_charge_w: float, power_discharge_w: float,
                               voltage_v: float, current_a: float, temperature_c: float,
                               timestamp=None):
    """
    Publish full status payload to local Mosquitto topic (e.g. h2s/site4/battery/status)
    so h2s_agent's collector ingests it directly for local storage, signed buffer, and live telemetry.
    """
    ts = _iso_utc(timestamp)

    battery_payload = {
        "timestamp": ts,
        "stock_id": stock_id,
        "soc_pct": round(soc_pct, 2),
        "power_charge_w": round(power_charge_w, 1),
        "power_discharge_w": round(power_discharge_w, 1),
        "voltage_v": round(voltage_v, 1),
        "current_a": round(current_a, 3),
        "temperature_c": round(temperature_c, 1),
    }
    _publish_raw(MQTT_TOPIC_BATTERY_STATUS, battery_payload)
    logger.debug(f"H2S local battery status published to {MQTT_TOPIC_BATTERY_STATUS}")

def publish_decision_report(action: str, details: dict, timestamp=None):
    """
    Publish relay execution and decision outcome to comptes-rendus topic.
    """
    report_payload = _make_payload_dict(
        valeur=json.dumps(details),
        unite="",
        capteur_id=f"decision-{action.lower()}",
        timestamp=timestamp
    )
    _publish_raw(MQTT_TOPIC_COMPTES_RENDUS, report_payload)
    logger.info(f"Decision report for {action} published to {MQTT_TOPIC_COMPTES_RENDUS}")

def publish_etat(extra: dict = None):
    ts = _iso_utc()
    etat_payload = {
        "timestamp": ts,
        "valeur": 1,
        "unite": "",
        "capteur_id": f"agent-{AGENT_UUID}"
    }
    if extra:
        etat_payload.update(extra)

    _publish_raw(MQTT_TOPIC_ETAT, etat_payload)
    logger.debug("Heartbeat (etat) published")

def disconnect_mqtt():
    global _client
    if _client is not None:
        try:
            _client.loop_stop()
            _client.disconnect()
            logger.info("MQTT client disconnected gracefully.")
        except Exception as e:
            logger.error(f"Error disconnecting MQTT client: {e}")
        finally:
            _client = None
