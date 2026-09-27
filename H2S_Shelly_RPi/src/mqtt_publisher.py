"""
MQTT Publisher — DevOps onboarding spec
========================================
Topics (per-agent ACL namespace):
    h2s/agents/<uuid>/telemetrie       Pi → cloud   QoS 1   sensor readings
    h2s/agents/<uuid>/comptes-rendus   Pi → cloud   QoS 1   activity reports
    h2s/agents/<uuid>/etat             Pi → cloud   QoS 1   status / heartbeat
    h2s/agents/<uuid>/ordres           cloud → Pi   QoS 1   subscribe only

Payload format (JSON):
    {
        "timestamp": "2026-09-25T14:30:00Z",   # ISO-8601 UTC
        "valeur":    12.5,                      # numeric value
        "unite":     "kWh",                     # unit string
        "capteur_id": "solar-panel-01"          # unique per sensor
    }
"""

import json
import logging
import time
import os
import sqlite3
import threading
import paho.mqtt.client as mqtt
import ssl

from .config import (
    MQTT_BROKER_HOST, MQTT_BROKER_PORT, MQTT_USER, MQTT_PASSWORD,
    MQTT_CLIENT_ID, AGENT_UUID,
    MQTT_TOPIC_TELEMETRIE, MQTT_TOPIC_COMPTES_RENDUS,
    MQTT_TOPIC_ETAT, MQTT_TOPIC_ORDRES
)

logger = logging.getLogger(__name__)

_client = None
_ordres_callback = None          # user-supplied callback for incoming orders
BUFFER_DB = "mqtt_buffer.db"
QOS = 1                           # DevOps spec: all topics use QoS 1

# ---------------------------------------------------------------------------
#  Offline buffer (SQLite) — retains up to 7 days of unsent messages
# ---------------------------------------------------------------------------

def _init_buffer_db():
    with sqlite3.connect(BUFFER_DB) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at REAL NOT NULL,
                retry_count INTEGER DEFAULT 0
            )
        ''')
        conn.commit()

_init_buffer_db()


def _save_to_buffer(topic: str, payload: str):
    with sqlite3.connect(BUFFER_DB) as conn:
        conn.execute(
            "INSERT INTO messages (topic, payload, created_at) VALUES (?, ?, ?)",
            (topic, payload, time.time())
        )
        # Prune messages older than 7 days
        conn.execute(
            "DELETE FROM messages WHERE created_at < ?",
            (time.time() - 7 * 24 * 3600,)
        )
        conn.commit()


def _flush_buffer():
    """Re-transmit buffered messages after reconnection."""
    if _client is None or not _client.is_connected():
        return

    with sqlite3.connect(BUFFER_DB) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, topic, payload FROM messages ORDER BY id ASC")
        rows = cursor.fetchall()

        for row_id, topic, payload in rows:
            if not _client.is_connected():
                break
            try:
                res = _client.publish(topic, payload, qos=QOS)
                res.wait_for_publish(timeout=5)
                if res.rc == mqtt.MQTT_ERR_SUCCESS:
                    conn.execute("DELETE FROM messages WHERE id=?", (row_id,))
            except Exception as e:
                logger.error(f"Failed to flush buffered message {row_id}: {e}")
                break
        conn.commit()

# ---------------------------------------------------------------------------
#  MQTT client lifecycle
# ---------------------------------------------------------------------------

def _on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        logger.info(f"Connected to MQTT broker at {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}")

        # Subscribe to cloud → Pi orders topic
        client.subscribe(MQTT_TOPIC_ORDRES, qos=QOS)
        logger.info(f"Subscribed to {MQTT_TOPIC_ORDRES}")

        # Flush any offline-buffered messages
        threading.Thread(target=_flush_buffer, daemon=True).start()
    else:
        logger.error(f"MQTT connection failed with code {rc}")


def _on_disconnect(client, userdata, flags, rc, properties=None):
    if rc != 0:
        logger.warning(f"Unexpected MQTT disconnect (rc={rc}). Will auto-reconnect.")


def _on_message(client, userdata, msg):
    """Handle incoming messages on the ordres topic."""
    logger.info(f"Received order on {msg.topic}: {msg.payload.decode('utf-8', errors='replace')}")
    if _ordres_callback is not None:
        try:
            data = json.loads(msg.payload)
            _ordres_callback(data)
        except Exception as e:
            logger.error(f"Error processing incoming order: {e}")


def get_mqtt_client():
    """Get (or create) the singleton MQTT client with TLS 1.3."""
    global _client
    if _client is not None and _client.is_connected():
        return _client

    try:
        _client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=MQTT_CLIENT_ID,
            protocol=mqtt.MQTTv5
        )
        _client.username_pw_set(MQTT_USER, MQTT_PASSWORD)

        # TLS 1.3 — Let's Encrypt root, system CA store (no custom CA needed)
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_context.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1 | ssl.OP_NO_TLSv1_2
        ssl_context.load_default_certs()
        _client.tls_set_context(ssl_context)

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
    """Register a handler for incoming cloud → Pi orders."""
    global _ordres_callback
    _ordres_callback = callback

# ---------------------------------------------------------------------------
#  Payload builder (DevOps format)
# ---------------------------------------------------------------------------

def _make_payload(valeur, unite: str, capteur_id: str, timestamp=None) -> str:
    """
    Build a JSON payload conforming to the DevOps spec.

    {
        "timestamp":   ISO-8601 UTC string,
        "valeur":      numeric sensor value,
        "unite":       unit string  ("W", "A", "V", "Wh", "kWh", "%", …),
        "capteur_id":  unique sensor identifier
    }
    """
    if timestamp is None:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    elif hasattr(timestamp, "strftime"):
        ts = timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        ts = str(timestamp)
        if not ts.endswith("Z"):
            ts += "Z"

    return json.dumps({
        "timestamp": ts,
        "valeur": valeur,
        "unite": unite,
        "capteur_id": capteur_id
    })

# ---------------------------------------------------------------------------
#  Internal publish helper
# ---------------------------------------------------------------------------

def _publish(topic: str, payload_json: str):
    """Buffer-first publish: save to SQLite, then attempt immediate send."""
    _save_to_buffer(topic, payload_json)
    threading.Thread(target=_flush_buffer, daemon=True).start()

# ---------------------------------------------------------------------------
#  Public publish functions — telemetrie
# ---------------------------------------------------------------------------

def publish_energy_minute(device_id, device_name, current_a, voltage_v,
                          power_w, energy_minute_wh, timestamp):
    """
    Publish per-minute Shelly energy readings to the telemetrie topic.
    Each measurement becomes a separate DevOps-format message.
    """
    capteur_prefix = f"shelly-{device_id}"

    readings = [
        (power_w,          "W",  f"{capteur_prefix}-power"),
        (current_a,        "A",  f"{capteur_prefix}-current"),
        (voltage_v,        "V",  f"{capteur_prefix}-voltage"),
        (energy_minute_wh, "Wh", f"{capteur_prefix}-energy-minute"),
    ]

    for valeur, unite, capteur_id in readings:
        payload = _make_payload(valeur, unite, capteur_id, timestamp)
        _publish(MQTT_TOPIC_TELEMETRIE, payload)

    logger.debug(f"Telemetrie published for {device_name} ({capteur_prefix})")


def publish_weather_hourly(temperature, humidity, wind_speed, precipitation,
                           condition, ghi, timestamp):
    """
    Publish hourly weather data to the telemetrie topic.
    """
    readings = [
        (temperature,   "°C",   "weather-temperature"),
        (humidity,       "%",   "weather-humidity"),
        (wind_speed,    "m/s",  "weather-wind-speed"),
        (precipitation, "mm",   "weather-precipitation"),
        (ghi,           "W/m²", "weather-ghi"),
    ]

    for valeur, unite, capteur_id in readings:
        payload = _make_payload(valeur, unite, capteur_id, timestamp)
        _publish(MQTT_TOPIC_TELEMETRIE, payload)

    # Weather condition (string) → publish as a compte-rendu
    condition_payload = json.dumps({
        "timestamp": timestamp.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(timestamp, "strftime")
                     else str(timestamp),
        "valeur": condition,
        "unite": "",
        "capteur_id": "weather-condition"
    })
    _publish(MQTT_TOPIC_COMPTES_RENDUS, condition_payload)

    logger.debug("Weather telemetrie published")

# ---------------------------------------------------------------------------
#  Public publish functions — comptes-rendus (activity reports)
# ---------------------------------------------------------------------------

def publish_energy_hourly(device_id, device_name, avg_current_a, avg_voltage_v,
                          avg_power_w, total_energy_wh, start_time, end_time):
    """
    Publish hourly aggregated energy to the comptes-rendus topic.
    """
    capteur_prefix = f"shelly-{device_id}"

    readings = [
        (avg_power_w,       "W",  f"{capteur_prefix}-avg-power"),
        (avg_current_a,     "A",  f"{capteur_prefix}-avg-current"),
        (avg_voltage_v,     "V",  f"{capteur_prefix}-avg-voltage"),
        (total_energy_wh,   "Wh", f"{capteur_prefix}-total-energy"),
    ]

    for valeur, unite, capteur_id in readings:
        payload = _make_payload(valeur, unite, capteur_id, start_time)
        _publish(MQTT_TOPIC_COMPTES_RENDUS, payload)

    logger.debug(f"Compte-rendu published for {device_name} ({capteur_prefix})")

# ---------------------------------------------------------------------------
#  Public publish functions — etat (heartbeat / status)
# ---------------------------------------------------------------------------

def publish_etat(extra: dict = None):
    """
    Publish agent heartbeat / status to the etat topic.
    Called every HEARTBEAT_INTERVAL seconds (recommended 60 s).

    `extra` can carry optional status fields (e.g. battery_pct, uptime_s).
    """
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    etat_payload = {
        "timestamp": ts,
        "valeur": 1,               # 1 = alive
        "unite": "",
        "capteur_id": f"agent-{AGENT_UUID}"
    }

    # Merge any additional status data
    if extra:
        etat_payload.update(extra)

    payload_json = json.dumps(etat_payload)
    _publish(MQTT_TOPIC_ETAT, payload_json)
    logger.debug("Heartbeat (etat) published")

# ---------------------------------------------------------------------------
#  Disconnect
# ---------------------------------------------------------------------------

def disconnect_mqtt():
    """Gracefully stop the MQTT client loop and disconnect."""
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
