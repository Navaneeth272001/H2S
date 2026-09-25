import json
import logging
import time
import base64
import os
import uuid
import sqlite3
import threading
import paho.mqtt.client as mqtt
import ssl
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.exceptions import InvalidSignature

from .config import (
    MQTT_BROKER_HOST, MQTT_BROKER_PORT, MQTT_USER, MQTT_PASSWORD,
    MQTT_CLIENT_ID, AGENT_UUID, MQTT_TOPIC_ENERGY_MINUTE,
    MQTT_TOPIC_ENERGY_HOURLY, MQTT_TOPIC_WEATHER_HOURLY, AES_DEK_HEX
)

logger = logging.getLogger(__name__)

_client = None
BUFFER_DB = "mqtt_buffer.db"

# We need an ECDSA P-256 private key for signing as confirmed by the backend.
# Normally loaded from TPM (Infineon SLB9672). Fallback to local file for dev.
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

def init_db():
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

init_db()

def get_next_sequence():
    with sqlite3.connect(BUFFER_DB) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE sequence SET seq_val = seq_val + 1 WHERE id = 1")
        cursor.execute("SELECT seq_val FROM sequence WHERE id = 1")
        return cursor.fetchone()[0]

def save_to_buffer(topic, payload):
    with sqlite3.connect(BUFFER_DB) as conn:
        conn.execute("INSERT INTO messages (topic, payload, created_at) VALUES (?, ?, ?)",
                     (topic, payload, time.time()))
        # Delete older than 7 days (offline buffering size logic)
        conn.execute("DELETE FROM messages WHERE created_at < ?", (time.time() - 7*24*3600,))
        conn.commit()

def flush_buffer():
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
                res = _client.publish(topic, payload, qos=1)
                res.wait_for_publish(timeout=5)
                if res.rc == mqtt.MQTT_ERR_SUCCESS:
                    conn.execute("DELETE FROM messages WHERE id=?", (row_id,))
            except Exception as e:
                logger.error(f"Failed to flush message {row_id}: {e}")
                break
        conn.commit()

def _on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        logger.info(f"Connected to MQTT broker at {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}")
        # Start a thread to flush buffer to avoid blocking the network loop
        threading.Thread(target=flush_buffer, daemon=True).start()
    else:
        logger.error(f"MQTT connection failed with code {rc}")

def _on_disconnect(client, userdata, flags, rc, properties=None):
    if rc != 0:
        logger.warning(f"Unexpected MQTT disconnect (rc={rc}). Will auto-reconnect.")

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
        
        # Configure TLS 1.3
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_context.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1 | ssl.OP_NO_TLSv1_2
        ssl_context.load_default_certs()
        _client.tls_set_context(ssl_context)

        _client.on_connect = _on_connect
        _client.on_disconnect = _on_disconnect

        _client.reconnect_delay_set(min_delay=1, max_delay=60)
        _client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, keepalive=60)
        _client.loop_start()
        logger.info("MQTT client initialised and loop started.")
        return _client
    except Exception as e:
        logger.error(f"Failed to initialise MQTT client: {e}")
        _client = None
        return None

def create_envelope(message_type: str, exact_json_payload: str, timestamp_iso: str) -> str:
    """Create envelope with AES-GCM encryption and ECDSA-SHA256 signature"""
    message_id = str(uuid.uuid4())
    sequence = get_next_sequence()
    
    # Encrypt payload
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

    # Sign the message (ECDSA-SHA256)
    # The payload to sign can be the concatenation of key fields to prevent tampering
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

def _publish(topic, message_type, raw_payload_dict):
    client = get_mqtt_client()
    
    ts_str = raw_payload_dict.get("timestamp", raw_payload_dict.get("start_time", ""))
    if not ts_str:
        ts_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        
    # Ensure UTC timezone suffix 'Z' if missing (ISO-8601 requirement from backend)
    if not ts_str.endswith("Z"):
        ts_str = ts_str + "Z"
        
    raw_payload_json = json.dumps(raw_payload_dict)
    
    try:
        envelope_json = create_envelope(message_type, raw_payload_json, ts_str)
    except Exception as e:
        logger.error(f"Failed to create secure envelope: {e}")
        return

    # Buffer offline, then flush
    save_to_buffer(topic, envelope_json)
    
    # Try flushing immediately
    threading.Thread(target=flush_buffer, daemon=True).start()

def publish_energy_minute(device_id, device_name, current_a, voltage_v, power_w, energy_minute_wh, timestamp):
    payload = {
        "device_id": str(device_id),
        "device_name": device_name,
        "current_a": current_a,
        "voltage_v": voltage_v,
        "power_w": power_w,
        "energy_minute_wh": energy_minute_wh,
        "timestamp": timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    }
    _publish(MQTT_TOPIC_ENERGY_MINUTE, "energy_minute", payload)

def publish_energy_hourly(device_id, device_name, avg_current_a, avg_voltage_v, avg_power_w, total_energy_wh, start_time, end_time):
    payload = {
        "device_id": str(device_id),
        "device_name": device_name,
        "avg_current_a": avg_current_a,
        "avg_voltage_v": avg_voltage_v,
        "avg_power_w": avg_power_w,
        "total_energy_wh": total_energy_wh,
        "start_time": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_time": end_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    }
    _publish(MQTT_TOPIC_ENERGY_HOURLY, "energy_hourly", payload)

def publish_weather_hourly(temperature, humidity, wind_speed, precipitation, condition, ghi, timestamp):
    payload = {
        "temperature": temperature,
        "humidity": humidity,
        "wind_speed": wind_speed,
        "precipitation": precipitation,
        "condition": condition,
        "ghi": ghi,
        "timestamp": timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    }
    _publish(MQTT_TOPIC_WEATHER_HOURLY, "weather_hourly", payload)

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
