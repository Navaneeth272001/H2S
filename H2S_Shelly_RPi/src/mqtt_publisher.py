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
import paho.mqtt.client as mqtt

from .config import (
    MQTT_BROKER_HOST, MQTT_BROKER_PORT, MQTT_USER, MQTT_PASSWORD,
    MQTT_CLIENT_ID, AGENT_UUID,
    MQTT_TOPIC_TELEMETRIE, MQTT_TOPIC_COMPTES_RENDUS,
    MQTT_TOPIC_ETAT, MQTT_TOPIC_ORDRES
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

def _make_payload_dict(valeur, unite: str, capteur_id: str, timestamp=None) -> dict:
    if timestamp is None:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    elif hasattr(timestamp, "strftime"):
        ts = timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        ts = str(timestamp)
        if not ts.endswith("Z"):
            ts += "Z"

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
        payload = _make_payload_dict(valeur, unite, capteur_id, timestamp)
        _publish_raw(MQTT_TOPIC_TELEMETRIE, payload)

    logger.debug(f"Telemetrie published for {device_name} ({capteur_prefix})")

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
        payload = _make_payload_dict(valeur, unite, capteur_id, start_time)
        _publish_raw(MQTT_TOPIC_COMPTES_RENDUS, payload)

    logger.debug(f"Compte-rendu published for {device_name} ({capteur_prefix})")

def publish_etat(extra: dict = None):
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
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
