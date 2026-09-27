"""
MQTT Publisher — DevOps spec WITH secure envelope
=================================================
Topics (per-agent ACL namespace):
    h2s/agents/<uuid>/telemetrie       Pi → cloud   QoS 1
    h2s/agents/<uuid>/comptes-rendus   Pi → cloud   QoS 1
    h2s/agents/<uuid>/etat             Pi → cloud   QoS 1
    h2s/agents/<uuid>/ordres           cloud → Pi   QoS 1   subscribe only

Payload format (JSON):
    Wrapped in AES-256-GCM encryption and signed with ECDSA-SHA256.
"""

import json
import logging
import time
import os
import uuid
import base64
import sqlite3
import threading
import paho.mqtt.client as mqtt
import ssl

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization, hashes

from .config import (
    MQTT_BROKER_HOST, MQTT_BROKER_PORT, MQTT_USER, MQTT_PASSWORD,
    MQTT_CLIENT_ID, AGENT_UUID, AES_DEK_HEX,
    MQTT_TOPIC_TELEMETRIE, MQTT_TOPIC_COMPTES_RENDUS,
    MQTT_TOPIC_ETAT, MQTT_TOPIC_ORDRES
)

logger = logging.getLogger(__name__)

_client = None
_ordres_callback = None
BUFFER_DB = "mqtt_buffer.db"
QOS = 1

# ---------------------------------------------------------------------------
#  Cryptographic Key (Software fallback for TPM)
# ---------------------------------------------------------------------------
try:
    if os.path.exists("dev_ecdsa_key.pem"):
        with open("dev_ecdsa_key.pem", "rb") as f:
            private_key = serialization.load_pem_private_key(f.read(), password=None)
    else:
        private_key = ec.generate_private_key(ec.SECP256R1())
        with open("dev_ecdsa_key.pem", "wb") as f:
            f.write(private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption()
            ))
except Exception as e:
    logger.warning(f"Could not load/generate ECDSA key: {e}")
    private_key = None

# ---------------------------------------------------------------------------
#  Offline buffer (SQLite)
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
        conn.execute('''
            CREATE TABLE IF NOT EXISTS sequence (
                id INTEGER PRIMARY KEY,
                seq_val INTEGER NOT NULL
            )
        ''')
        conn.execute('INSERT OR IGNORE INTO sequence (id, seq_val) VALUES (1, 1)')
        conn.commit()

_init_buffer_db()

def get_next_sequence():
    with sqlite3.connect(BUFFER_DB) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE sequence SET seq_val = seq_val + 1 WHERE id = 1")
        cursor.execute("SELECT seq_val FROM sequence WHERE id = 1")
        return cursor.fetchone()[0]

def _save_to_buffer(topic: str, payload: str):
    with sqlite3.connect(BUFFER_DB) as conn:
        conn.execute(
            "INSERT INTO messages (topic, payload, created_at) VALUES (?, ?, ?)",
            (topic, payload, time.time())
        )
        conn.execute(
            "DELETE FROM messages WHERE created_at < ?",
            (time.time() - 7 * 24 * 3600,)
        )
        conn.commit()

def _flush_buffer():
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
#  Encryption & Envelope
# ---------------------------------------------------------------------------

def create_envelope(message_type: str, exact_json_payload: str, timestamp_iso: str) -> str:
    message_id = str(uuid.uuid4())
    sequence = get_next_sequence()
    
    try:
        aesgcm = AESGCM(bytes.fromhex(AES_DEK_HEX))
        nonce_bytes = os.urandom(12)
        aad = f"{AGENT_UUID}{timestamp_iso}1".encode('utf-8')
        ct = aesgcm.encrypt(nonce_bytes, exact_json_payload.encode('utf-8'), aad)
        
        ciphertext = ct[:-16]
        tag = ct[-16:]
        
        nonce_b64 = base64.b64encode(nonce_bytes).decode('utf-8')
        ciphertext_b64 = base64.b64encode(ciphertext).decode('utf-8')
        tag_b64 = base64.b64encode(tag).decode('utf-8')
    except Exception as e:
        logger.error(f"Encryption failed: {e}")
        raise e

    # Sign the envelope
    to_sign = f"{message_id}{sequence}{timestamp_iso}{ciphertext_b64}".encode('utf-8')
    if private_key:
        sig = private_key.sign(to_sign, ec.ECDSA(hashes.SHA256()))
        sig_b64 = base64.b64encode(sig).decode('utf-8')
    else:
        sig_b64 = "DUMMY_SIG"

    envelope = {
        "version": 1,
        "device_id": AGENT_UUID,
        "message_id": message_id,
        "sequence": sequence,
        "message_type": message_type,
        "timestamp": timestamp_iso,
        "key_id": f"{AGENT_UUID}_key_01",
        "algorithm": "AES-256-GCM",
        "nonce": nonce_b64,
        "ciphertext": ciphertext_b64,
        "tag": tag_b64,
        "signature_algorithm": "ECDSA-SHA256",
        "signature": sig_b64
    }
    return json.dumps(envelope)

# ---------------------------------------------------------------------------
#  MQTT client lifecycle
# ---------------------------------------------------------------------------

def _on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        logger.info(f"Connected to MQTT broker at {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}")
        client.subscribe(MQTT_TOPIC_ORDRES, qos=QOS)
        logger.info(f"Subscribed to {MQTT_TOPIC_ORDRES}")
        threading.Thread(target=_flush_buffer, daemon=True).start()
    else:
        logger.error(f"MQTT connection failed with code {rc}")

def _on_disconnect(client, userdata, flags, rc, properties=None):
    if rc != 0:
        logger.warning(f"Unexpected MQTT disconnect (rc={rc}). Will auto-reconnect.")

def _on_message(client, userdata, msg):
    logger.info(f"Received order on {msg.topic}")
    if _ordres_callback is not None:
        try:
            # Here we would normally decrypt and verify the order envelope
            # For now, just pass the raw payload
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
        _client.username_pw_set(MQTT_USER, MQTT_PASSWORD)

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
    global _ordres_callback
    _ordres_callback = callback

# ---------------------------------------------------------------------------
#  Internal publish helper
# ---------------------------------------------------------------------------

def _publish_secure(topic: str, message_type: str, payload_dict: dict):
    ts_str = payload_dict.get("timestamp")
    payload_json = json.dumps(payload_dict)
    
    try:
        envelope_json = create_envelope(message_type, payload_json, ts_str)
    except Exception as e:
        logger.error(f"Failed to create secure envelope: {e}")
        return

    _save_to_buffer(topic, envelope_json)
    threading.Thread(target=_flush_buffer, daemon=True).start()

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
        _publish_secure(MQTT_TOPIC_TELEMETRIE, "energy_minute", payload)

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
        _publish_secure(MQTT_TOPIC_TELEMETRIE, "weather_hourly", payload)

    condition_payload = _make_payload_dict(condition, "", "weather-condition", timestamp)
    _publish_secure(MQTT_TOPIC_COMPTES_RENDUS, "weather_hourly", condition_payload)
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
        _publish_secure(MQTT_TOPIC_COMPTES_RENDUS, "energy_hourly", payload)

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

    _publish_secure(MQTT_TOPIC_ETAT, "etat", etat_payload)
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
