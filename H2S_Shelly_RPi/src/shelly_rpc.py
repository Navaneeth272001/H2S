"""Shelly Gen2/3 local JSON-RPC adapter with read-back verification."""

import asyncio
import logging
import time
from datetime import datetime, timezone

import aiohttp

from .config import (
    SHELLY_COMMAND_TIMEOUT_SECONDS,
    SHELLY_MAX_RETRIES,
)

LOG = logging.getLogger("shelly_rpc")


class ShellyRPCError(RuntimeError):
    """Shelly RPC request failed or returned an invalid response."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _number(mapping: dict, *keys: str) -> float | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _energy_wh(component: dict) -> float | None:
    # Shelly Gen2 power components report `aenergy.total` in Wh. Do not infer
    # cumulative energy from instantaneous power here.
    energy = component.get("aenergy")
    if isinstance(energy, dict):
        return _number(energy, "total")
    return None


class ShellyRPCClient:
    """Reusable local RPC client. Only Gen2-or-newer RPC devices are accepted."""

    def __init__(self, timeout: float = SHELLY_COMMAND_TIMEOUT_SECONDS,
                 max_retries: int = SHELLY_MAX_RETRIES):
        if timeout <= 0:
            raise ValueError("Shelly RPC timeout must be positive")
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self._identity: dict[str, dict] = {}

    async def _request(self, session: aiohttp.ClientSession, device: dict,
                       method: str, params: dict | None = None, *, post: bool = False) -> dict:
        if device.get("protocol", "gen2_rpc") != "gen2_rpc":
            raise ShellyRPCError(f"Unsupported configured Shelly protocol: {device.get('protocol')}")
        url = f"http://{device['ip']}/rpc/{method}"
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                request = session.post(url, json=params or {}, timeout=timeout) if post else session.get(
                    url, params=params or {}, timeout=timeout
                )
                async with request as response:
                    if response.status >= 500:
                        raise ShellyRPCError(f"{method} returned HTTP {response.status}")
                    response.raise_for_status()
                    try:
                        data = await response.json()
                    except (ValueError, aiohttp.ContentTypeError) as exc:
                        raise ShellyRPCError(f"{method} returned invalid JSON: {exc}") from exc
                if not isinstance(data, dict):
                    raise ShellyRPCError(f"{method} returned non-object JSON")
                if "error" in data:
                    raise ShellyRPCError(f"{method} RPC error: {data['error']}")
                return data
            except (aiohttp.ClientError, asyncio.TimeoutError, ShellyRPCError) as exc:
                last_error = exc
                if attempt < self.max_retries:
                    await asyncio.sleep(min(0.2 * (attempt + 1), 1.0))
        raise ShellyRPCError(f"{method} failed after {self.max_retries + 1} attempt(s): {last_error}")

    async def get_identity(self, session: aiohttp.ClientSession, device: dict) -> dict:
        ip = device["ip"]
        if ip not in self._identity:
            data = await self._request(session, device, "Shelly.GetDeviceInfo")
            generation = data.get("gen")
            if generation is not None:
                try:
                    if int(generation) < 2:
                        raise ShellyRPCError(f"Shelly generation {generation} is not Gen2 RPC")
                except (TypeError, ValueError):
                    raise ShellyRPCError(f"Unrecognized Shelly generation: {generation!r}")
            identity = {
                "id": data.get("id"),
                "name": data.get("name"),
                "model": data.get("model"),
                "gen": generation,
                "fw_id": data.get("fw_id"),
                "app": data.get("app"),
            }
            self._identity[ip] = identity
            LOG.info("Shelly online %s (%s, gen=%s, fw=%s)", ip, identity["model"], generation, identity["fw_id"])
        return self._identity[ip]

    async def get_status(self, session: aiohttp.ClientSession, device: dict) -> dict:
        await self.get_identity(session, device)
        status = await self._request(session, device, "Shelly.GetStatus")
        return status

    @staticmethod
    def parse_device(device_id: int, device: dict, identity: dict,
                     status: dict, *, observed_at: str | None = None) -> dict:
        switch_id = int(device.get("switch_id", 0))
        relay_component = status.get(f"switch:{switch_id}")
        relay_state = relay_component.get("output") if isinstance(relay_component, dict) else None
        component_key = device.get("measurement_component", f"switch:{switch_id}")
        measured = status.get(component_key)
        measured = measured if isinstance(measured, dict) else {}
        power = _number(measured, "apower", "act_power", "total_act_power", "power")
        return {
            "device_id": device_id,
            "device_name": device.get("name", f"Shelly {device_id}"),
            "ip": device["ip"],
            "identity": identity,
            "relay_state": relay_state if isinstance(relay_state, bool) else None,
            "measurement_component": component_key,
            "power_w": power,
            "current_a": _number(measured, "current"),
            "voltage_v": _number(measured, "voltage"),
            "power_factor": _number(measured, "pf", "power_factor"),
            "energy_total_wh": _energy_wh(measured),
            "measurement_timestamp": observed_at or utc_now(),
            "measurement_quality": "OK" if power is not None else "UNAVAILABLE",
        }

    async def read_device(self, session: aiohttp.ClientSession, device_id: int, device: dict) -> dict:
        identity = await self.get_identity(session, device)
        status = await self._request(session, device, "Shelly.GetStatus")
        reading = self.parse_device(device_id, device, identity, status)
        if reading["relay_state"] is None:
            # Some measurement-only EMs have no relay component. They remain
            # valid meters; missing switch state is explicitly unavailable.
            if device.get("role") != "monitor_only":
                raise ShellyRPCError(f"switch:{device.get('switch_id', 0)} absent from Shelly status")
        return reading

    async def set_relay_verified(self, session: aiohttp.ClientSession, device_id: int,
                                 device: dict, target_on: bool) -> dict:
        started = time.monotonic()
        before = await self.read_device(session, device_id, device)
        if before["relay_state"] is None:
            raise ShellyRPCError("Relay state is unavailable; refusing to switch")

        sent = before["relay_state"] != target_on
        if sent:
            await self._request(
                session,
                device,
                "Switch.Set",
                {"id": int(device.get("switch_id", 0)), "on": bool(target_on)},
                post=True,
            )
        after = await self.read_device(session, device_id, device)
        return {
            "device_id": device_id,
            "name": device.get("name"),
            "ip": device["ip"],
            "switch_id": int(device.get("switch_id", 0)),
            "target_state": target_on,
            "state_before": before["relay_state"],
            "command_sent": sent,
            "command_acknowledged": True if sent else None,
            "state_after": after["relay_state"],
            "verified": after["relay_state"] is target_on,
            "measurement_before_w": before["power_w"],
            "measurement_after_w": after["power_w"],
            "measurement_component": before["measurement_component"],
            "measurement_quality": after["measurement_quality"],
            "command_timestamp": utc_now() if sent else None,
            "observed_timestamp": after["measurement_timestamp"],
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
        }
