# H2S_Shelly_RPi

This repository contains the data collection, federated learning client, and MQTT edge agent for the H2S Raspberry Pi gateways.

## Backend Integration Contracts (Confirmed)

The following specifications are confirmed and aligned with the backend `agent-sync` service.

### 1. MQTT Topics Structure
The topic hierarchy is versioned and isolated per gateway (no wildcard writes):
*   **Per-Minute Energy Telemetry**: `h2s/v1/telemetry/<gateway_id>/energy/minute`
*   **Hourly Energy Aggregation**: `h2s/v1/telemetry/<gateway_id>/energy/hourly`
*   **Hourly Weather**: `h2s/v1/telemetry/<gateway_id>/weather`
*   **Federated Learning Gradients**: `h2s/v1/fl/<gateway_id>/gradient`

### 2. Message Format & Encryption
*   **Envelope Structure**: All messages are wrapped in a standard JSON envelope containing `message_id`, `sequence`, `message_type`, `timestamp`, encryption metadata, and the `signature`.
*   **Payload Encryption**: The inner payload is encrypted end-to-end using **AES-256-GCM**. The backend handles schema versioning for decrypted payloads.

### 3. Device Signatures (TPM)
*   **Signing Method**: The device signs all outgoing MQTT message canonical strings using **ECDSA-SHA256**. 
*   **Key Storage**: The private key is secured within the physical Infineon SLB9672 TPM 2.0 module.
*   **Verification**: The backend maintains the public key registry to verify these signatures upon arrival.

### 4. Replay Protection & Buffering
*   **Replay Validation**: The backend validates uniqueness and freshness using the `message_id` + `sequence` + `timestamp` combination.
*   **Offline Buffering**: In the event of network or broker outages, the Raspberry Pi buffers data locally to disk. Buffered messages will be re-transmitted upon reconnection. 
*   *Note for Backend Replay Window*: The buffer is sized to retain up to **7 days** of offline telemetry. The replay window should accept timestamps at least 7 days old provided their sequence numbers have not been seen before.

### 5. Sensor Fields and Units
**Energy Measurements (Shelly):**
*   `current_a`: Current (Amperes)
*   `voltage_v`: Voltage (Volts)
*   `power_w`: Active Power (Watts)
*   `energy_minute_wh`: Accumulated Energy per minute (Watt-hours)
*   `total_energy_wh`: Total Energy per hour (Watt-hours)

**Weather Measurements (Open-Meteo):**
*   `temperature`: Temperature (Celsius)
*   `humidity`: Relative Humidity (%)
*   `wind_speed`: Wind Speed (m/s)
*   `precipitation`: Precipitation (mm)
*   `ghi`: Global Horizontal Irradiance (W/m²)
*   `condition`: Weather condition description (String)

### 6. Federated Learning Gradients
*   **Model**: LightGBM Regressor
*   **Format**: Histogram gradients and tree parameters serialized as NumPy arrays (transported via Flower framework).
*   **Size**: Estimated between **50 KB - 250 KB** per federated round update depending on tree depth and histogram bin configurations.
