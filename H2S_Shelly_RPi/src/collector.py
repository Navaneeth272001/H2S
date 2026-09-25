import aiohttp
import logging
from datetime import datetime
from .config import LAT, LON, TIMEZONE, WEATHER_CODES
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

async def fetch_shelly(session: aiohttp.ClientSession, device_id: int, device_info: dict):
    url = f"http://{device_info['ip']}/rpc/Switch.GetStatus?id=0"
    try:
        async with session.get(url, timeout=5) as response:
            response.raise_for_status()
            data = await response.json()
            
            # current, voltage, power
            current = float(data.get("current", 0.0))
            voltage = float(data.get("voltage", 0.0))
            power = float(data.get("apower", 0.0))
            
            return {
                "device_id": device_id,
                "device_name": device_info["name"],
                "current_a": current,
                "voltage_v": voltage,
                "power_w": power,
                "success": True
            }
    except Exception as e:
        logger.error(f"Failed to fetch Shelly data for {device_info['name']} (IP: {device_info['ip']}): {e}")
        return {
            "device_id": device_id,
            "device_name": device_info["name"],
            "current_a": None,
            "voltage_v": None,
            "power_w": None,
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
