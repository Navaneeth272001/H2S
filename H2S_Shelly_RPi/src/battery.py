"""
Battery Manager & Simulator for Physical Battery Integration (Chemin B)
=======================================================================
Handles battery state monitoring, physical readings (Jackery/BMS if available),
and high-fidelity battery state simulation when physical comms are not wired.

Physical Hypothesis (from H2S specification):
- The physical battery (Jackery) is connected to Enedis (grid) for base charge safety.
- The battery powers the appliances via Shelly EM relay plugs.
- The digital twin computes decisions (CHARGE, DISCHARGE, STANDBY).
- Simulated SoC represents renewable surplus availability.
- During DISCHARGE: Priority devices are powered from the battery (discharges).
- During CHARGE: Non-critical devices are switched OFF, simulating charging by surplus.
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .config import (
    BATTERY_ENABLED,
    BATTERY_STOCK_ID,
    BATTERY_NAME,
    BATTERY_NOMINAL_CAPACITY_WH,
    BATTERY_VOLTAGE_NOMINAL,
    BATTERY_MAX_POWER_W,
    BATTERY_INITIAL_SOC,
    BATTERY_SIMULATED_CHARGE_W,
)

logger = logging.getLogger("battery_manager")


@dataclass
class BatteryReading:
    timestamp: datetime
    stock_id: int
    name: str
    soc_pct: float
    power_charge_w: float
    power_discharge_w: float
    voltage_v: float
    current_a: float
    temperature_c: float
    is_simulated: bool
    action: str

    def to_dict(self) -> dict:
        ts = self.timestamp.isoformat()
        if not ts.endswith("Z") and "+" not in ts:
            ts += "Z"
        return {
            "timestamp": ts,
            "stock_id": self.stock_id,
            "name": self.name,
            "soc_pct": round(self.soc_pct, 2),
            "power_charge_w": round(self.power_charge_w, 1),
            "power_discharge_w": round(self.power_discharge_w, 1),
            "voltage_v": round(self.voltage_v, 1),
            "current_a": round(self.current_a, 3),
            "temperature_c": round(self.temperature_c, 1),
            "is_simulated": self.is_simulated,
            "action": self.action,
        }

    def to_h2s_payload(self) -> dict:
        """Format expected by h2s_agent's parsers.h2s_battery and local MQTT."""
        ts = self.timestamp.isoformat()
        if not ts.endswith("Z") and "+" not in ts:
            ts += "Z"
        return {
            "timestamp": ts,
            "soc_pct": round(self.soc_pct, 2),
            "power_charge_w": round(self.power_charge_w, 1),
            "power_discharge_w": round(self.power_discharge_w, 1),
            "voltage_v": round(self.voltage_v, 1),
            "current_a": round(self.current_a, 3),
            "temperature_c": round(self.temperature_c, 1),
        }


class BatteryManager:
    def __init__(
        self,
        stock_id: int = BATTERY_STOCK_ID,
        name: str = BATTERY_NAME,
        capacity_wh: float = BATTERY_NOMINAL_CAPACITY_WH,
        voltage_nominal: float = BATTERY_VOLTAGE_NOMINAL,
        max_power_w: float = BATTERY_MAX_POWER_W,
        initial_soc: float = BATTERY_INITIAL_SOC,
        charge_power_w: float = BATTERY_SIMULATED_CHARGE_W,
    ):
        self.stock_id = stock_id
        self.name = name
        self.capacity_wh = capacity_wh
        self.voltage_nominal = voltage_nominal
        self.max_power_w = max_power_w
        self.soc_pct = max(5.0, min(100.0, initial_soc))
        self.charge_power_w = charge_power_w
        self.current_action = "STANDBY"
        self._physical_driver = None  # Hook for Jackery / BMS driver (Chemin A)

    def set_action(self, action: str):
        """Update current operational action from AI decision: CHARGE, DISCHARGE, STANDBY."""
        valid_actions = ("CHARGE", "DISCHARGE", "STANDBY")
        norm = action.upper().strip()
        if norm in valid_actions:
            self.current_action = norm
            logger.info(f"Battery {self.name} action set to {self.current_action}")
        else:
            logger.warning(f"Unknown battery action {action}, keeping {self.current_action}")

    def update(
        self,
        active_load_w: float = 0.0,
        action: Optional[str] = None,
        dt_seconds: float = 60.0,
        timestamp: Optional[datetime] = None,
    ) -> BatteryReading:
        """
        Step battery dynamics based on load and decision action.
        If physical hardware is connected, attempts to read physical state;
        otherwise runs simulation loop.
        """
        if action:
            self.set_action(action)

        ts = timestamp or datetime.now(timezone.utc)

        # 1. Check physical hardware driver (Chemin A) if configured
        if self._physical_driver and hasattr(self._physical_driver, "read_battery"):
            try:
                phys = self._physical_driver.read_battery()
                if phys:
                    return BatteryReading(
                        timestamp=ts,
                        stock_id=self.stock_id,
                        name=self.name,
                        soc_pct=phys.get("soc_pct", self.soc_pct),
                        power_charge_w=phys.get("power_charge_w", 0.0),
                        power_discharge_w=phys.get("power_discharge_w", active_load_w),
                        voltage_v=phys.get("voltage_v", self.voltage_nominal),
                        current_a=phys.get("current_a", 0.0),
                        temperature_c=phys.get("temperature_c", 22.0),
                        is_simulated=False,
                        action=self.current_action,
                    )
            except Exception as e:
                logger.warning(f"Physical battery read failed: {e}. Falling back to simulation.")

        # 2. Physics simulation (Chemin B)
        p_charge = 0.0
        p_discharge = 0.0

        if self.current_action == "DISCHARGE":
            # Devices powered by the battery draw real load
            p_discharge = max(0.0, min(float(active_load_w), self.max_power_w))
            p_charge = 0.0
            # Energy delta in Wh
            d_energy_wh = p_discharge * (dt_seconds / 3600.0)
            d_soc = (d_energy_wh / self.capacity_wh) * 100.0
            self.soc_pct = max(10.0, self.soc_pct - d_soc)  # 10% safety cutoff

        elif self.current_action == "CHARGE":
            # Non-critical devices switched off; simulated surplus absorbs power
            p_charge = self.charge_power_w
            p_discharge = 0.0
            d_energy_wh = p_charge * (dt_seconds / 3600.0)
            d_soc = (d_energy_wh / self.capacity_wh) * 100.0
            self.soc_pct = min(100.0, self.soc_pct + d_soc)

        else:  # STANDBY
            p_charge = 0.0
            p_discharge = 0.0
            # Minor standby self-discharge (0.01% per hour)
            self.soc_pct = max(5.0, self.soc_pct - (0.01 * dt_seconds / 3600.0))

        # Realistic electrical modeling
        # Small voltage droop proportional to discharge power
        droop = (p_discharge / max(1.0, self.max_power_w)) * 3.5
        boost = (p_charge / max(1.0, self.max_power_w)) * 2.0
        voltage = self.voltage_nominal - droop + boost

        active_power = max(p_charge, p_discharge)
        current = active_power / max(1.0, voltage)

        # Thermal dissipation model (21°C ambient + rise proportional to active power)
        temperature = 21.5 + (active_power / max(1.0, self.max_power_w)) * 7.5

        return BatteryReading(
            timestamp=ts,
            stock_id=self.stock_id,
            name=self.name,
            soc_pct=self.soc_pct,
            power_charge_w=p_charge,
            power_discharge_w=p_discharge,
            voltage_v=voltage,
            current_a=current,
            temperature_c=temperature,
            is_simulated=True,
            action=self.current_action,
        )


# Global singleton instance
_battery_manager: Optional[BatteryManager] = None


def get_battery_manager() -> BatteryManager:
    global _battery_manager
    if _battery_manager is None:
        _battery_manager = BatteryManager()
    return _battery_manager
