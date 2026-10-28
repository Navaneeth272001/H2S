import aiohttp
import logging
import math
from datetime import datetime, timezone
from .config import LAT, LON, TIMEZONE, WEATHER_CODES, SHELLY_EXECUTION_MODE, POLL_INTERVAL
from zoneinfo import ZoneInfo
from .shelly_rpc import ShellyRPCClient, ShellyRPCError, utc_now

logger = logging.getLogger(__name__)
_shelly_rpc = ShellyRPCClient()

# Baseline appliance power (W) for digital-twin simulation fallback when a physical
# Shelly plug is unreachable in SHELLY_EXECUTION_MODE="simulation".
_SIMULATED_DEVICE_WATTS: dict[int, float] = {
    8: 13.5,    # Living Room Lamp
    9: 85.0,    # Living Room TV
    10: 45.0,   # Kitchen Coffee Machine (average standby/brew cycle)
    11: 12.0,   # Kitchen Lamp
    12: 10.0,   # Bedroom Lamp
    13: 28.0,   # Bedroom PC Screen
    14: 65.0,   # Bedroom Computer
    15: 15.0,   # Bedroom Charger
}
_simulated_energy_counters: dict[int, float] = {}
_simulated_relay_states: dict[int, bool] = {}


def set_simulated_relay_state(device_id: int, state: bool) -> None:
    _simulated_relay_states[device_id] = bool(state)


def resume_simulated_device_counters(counters: dict[int, float]) -> None:
    for dev_id, total_wh in counters.items():
        if total_wh is not None and total_wh > 0:
            _simulated_energy_counters[int(dev_id)] = float(total_wh)


def _simulate_shelly_reading(device_id: int, device_info: dict, dt_seconds: float = POLL_INTERVAL) -> dict:
    relay_on = _simulated_relay_states.get(device_id, True)
    base_w = float(device_info.get("simulated_power_w", _SIMULATED_DEVICE_WATTS.get(device_id, 25.0)))
    now_utc = datetime.now(timezone.utc)
    # Subtle deterministic variation (+/- 5%) so readings reflect realistic AC load dynamics
    phase = (now_utc.minute * 60 + now_utc.second + device_id * 17) / 600.0
    power_w = round(base_w * (1.0 + 0.05 * math.sin(phase)), 2) if relay_on else 0.0
    voltage_v = round(230.0 - (power_w / 2000.0) * 1.5, 1)
    pf = 0.96 if power_w > 0 else 0.0
    current_a = round(power_w / max(1.0, voltage_v * max(0.8, pf)), 3) if power_w > 0 else 0.0
    prev_total = _simulated_energy_counters.get(device_id, 0.0)
    new_total = round(prev_total + power_w * max(0.0, dt_seconds) / 3600.0, 3)
    _simulated_energy_counters[device_id] = new_total
    return {
        "device_id": device_id,
        "device_name": device_info["name"],
        "ip": device_info.get("ip", ""),
        "identity": {"model": "SIMULATED-SHELLY-GEN2", "fw_id": "sim-1.0"},
        "relay_state": relay_on,
        "measurement_component": device_info.get("measurement_component", "switch:0"),
        "current_a": current_a,
        "voltage_v": voltage_v,
        "power_w": power_w,
        "power_factor": pf,
        "energy_total_wh": new_total,
        "measurement_timestamp": utc_now(),
        "measurement_quality": "ESTIMATED",
        "is_simulated": True,
        "success": True,
    }


async def fetch_shelly(session: aiohttp.ClientSession, device_id: int, device_info: dict,
                       rpc_client: ShellyRPCClient | None = None):
    """Read actual relay and configured component telemetry; fall back to digital twin in simulation mode."""
    try:
        reading = await (rpc_client or _shelly_rpc).read_device(session, device_id, device_info)
        if reading.get("power_w") is not None:
            prev_total = _simulated_energy_counters.get(device_id, 0.0)
            if reading.get("energy_total_wh") is None:
                new_total = round(prev_total + float(reading["power_w"]) * POLL_INTERVAL / 3600.0, 3)
                reading["energy_total_wh"] = new_total
                _simulated_energy_counters[device_id] = new_total
            else:
                _simulated_energy_counters[device_id] = float(reading["energy_total_wh"])
            if reading.get("power_factor") is None:
                u = float(reading.get("voltage_v") or 230.0)
                i = float(reading.get("current_a") or 0.0)
                p = float(reading["power_w"])
                apparent = u * i
                reading["power_factor"] = round(min(1.0, max(-1.0, p / apparent)), 3) if apparent > 0 else (0.98 if p > 0 else 0.0)
        reading["success"] = True  # device reachable; measurement quality is represented separately
        return reading
    except Exception as e:
        if SHELLY_EXECUTION_MODE == "simulation":
            logger.info(
                "Shelly %s (%s) unreachable (%s); using digital-twin simulated load",
                device_id, device_info.get("name"), e,
            )
            return _simulate_shelly_reading(device_id, device_info)
        logger.error(f"Failed to fetch Shelly data for {device_info['name']} (IP: {device_info['ip']}): {e}")
        return {
            "device_id": device_id,
            "device_name": device_info["name"],
            "current_a": None,
            "voltage_v": None,
            "power_w": None,
            "power_factor": None,
            "energy_total_wh": None,
            "relay_state": None,
            "measurement_quality": "UNAVAILABLE",
            "success": False
        }


def _forecast_url() -> str:
    return (
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={LAT}&longitude={LON}"
        "&hourly=temperature_2m,relative_humidity_2m,"
        "wind_speed_10m,precipitation,weather_code,shortwave_radiation"
        "&wind_speed_unit=ms"
        f"&timezone={TIMEZONE}"
    )


def _fallback_weather_row(target_time: datetime) -> dict:
    """Deterministic diurnal weather profile used when Open-Meteo is unreachable."""
    hour = target_time.hour + target_time.minute / 60.0
    # Solar irradiance bell curve between 07:00 and 19:00 peaking at 650 W/m²
    if 7.0 <= hour <= 19.0:
        ghi = round(650.0 * math.sin(math.pi * (hour - 7.0) / 12.0), 1)
    else:
        ghi = 0.0
    temp = round(16.0 + 6.0 * math.sin(math.pi * (hour - 6.0) / 12.0), 1)
    humidity = round(min(95.0, max(40.0, 68.0 - 18.0 * math.sin(math.pi * (hour - 6.0) / 12.0))), 1)
    wind_m_s = round(5.5 + 1.8 * math.cos(math.pi * hour / 12.0), 2)
    return {
        "temperature": temp,
        "humidity": humidity,
        "wind_speed": max(0.0, wind_m_s),
        "precipitation": 0.0,
        "condition": "Mainly Clear" if ghi > 0 else "Clear Sky",
        "ghi": max(0.0, ghi),
    }


def _hourly_row(hourly: dict, index: int) -> dict:
    return {
        "temperature": hourly["temperature_2m"][index],
        "humidity": hourly["relative_humidity_2m"][index],
        "wind_speed": hourly["wind_speed_10m"][index],
        "precipitation": hourly["precipitation"][index],
        "condition": WEATHER_CODES.get(hourly["weather_code"][index], "Unknown"),
        "ghi": hourly["shortwave_radiation"][index],
    }


async def _fetch_hourly(session: aiohttp.ClientSession) -> dict | None:
    try:
        async with session.get(_forecast_url(), timeout=15) as response:
            response.raise_for_status()
            data = await response.json()
            return data.get("hourly") or None
    except Exception as e:
        logger.error(f"Failed to fetch weather data: {e}")
        return None


async def fetch_weather(session: aiohttp.ClientSession, target_time: datetime):
    hourly = await _fetch_hourly(session)
    if hourly:
        target_str = target_time.strftime("%Y-%m-%dT%H:00")
        times = hourly.get("time", [])
        for i, t in enumerate(times):
            if t == target_str:
                return _hourly_row(hourly, i)
    logger.warning("Weather API unavailable for %s; using diurnal digital-twin weather profile", target_time)
    return _fallback_weather_row(target_time)


async def fetch_weather_window(session: aiohttp.ClientSession, hour_start: datetime):
    """This hour and the next one, from a single forecast request.

    The Open-Meteo hourly series is a step: irradiance and wind jump on the hour, so a production
    curve driven by it would too. ``interpolate_weather`` blends the two hours linearly, which is
    what a real panel and a real rotor do across the hour. Falls back to the diurnal weather model
    when the remote forecast cannot be reached.
    """
    from datetime import timedelta
    hourly = await _fetch_hourly(session)
    if hourly:
        times = hourly.get("time", [])
        target_str = hour_start.strftime("%Y-%m-%dT%H:00")
        for i, t in enumerate(times):
            if t == target_str:
                current = _hourly_row(hourly, i)
                following = _hourly_row(hourly, i + 1) if i + 1 < len(times) else None
                return {"current": current, "next": following}
    logger.warning("Weather window unavailable for %s; using diurnal digital-twin weather profile", hour_start)
    return {
        "current": _fallback_weather_row(hour_start),
        "next": _fallback_weather_row(hour_start + timedelta(hours=1)),
    }


def _blend(a, b, fraction: float):
    if a is None:
        return b
    if b is None:
        return a
    return a + (b - a) * fraction


def interpolate_weather(window: dict | None, fraction: float) -> dict | None:
    """Weather at ``fraction`` (0…1) of the current hour, blended towards the next hour."""
    if not window:
        return None
    current = window.get("current")
    if not current:
        return None
    following = window.get("next") or {}
    f = min(1.0, max(0.0, float(fraction)))
    blended = dict(current)
    for key in ("temperature", "humidity", "wind_speed", "ghi"):
        blended[key] = _blend(current.get(key), following.get(key), f)
    # Precipitation is already an hourly depth: splitting it would divide the same rain twice.
    blended["precipitation"] = current.get("precipitation")
    return blended
