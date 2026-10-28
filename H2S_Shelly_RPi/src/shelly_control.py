"""Safety-gated indirect battery control through Shelly relay channels."""

import logging
from typing import Dict, Optional
from datetime import datetime, timezone

import aiohttp

from .config import (
    SHELLY_DEVICES,
    SHELLY_EXECUTION_MODE,
    SHELLY_PHYSICAL_COMMANDS_ENABLED,
    SHELLY_EFFECT_THRESHOLD_W,
)
from .shelly_rpc import ShellyRPCClient, ShellyRPCError

LOG = logging.getLogger("shelly_control")
_RPC = ShellyRPCClient()


async def set_relay(session: aiohttp.ClientSession, ip: str, on: bool,
                    timeout: float = 3.0, switch_id: int = 0) -> bool:
    """Compatibility helper that switches a relay and verifies its read-back."""
    if SHELLY_EXECUTION_MODE != "hardware" or not SHELLY_PHYSICAL_COMMANDS_ENABLED:
        LOG.warning("Refusing low-level relay command outside explicitly enabled hardware mode")
        return False
    client = ShellyRPCClient(timeout=timeout)
    device = {"ip": ip, "name": ip, "role": "priority", "switch_id": switch_id,
              "measurement_component": f"switch:{switch_id}", "protocol": "gen2_rpc"}
    try:
        result = await client.set_relay_verified(session, 0, device, on)
        return result["verified"]
    except ShellyRPCError:
        LOG.exception("Shelly relay command failed for %s", ip)
        return False


async def get_relay_state(session: aiohttp.ClientSession, ip: str,
                          timeout: float = 3.0, switch_id: int = 0) -> Optional[bool]:
    """Read a relay state from Shelly Gen2/3 ``Shelly.GetStatus``."""
    client = ShellyRPCClient(timeout=timeout)
    device = {"ip": ip, "name": ip, "role": "priority", "switch_id": switch_id,
              "measurement_component": f"switch:{switch_id}", "protocol": "gen2_rpc"}
    try:
        reading = await client.read_device(session, 0, device)
        return reading["relay_state"]
    except ShellyRPCError:
        LOG.debug("Could not read Shelly relay state from %s", ip, exc_info=True)
        return None


def _targets(action: str, devices: dict[int, dict]) -> list[tuple[int, dict, bool]]:
    if action == "DISCHARGE":
        role, state = "priority", True
    elif action == "CHARGE":
        role, state = "non_critical", False
    else:
        return []
    return [(device_id, device, state) for device_id, device in devices.items()
            if device.get("role", "priority" if device.get("priority") else "non_critical") == role]


async def execute_battery_decision(
    session: aiohttp.ClientSession,
    action: str,
    devices: Optional[Dict[int, dict]] = None,
    *,
    rpc_client: ShellyRPCClient | None = None,
    execution_mode: str | None = None,
    physical_commands_enabled: bool | None = None,
    correlation_id: str | None = None,
) -> dict:
    """Apply a CHARGE/DISCHARGE/STANDBY order through a verified safe mode.

    `simulation` produces a proposed action without network relay commands.
    `dry_run` reads actual states and logs proposals but does not switch.
    `hardware` actuates only with the separate physical-command enable flag.
    """
    action_norm = str(action).strip().upper()
    mode = (execution_mode or SHELLY_EXECUTION_MODE).lower()
    enabled = SHELLY_PHYSICAL_COMMANDS_ENABLED if physical_commands_enabled is None else physical_commands_enabled
    device_map = SHELLY_DEVICES if devices is None else devices
    client = rpc_client or _RPC
    result = {
        "schema_version": 1,
        "correlation_id": correlation_id,
        "action": action_norm,
        "strategy": "shelly_indirect",
        "execution_mode": mode,
        "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "PENDING",
        "commands": [],
        "switched_on": [],
        "switched_off": [],
        "errors": [],
        "physical_battery_charge": "NOT_COMMANDED_OR_MEASURED_BY_SHELLY",
        "consumption_effect": {"status": "UNAVAILABLE", "power_before_w": None,
                               "power_after_w": None, "delta_w": None},
    }

    if action_norm not in ("CHARGE", "DISCHARGE", "STANDBY"):
        result.update(status="REJECTED", errors=[{"error": f"unsupported action {action_norm!r}"}])
        return result
    if mode not in ("simulation", "dry_run", "hardware"):
        result.update(status="REJECTED", errors=[{"error": f"invalid execution mode {mode!r}"}])
        return result
    if action_norm == "STANDBY":
        result["status"] = "MAINTAINED"
        result["consumption_effect"]["status"] = "NOT_APPLICABLE"
        LOG.info("Shelly STANDBY: preserving all relay states; no command sent")
        return result

    targets = _targets(action_norm, device_map)
    if not targets:
        result.update(status="NO_TARGETS", errors=[{"error": f"no configured Shelly targets for {action_norm}"}])
        return result
    if mode == "hardware" and not enabled:
        result.update(status="REJECTED", errors=[{"error": "hardware mode requires SHELLY_PHYSICAL_COMMANDS_ENABLED=true"}])
        return result

    if action_norm == "CHARGE":
        result["physical_battery_charge"] = "SIMULATED_LOAD_REDUCTION_ONLY"

    before_samples, after_samples = {}, {}
    for device_id, device, target_on in targets:
        planned = {"device_id": device_id, "name": device.get("name"), "ip": device.get("ip"),
                   "switch_id": device.get("switch_id", 0), "target_state": target_on}
        if mode == "simulation":
            from .collector import set_simulated_relay_state
            set_simulated_relay_state(device_id, target_on)
            planned.update({"command_sent": False, "verified": None, "state_before": None,
                            "state_after": target_on, "measurement_quality": "UNAVAILABLE"})
            result["commands"].append(planned)
            continue

        try:
            before = await client.read_device(session, device_id, device)
            if before["power_w"] is not None:
                before_samples[device_id] = before["power_w"]
            if mode == "dry_run":
                planned.update({"command_sent": False, "verified": None,
                                "state_before": before["relay_state"], "state_after": before["relay_state"],
                                "measurement_before_w": before["power_w"],
                                "measurement_quality": before["measurement_quality"]})
                result["commands"].append(planned)
                continue

            executed = await client.set_relay_verified(session, device_id, device, target_on)
            result["commands"].append(executed)
            if executed["measurement_after_w"] is not None:
                after_samples[device_id] = executed["measurement_after_w"]
            if not executed["verified"]:
                result["errors"].append({"device_id": device_id, "error": "relay state did not match target after read-back"})
            elif executed["command_sent"] and target_on:
                result["switched_on"].append({"device_id": device_id, "name": device.get("name"), "ip": device.get("ip")})
            elif executed["command_sent"]:
                result["switched_off"].append({"device_id": device_id, "name": device.get("name"), "ip": device.get("ip")})
        except Exception as exc:
            LOG.exception("Shelly command failed for configured device %s", device_id)
            result["errors"].append({"device_id": device_id, "name": device.get("name"),
                                     "error": str(exc), "target_state": target_on})

    expected_devices = len(targets)
    verified_count = sum(1 for c in result["commands"] if c.get("verified") is True)
    if mode == "simulation":
        result["status"] = "SIMULATED"
    elif mode == "dry_run":
        result["status"] = "DRY_RUN"
    elif result["errors"] and verified_count:
        result["status"] = "PARTIAL"
    elif result["errors"]:
        result["status"] = "FAILED"
    elif verified_count == expected_devices:
        result["status"] = "COMPLETED"
    else:
        result["status"] = "FAILED"

    if mode == "hardware" and after_samples:
        effect = result["consumption_effect"]
        common_samples = set(before_samples) & set(after_samples)
        effect["devices_measured"] = len(common_samples)
        effect["devices_targeted"] = len(targets)
        if common_samples == {device_id for device_id, _device, _target in targets}:
            effect["power_before_w"] = round(sum(before_samples[i] for i in common_samples), 2)
            effect["power_after_w"] = round(sum(after_samples[i] for i in common_samples), 2)
            delta = effect["power_after_w"] - effect["power_before_w"]
            effect["delta_w"] = round(delta, 2)
            expected_direction = -1 if action_norm == "CHARGE" else 1
            if abs(delta) < SHELLY_EFFECT_THRESHOLD_W:
                effect["status"] = "NO_EXPECTED_CHANGE_OBSERVED"
            elif delta * expected_direction > 0:
                effect["status"] = "EXPECTED_LOAD_EFFECT_OBSERVED"
            else:
                effect["status"] = "OPPOSITE_LOAD_EFFECT_OBSERVED"
        else:
            effect["status"] = "INCOMPLETE_MEASUREMENTS"
    elif mode in ("simulation", "dry_run"):
        result["consumption_effect"]["status"] = "NOT_MEASURED"

    LOG.info("Shelly decision=%s mode=%s status=%s verified=%s/%s effect=%s",
             action_norm, mode, result["status"], verified_count, expected_devices,
             result["consumption_effect"]["status"])
    return result
