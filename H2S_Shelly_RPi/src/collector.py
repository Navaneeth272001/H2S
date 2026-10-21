import aiohttp
import logging
from datetime import datetime
from .config import LAT, LON, TIMEZONE, WEATHER_CODES
from zoneinfo import ZoneInfo
from .shelly_rpc import ShellyRPCClient, ShellyRPCError

logger = logging.getLogger(__name__)
_shelly_rpc = ShellyRPCClient()

async def fetch_shelly(session: aiohttp.ClientSession, device_id: int, device_info: dict,
                       rpc_client: ShellyRPCClient | None = None):
    """Read actual relay and configured component telemetry; absent fields stay unavailable."""
    try:
        reading = await (rpc_client or _shelly_rpc).read_device(session, device_id, device_info)
        reading["success"] = True  # device reachable; measurement quality is represented separately
        return reading
    except Exception as e:
        logger.error(f"Failed to fetch Shelly data for {device_info['name']} (IP: {device_info['ip']}): {e}")
        return {
            "device_id": device_id,
            "device_name": device_info["name"],
            "current_a": None,
            "voltage_v": None,
            "power_w": None,
            "energy_total_wh": None,
            "relay_state": None,
            "measurement_quality": "UNAVAILABLE",
            "success": False
        }

async def fetch_weather(session: aiohttp.ClientSession, target_time: datetime):
    target_str = target_time.strftime("%Y-%m-%dT%H:00")
    
    url = (
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={LAT}&longitude={LON}"
        "&hourly=temperature_2m,relative_humidity_2m,"
        "wind_speed_10m,precipitation,weather_code,shortwave_radiation"
        f"&timezone={TIMEZONE}"
    )
    
    try:
        async with session.get(url, timeout=15) as response:
            response.raise_for_status()
            data = await response.json()
            hourly = data.get("hourly", {})
            
            times = hourly.get("time", [])
            for i, t in enumerate(times):
                if t == target_str:
                    return {
                        "temperature": hourly["temperature_2m"][i],
                        "humidity": hourly["relative_humidity_2m"][i],
                        "wind_speed": hourly["wind_speed_10m"][i],
                        "precipitation": hourly["precipitation"][i],
                        "condition": WEATHER_CODES.get(hourly["weather_code"][i], "Unknown"),
                        "ghi": hourly["shortwave_radiation"][i]
                    }
            logger.warning(f"Weather data not found for target time: {target_str}")
            return None
    except Exception as e:
        logger.error(f"Failed to fetch weather data: {e}")
        return None
