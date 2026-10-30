import os
import json
from dotenv import load_dotenv

load_dotenv()

# Database Config
POSTGRES_USER = os.getenv("POSTGRES_USER", "h2s_user")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "h2s_pass")
POSTGRES_DB = os.getenv("POSTGRES_DB", "h2s_db")
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")
DATABASE_URL = f"postgresql+asyncpg://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"

# Email Config
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
EMAIL_TO = os.getenv("EMAIL_TO", "")

# App Config
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 60))
MINUTES_PER_HOUR = int(os.getenv("MINUTES_PER_HOUR", 60))
MAX_ERRORS = int(os.getenv("MAX_ERRORS", 2))

# Weather Config
LAT = float(os.getenv("LAT", 48.8566))
LON = float(os.getenv("LON", 2.3522))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Paris")

# MQTT Broker Config (Local Mosquitto)
MQTT_BROKER_HOST = os.getenv("MQTT_BROKER_HOST", "localhost")
MQTT_BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", 1883))
AGENT_UUID = os.getenv("AGENT_UUID", "123e4567-e89b-12d3-a456-426614174000")
MQTT_USER = os.getenv("MQTT_USER", AGENT_UUID)
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "secret")
MQTT_CLIENT_ID = os.getenv("MQTT_CLIENT_ID", AGENT_UUID)

# MQTT Topics — DevOps-specified per-agent namespace
# ACL enforces that each agent can only access h2s/agents/<its-own-uuid>/…
MQTT_TOPIC_TELEMETRIE     = f"h2s/agents/{AGENT_UUID}/telemetrie"       # Pi → cloud: sensor readings
MQTT_TOPIC_COMPTES_RENDUS = f"h2s/agents/{AGENT_UUID}/comptes-rendus"   # Pi → cloud: activity reports
MQTT_TOPIC_ETAT           = f"h2s/agents/{AGENT_UUID}/etat"             # Pi → cloud: status / heartbeat
MQTT_TOPIC_ORDRES         = f"h2s/agents/{AGENT_UUID}/ordres"           # cloud → Pi: subscribe only

# Heartbeat interval (seconds) — DevOps recommends 60s for etat
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", 60))

# Battery Configuration (Physical Jackery & Simulation — Chemin B)
BATTERY_ENABLED = os.getenv("BATTERY_ENABLED", "true").lower() in ("true", "1", "yes")
BATTERY_STOCK_ID = int(os.getenv("BATTERY_STOCK_ID", "1"))
BATTERY_NAME = os.getenv("BATTERY_NAME", "Jackery Explorer 1000")
BATTERY_NOMINAL_CAPACITY_WH = float(os.getenv("BATTERY_NOMINAL_CAPACITY_WH", "1070.0"))
BATTERY_VOLTAGE_NOMINAL = float(os.getenv("BATTERY_VOLTAGE_NOMINAL", "36.0"))
BATTERY_MAX_POWER_W = float(os.getenv("BATTERY_MAX_POWER_W", "1000.0"))
BATTERY_INITIAL_SOC = float(os.getenv("BATTERY_INITIAL_SOC", "85.0"))
# Peak virtual renewable generation at 1,000 W/m² irradiance; no panels are assumed installed.
BATTERY_SIMULATED_CHARGE_W = float(os.getenv("BATTERY_SIMULATED_CHARGE_W", "500.0"))

# Optional Jackery telemetry through the third-party ``socketry`` client.
# Keep simulation as the default until the operator explicitly enables it.
BATTERY_SOURCE = os.getenv("BATTERY_SOURCE", "simulation").strip().lower()
JACKERY_EMAIL = os.getenv("JACKERY_EMAIL", "")
JACKERY_PASSWORD = os.getenv("JACKERY_PASSWORD", "")
JACKERY_DEVICE_SN = os.getenv("JACKERY_DEVICE_SN", "")

# Local Mosquitto topic for battery status (consumed by h2s_agent)
MQTT_TOPIC_BATTERY_STATUS = os.getenv("MQTT_TOPIC_BATTERY_STATUS", "h2s/site4/battery/status")
# Local Mosquitto topic prefix where h2s_agent publishes battery commands (/charge, /discharge, /standby)
MQTT_TOPIC_BATTERY_COMMANDS = os.getenv("MQTT_TOPIC_BATTERY_COMMANDS", "h2s/site4/commands")

# Local Mosquitto topic for weather, in the H2S gateway format read by h2s_agent
# (parsers.h2s_weather). The legacy per-sensor packets on the agent telemetry topic
# are not subscribed by h2s_agent, so weather never reached the cloud that way.
MQTT_TOPIC_WEATHER_STATUS = os.getenv("MQTT_TOPIC_WEATHER_STATUS", "h2s/site4/weather/status")

# Shelly configuration is overridable as JSON so channel/role assumptions stay
# in deployment config. The built-in map preserves the existing installation.
_legacy_shelly_devices = {
    8: {"name": "LIVING ROOM LAMP", "ip": "192.168.1.8", "role": "non_critical"},
    9: {"name": "LIVING ROOM - TV", "ip": "192.168.1.10", "role": "priority"},
    10: {"name": "KITCHEN - COFFEE MACHINE", "ip": "192.168.1.103", "role": "non_critical"},
    11: {"name": "KITCHEN - LAMP", "ip": "192.168.1.130", "role": "non_critical"},
    12: {"name": "BED ROOM - LAMP", "ip": "192.168.1.157", "role": "non_critical"},
    13: {"name": "BED ROOM - PC SCREEN", "ip": "192.168.1.197", "role": "priority"},
    14: {"name": "BED ROOM - COMPUTER", "ip": "192.168.1.176", "role": "priority"},
    15: {"name": "BED ROOM - MOBILE PHONE/TABLET CHARGER", "ip": "192.168.1.128", "role": "priority"},
}


def _load_shelly_devices() -> dict[int, dict]:
    raw = os.getenv("SHELLY_DEVICES_JSON", "").strip()
    devices = json.loads(raw) if raw else _legacy_shelly_devices
    if not isinstance(devices, dict):
        raise ValueError("SHELLY_DEVICES_JSON must be a JSON object keyed by H2S device ID")

    normalized = {}
    for key, value in devices.items():
        device_id = int(key)
        if not isinstance(value, dict) or not value.get("ip"):
            raise ValueError(f"Shelly device {key!r} must be an object with an ip")
        item = dict(value)
        # Keep the prior `priority` boolean accepted for existing local configs.
        role = item.get("role")
        if role is None:
            role = "priority" if item.get("priority") else "non_critical"
        if role not in ("priority", "non_critical", "monitor_only"):
            raise ValueError(f"Shelly device {key!r} has unsupported role {role!r}")
        protocol = str(item.get("protocol", "gen2_rpc"))
        if protocol != "gen2_rpc":
            raise ValueError(f"Shelly device {key!r} has unsupported protocol {protocol!r}; configured adapter supports gen2_rpc")
        switch_id = int(item.get("switch_id", 0))
        if switch_id < 0:
            raise ValueError(f"Shelly device {key!r} switch_id cannot be negative")
        item.update({
            "name": str(item.get("name", f"Shelly {device_id}")),
            "ip": str(item["ip"]),
            "role": role,
            "switch_id": switch_id,
            "measurement_component": str(item.get("measurement_component", f"switch:{switch_id}")),
            "protocol": protocol,
        })
        normalized[device_id] = item
    return normalized


SHELLY_DEVICES = _load_shelly_devices()
SHELLY_PRIORITY_IDS = [i for i, d in SHELLY_DEVICES.items() if d["role"] == "priority"]
SHELLY_NON_CRITICAL_IDS = [i for i, d in SHELLY_DEVICES.items() if d["role"] == "non_critical"]
SHELLY_EXECUTION_MODE = os.getenv("SHELLY_EXECUTION_MODE", "simulation").strip().lower()
if SHELLY_EXECUTION_MODE not in ("simulation", "dry_run", "hardware"):
    raise ValueError("SHELLY_EXECUTION_MODE must be simulation, dry_run, or hardware")
SHELLY_PHYSICAL_COMMANDS_ENABLED = os.getenv("SHELLY_PHYSICAL_COMMANDS_ENABLED", "false").lower() in ("true", "1", "yes")
SHELLY_COMMAND_TIMEOUT_SECONDS = float(os.getenv("SHELLY_COMMAND_TIMEOUT_SECONDS", "5"))
SHELLY_MAX_RETRIES = max(0, int(os.getenv("SHELLY_MAX_RETRIES", "2")))
SHELLY_EFFECT_THRESHOLD_W = max(0.0, float(os.getenv("SHELLY_EFFECT_THRESHOLD_W", "5")))

# Renewable production sources (digital twin)
# -----------------------------------------------------------------------------
# Production is NOT a field of the payload: the cloud decides whether a
# "source: device" reading counts as production (solar / wind) or consumption
# from the role declared on the site for that device. Every simulated source
# therefore publishes under its own device_id, and that device must be declared
# on the site as SOLAR or WIND. An undeclared device_id makes the cloud reject
# the record (fail-closed), which is why the ids are configuration, never guessed.
#
# Physics of the client specification (§4.1.4):
#   P_PV   = G × S × η_PV
#   P_wind = ½ × ρ × A × V³ × η_wind,  A = π × (D / 2)²
AIR_DENSITY_KG_M3 = 1.225
PV_EFFICIENCY_DEFAULT = 0.22
WIND_EFFICIENCY_DEFAULT = 0.35

# AC side the production meters read (inverter output).
PRODUCTION_AC_VOLTAGE_V = float(os.getenv("PRODUCTION_AC_VOLTAGE_V", "230.0"))
PRODUCTION_POWER_FACTOR = float(os.getenv("PRODUCTION_POWER_FACTOR", "0.98"))
# Below this output an inverter is considered asleep and reports 0 W.
PRODUCTION_MIN_OUTPUT_W = max(0.0, float(os.getenv("PRODUCTION_MIN_OUTPUT_W", "5")))

_legacy_production_sources = {
    29: {"name": "ROOFTOP PV (SIMULATED)", "type": "SOLAR", "surface_m2": 10.0, "efficiency": 0.22},
    30: {
        "name": "WIND TURBINE (SIMULATED)",
        "type": "WIND",
        "rotor_diameter_m": 3.0,
        "efficiency": 0.35,
        "cut_in_m_s": 3.0,
        "cut_out_m_s": 25.0,
        "rated_power_w": 2000.0,
    },
}


def _positive(value, what: str, key: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"production source {key!r}: {what} must be a number") from None
    if number <= 0:
        raise ValueError(f"production source {key!r}: {what} must be positive")
    return number


def _load_production_sources() -> dict[int, dict]:
    """Simulated production sources from ``PRODUCTION_SOURCES_JSON``.

    An empty JSON object disables the feature and the collector then reports the
    battery only, exactly as before. Nothing is invented at runtime: a source
    missing a physical characteristic is a configuration error, not a silently
    zero-producing source.
    """
    raw = os.getenv("PRODUCTION_SOURCES_JSON", "").strip()
    sources = json.loads(raw) if raw else dict(_legacy_production_sources)
    if not isinstance(sources, dict):
        raise ValueError("PRODUCTION_SOURCES_JSON must be a JSON object keyed by H2S device ID")

    # If /etc/h2s-agent/agent.json exists on the Pi and declares additional
    # PRODUCTION device IDs, include them so no declared production source is silent.
    agent_cfg_path = os.getenv("H2S_AGENT_CONFIG", "/etc/h2s-agent/agent.json")
    if not raw and os.path.isfile(agent_cfg_path):
        try:
            with open(agent_cfg_path, encoding="utf-8") as fh:
                agent_cfg = json.load(fh)
            agent_sources = {
                int(s["source_id"]): s
                for s in (agent_cfg.get("sources") or [])
                if isinstance(s, dict) and s.get("source_id") is not None
            }
            prod_devs = [
                int(d["device_id"])
                for d in (agent_cfg.get("devices") or [])
                if isinstance(d, dict)
                and str(d.get("role", "")).upper() == "PRODUCTION"
                and d.get("device_id") is not None
            ]
            for idx, dev_id in enumerate(prod_devs):
                if dev_id in sources or str(dev_id) in sources:
                    continue
                src_meta = agent_sources.get(dev_id, {})
                stype = str(src_meta.get("type", "SOLAR" if idx % 2 == 0 else "WIND")).upper()
                if stype == "SOLAR":
                    sources[dev_id] = {
                        "name": f"ROOFTOP PV {dev_id} (SIMULATED)",
                        "type": "SOLAR",
                        "surface_m2": float(src_meta.get("surface_m2") or 10.0),
                        "efficiency": float(src_meta.get("rendement_pv") or PV_EFFICIENCY_DEFAULT),
                    }
                else:
                    sources[dev_id] = {
                        "name": f"WIND TURBINE {dev_id} (SIMULATED)",
                        "type": "WIND",
                        "rotor_diameter_m": float(src_meta.get("diametre_rotor_m") or 3.0),
                        "efficiency": float(src_meta.get("rendement_eolien") or WIND_EFFICIENCY_DEFAULT),
                        "cut_in_m_s": float(src_meta.get("vent_demarrage_m_s") or 3.0),
                        "cut_out_m_s": float(src_meta.get("vent_arret_m_s") or 25.0),
                        "rated_power_w": float(src_meta.get("puissance_nominale_w") or 2000.0),
                    }
        except Exception:
            pass

    normalized: dict[int, dict] = {}
    for key, value in sources.items():
        device_id = int(key)
        if not isinstance(value, dict):
            raise ValueError(f"production source {key!r} must be an object")
        source_type = str(value.get("type", "")).upper()
        if source_type not in ("SOLAR", "WIND"):
            raise ValueError(f"production source {key!r}: type must be SOLAR or WIND ({source_type!r})")
        default_efficiency = PV_EFFICIENCY_DEFAULT if source_type == "SOLAR" else WIND_EFFICIENCY_DEFAULT
        item = {
            "device_id": device_id,
            "name": str(value.get("name", f"{source_type.title()} {device_id}")),
            "type": source_type,
            "efficiency": float(value.get("efficiency", default_efficiency)),
        }
        if not 0.0 < item["efficiency"] <= 1.0:
            raise ValueError(f"production source {key!r}: efficiency must be in (0, 1]")
        if source_type == "SOLAR":
            item["surface_m2"] = _positive(value.get("surface_m2"), "surface_m2", key)
        else:
            item["rotor_diameter_m"] = _positive(value.get("rotor_diameter_m"), "rotor_diameter_m", key)
            item["cut_in_m_s"] = max(0.0, float(value.get("cut_in_m_s", 0.0)))
            cut_out = float(value.get("cut_out_m_s", 0.0))
            item["cut_out_m_s"] = cut_out if cut_out > 0 else None
            rated = value.get("rated_power_w")
            item["rated_power_w"] = _positive(rated, "rated_power_w", key) if rated is not None else None
            if item["cut_out_m_s"] is not None and item["cut_out_m_s"] <= item["cut_in_m_s"]:
                raise ValueError(f"production source {key!r}: cut_out_m_s must exceed cut_in_m_s")
        normalized[device_id] = item
    return normalized


PRODUCTION_SOURCES = _load_production_sources()
PRODUCTION_ENABLED = bool(PRODUCTION_SOURCES)

WEATHER_CODES = {
    0: "Clear Sky",
    1: "Mainly Clear",
    2: "Partly Cloudy",
    3: "Cloudy",
    45: "Fog",
    48: "Fog",
    51: "Drizzle",
    61: "Rain",
    71: "Snow",
    80: "Rain Showers"
}
