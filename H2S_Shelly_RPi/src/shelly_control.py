"""
Shelly Gen2 Relay Control & Decision Execution Module (Chemin B)
================================================================
Controls Shelly EM / Plug S relays via local JSON-RPC API:
  POST/GET /rpc/Switch.Set?id=0&on=true|false

Implements decision translation:
- DISCHARGE: Switch ON priority devices (drawn from battery).
- CHARGE:    Switch OFF non-critical devices (reduces consumption, simulates charge).
- STANDBY:   Preserve relay states.
"""

import aiohttp
import asyncio
import logging
from typing import Dict, List, Optional
from .config import SHELLY_DEVICES, SHELLY_PRIORITY_IDS, SHELLY_NON_CRITICAL_IDS

logger = logging.getLogger("shelly_control")


async def set_relay(session: aiohttp.ClientSession, ip: str, on: bool, timeout: float = 3.0) -> bool:
    """Set relay state via Shelly Gen2 JSON-RPC Switch.Set."""
    url = f"http://{ip}/rpc/Switch.Set"
    payload = {"id": 0, "on": on}
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status == 200:
                data = await resp.json()
                logger.debug(f"Relay on {ip} set to {on}: response={data}")
                return True
            else:
                logger.warning(f"Failed to set relay on {ip} (status={resp.status})")
                return False
    except Exception as e:
        logger.error(f"Error setting relay on {ip} to {on}: {e}")
        return False


async def get_relay_state(session: aiohttp.ClientSession, ip: str, timeout: float = 3.0) -> Optional[bool]:
    """Read relay output status from Shelly Gen2."""
    url = f"http://{ip}/rpc/Switch.GetStatus?id=0"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("output")
    except Exception as e:
        logger.debug(f"Could not read relay status from {ip}: {e}")
    return None


async def execute_battery_decision(
    session: aiohttp.ClientSession,
    action: str,
    devices: Optional[Dict[int, dict]] = None
) -> dict:
    """
    Translates AI decision into physical Shelly relay actions (Chemin B).
    
    Returns execution details dictionary.
    """
    dev_map = devices or SHELLY_DEVICES
    action_norm = action.upper().strip()
    result = {
        "action": action_norm,
        "switched_on": [],
        "switched_off": [],
        "errors": [],
        "status": "COMPLETED"
    }

    if action_norm == "DISCHARGE":
        # Turn ON priority appliances to draw energy from battery
        tasks = []
        for dev_id in SHELLY_PRIORITY_IDS:
            if dev_id in dev_map:
                dev = dev_map[dev_id]
                tasks.append((dev_id, dev["name"], dev["ip"], True))

        for dev_id, name, ip, target_on in tasks:
            ok = await set_relay(session, ip, target_on)
            if ok:
                result["switched_on"].append({"device_id": dev_id, "name": name, "ip": ip})
            else:
                result["errors"].append({"device_id": dev_id, "name": name, "ip": ip, "target": target_on})

        logger.info(f"[Chemin B] DISCHARGE executed: {len(result['switched_on'])} priority devices switched ON.")

    elif action_norm == "CHARGE":
        # Turn OFF non-critical devices to drop load and simulate charging by surplus
        tasks = []
        for dev_id in SHELLY_NON_CRITICAL_IDS:
            if dev_id in dev_map:
                dev = dev_map[dev_id]
                tasks.append((dev_id, dev["name"], dev["ip"], False))

        for dev_id, name, ip, target_on in tasks:
            ok = await set_relay(session, ip, target_on)
            if ok:
                result["switched_off"].append({"device_id": dev_id, "name": name, "ip": ip})
            else:
                result["errors"].append({"device_id": dev_id, "name": name, "ip": ip, "target": target_on})

        logger.info(f"[Chemin B] CHARGE executed: {len(result['switched_off'])} non-critical devices switched OFF.")

    elif action_norm == "STANDBY":
        logger.info("[Chemin B] STANDBY executed: all device states maintained.")
        result["status"] = "MAINTAINED"

    else:
        logger.warning(f"[Chemin B] Unrecognized decision action: {action_norm}")
        result["status"] = "IGNORED"

    return result
