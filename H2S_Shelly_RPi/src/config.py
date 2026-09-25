import os
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

# MQTT Broker Config (Cloud Server)
MQTT_BROKER_HOST = os.getenv("MQTT_BROKER_HOST", "mqtt.dev.h2splug.com")
MQTT_BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", 8883))
AGENT_UUID = os.getenv("AGENT_UUID", "123e4567-e89b-12d3-a456-426614174000")
MQTT_USER = os.getenv("MQTT_USER", AGENT_UUID)
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "secret")
MQTT_CLIENT_ID = os.getenv("MQTT_CLIENT_ID", AGENT_UUID)

# Topics according to backend agreement
MQTT_TOPIC_ENERGY_MINUTE = f"h2s/v1/telemetry/{AGENT_UUID}/energy/minute"
MQTT_TOPIC_ENERGY_HOURLY = f"h2s/v1/telemetry/{AGENT_UUID}/energy/hourly"
MQTT_TOPIC_WEATHER_HOURLY = f"h2s/v1/telemetry/{AGENT_UUID}/weather"
MQTT_TOPIC_FL_GRADIENT = f"h2s/v1/fl/{AGENT_UUID}/gradient"

# Encryption & Signing Keys
AES_DEK_HEX = os.getenv("AES_DEK_HEX", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")


# Shelly Devices
SHELLY_DEVICES = {
    2001: {"name": "TV Salle à manger", "ip": "192.168.1.10"},
    2002: {"name": "Ecran", "ip": "192.168.1.197"},
    2003: {"name": "Telephone / Tablette", "ip": "192.168.1.128"},
    2004: {"name": "Cafetiere", "ip": "192.168.1.103"},
    2005: {"name": "Lampe Salle à manger", "ip": "192.168.1.8"},
    2006: {"name": "PC Portable", "ip": "192.168.1.176"},
    2007: {"name": "Lampe Cuisine", "ip": "192.168.1.130"},
    2008: {"name": "Lampe Chambre", "ip": "192.168.1.157"},
}

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
