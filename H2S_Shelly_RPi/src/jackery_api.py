"""Read Jackery telemetry through the third-party ``socketry`` client.

This uses Jackery's cloud HTTP property endpoint. It is read-only and does not
send control commands to the power station. ``socketry`` reverse engineers an
unofficial API and supports only the models listed in its own documentation.
"""

import logging
import re
from typing import Any

from .config import JACKERY_DEVICE_SN, JACKERY_EMAIL, JACKERY_PASSWORD

logger = logging.getLogger("jackery_api")


def _number(value: Any, property_name: str, setting: Any = None) -> float:
    """Convert a socketry property into the user-facing value and unit."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"Jackery property {property_name!r} is unavailable")
    # Socketry returns protocol raw values from get_property(). Its formatter
    # applies property-specific scaling (for example tenths of a degree).
    formatted = setting.format_value(value) if setting is not None else value
    try:
        result = float(formatted)
    except (TypeError, ValueError):
        match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", str(formatted))
        if match is None:
            raise ValueError(f"Jackery property {property_name!r} is not numeric: {formatted!r}")
        result = float(match.group(0))
    if not (-1e9 < result < 1e9):
        raise ValueError(f"Jackery property {property_name!r} is outside a valid range")
    return result


class JackeryClient:
    """Persistent async reader for a selected Jackery station."""

    def __init__(self, email: str = JACKERY_EMAIL, password: str = JACKERY_PASSWORD,
                 device_sn: str = JACKERY_DEVICE_SN):
        self.email = email
        self.password = password
        self.device_sn = device_sn
        self._client = None
        self._device = None

    async def _connect(self):
        if not self.email or not self.password:
            raise RuntimeError("Set JACKERY_EMAIL and JACKERY_PASSWORD in the collector .env")
        if not self.device_sn:
            raise RuntimeError("Set JACKERY_DEVICE_SN to the station serial number")

        try:
            from socketry import Client
        except ImportError as exc:
            raise RuntimeError("socketry is not installed; rebuild/reinstall collector requirements") from exc

        client = await Client.login(self.email, self.password)
        devices = await client.fetch_devices()
        matched = next((d for d in devices if str(d.get("devSn", "")) == self.device_sn), None)
        if matched is None:
            available = ", ".join(str(d.get("devSn", "?")) for d in devices) or "none"
            raise RuntimeError(
                f"Jackery serial {self.device_sn!r} was not found in this account (devices: {available})"
            )
        self._client = client
        self._device = client.device(self.device_sn)
        logger.info("Connected to Jackery device %s (%s)", self.device_sn, matched.get("devName", "unknown model"))

    async def read_battery(self) -> dict[str, float]:
        """Poll real station properties; fail the sample if required data is absent."""
        if self._device is None:
            await self._connect()

        # Each property lookup uses socketry's documented raw property API.
        # Keep these values strict: a failed/missing reading must not be sent as
        # a fabricated simulated value under a physical telemetry stream.
        properties = {}
        for name in ("battery", "input-power", "output-power", "ac-voltage", "battery-temp"):
            try:
                setting, value = await self._device.get_property(name)
            except (KeyError, ValueError) as exc:
                raise RuntimeError(f"Jackery did not provide required property {name!r}: {exc}") from exc
            properties[name] = _number(value, name, setting)

        soc = properties["battery"]
        voltage = properties["ac-voltage"]
        if not 0 <= soc <= 100:
            raise ValueError(f"Jackery SoC is outside 0–100%: {soc}")
        if voltage <= 0:
            raise ValueError(f"Jackery inverter voltage is unavailable: {voltage}")

        charge_w = max(0.0, properties["input-power"])
        discharge_w = max(0.0, properties["output-power"])
        net_power_w = abs(discharge_w - charge_w)
        return {
            "soc_pct": soc,
            "power_charge_w": charge_w,
            "power_discharge_w": discharge_w,
            "voltage_v": voltage,
            # Socketry exposes station input/output power, not battery current;
            # current is derived from the measured net power and AC voltage.
            "current_a": net_power_w / voltage,
            "temperature_c": properties["battery-temp"],
        }
