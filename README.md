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

---

## Getting Started (Development & DevOps)

The following guide will help you set up the Edge Agent locally for development and prepare it for production deployments.

### Prerequisites
* Docker & Docker Compose
* Python 3.11+ (if running natively)

### 1. Environment Setup
You need to configure your environment variables before running the agent. 
Navigate to the edge agent directory and copy the example environment file:
```bash
cd H2S_Shelly_RPi
cp .env.example .env
```
Open `.env` and fill in the required MQTT, Database, and SMTP credentials.

### 2. Running via Docker (Recommended for DevOps)
The repository is fully Dockerized for easy deployment on the Raspberry Pi. The provided `docker-compose.yml` spins up both the **PostgreSQL Database** and the **Python Edge Agent**.

To build and start the services in detached mode:
```bash
cd H2S_Shelly_RPi
docker-compose up -d --build
```
To view the logs of the collector agent:
```bash
docker-compose logs -f collector
```

### 3. Local Native Development (Dev)
If you prefer to run the agent without Docker for local debugging:

1. Start just the database:
   ```bash
   cd H2S_Shelly_RPi
   docker-compose up -d postgres
   ```
2. Set up a Python virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```
3. Install the dependencies (requires `libomp-dev` and `libpq-dev` system packages):
   ```bash
   pip install -r requirements.txt
   ```
4. Run the collector:
   ```bash
   python main_collector.py
   ```

### 4. CI/CD Pipeline
This repository includes a basic GitHub Actions pipeline (`.github/workflows/ci.yml`) that triggers on pushes to the `main` branch. 
It automatically:
* Checks out the code.
* Sets up the Python 3.11 environment.
* Installs dependencies.
* Runs a syntax and linting check using `flake8`.
