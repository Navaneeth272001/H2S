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
    BATTERY_SOURCE,
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
    simulated_production_w: float | None = None
    simulated_surplus_w: float | None = None

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
            "simulated_production_w": self.simulated_production_w,
            "simulated_surplus_w": self.simulated_surplus_w,
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
            "is_simulated": self.is_simulated,
        }


def estimate_simulated_production_w(ghi_w_m2: float | None, peak_power_w: float) -> float:
    """Estimate virtual renewable output from forecast GHI (no physical generator assumed).

    The proposal does not specify a generation model. This uses a configurable peak
    output scaled linearly by irradiance relative to 1,000 W/m², clamped to [0, peak].
    Missing weather safely yields zero simulated renewable production.
    """
    if ghi_w_m2 is None or peak_power_w <= 0:
        return 0.0
    try:
        ghi = float(ghi_w_m2)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(ghi):
        return 0.0
    return round(peak_power_w * min(1000.0, max(0.0, ghi)) / 1000.0, 2)


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
        self._explicit_action: Optional[str] = None
        self._physical_driver = None
        if BATTERY_SOURCE == "socketry":
            from .jackery_api import JackeryClient
            self._physical_driver = JackeryClient()
        elif BATTERY_SOURCE != "simulation":
            raise ValueError(f"Unsupported BATTERY_SOURCE={BATTERY_SOURCE!r}; use simulation or socketry")

    def set_action(self, action: str):
        """Update current operational action from AI decision: CHARGE, DISCHARGE, STANDBY."""
        valid_actions = ("CHARGE", "DISCHARGE", "STANDBY")
        norm = action.upper().strip()
        if norm in valid_actions:
            self.current_action = norm
            self._explicit_action = norm
            logger.info(f"Battery {self.name} action set to {self.current_action}")
        else:
            logger.warning(f"Unknown battery action {action}, keeping {self.current_action}")

    def update(
        self,
        active_load_w: float = 0.0,
        action: Optional[str] = None,
        dt_seconds: float = 60.0,
        timestamp: Optional[datetime] = None,
        simulated_production_w: float | None = None,
        site_load_w: float | None = None,
        site_load_complete: bool = True,
    ) -> BatteryReading:
        """
        Step battery dynamics based on load and decision action.
        Run the simulation model. The async collector uses update_async when
        the socketry physical source is enabled.
        """
        if self._physical_driver is not None:
            raise RuntimeError("BATTERY_SOURCE=socketry requires awaiting update_async(); refusing simulated data")
        if action:
            self.set_action(action)

        ts = timestamp or datetime.now(timezone.utc)

        # Phase 1 digital twin: simulated generation and measured Shelly load
        # form an estimated site balance.
        measured_site_load_w = max(0.0, float(active_load_w if site_load_w is None else site_load_w))
        priority_load_w = max(0.0, float(active_load_w))

        # When running autonomously in the collector loop (no explicit STANDBY forced),
        # derive the active regime from the digital twin's net power balance so that
        # charge/discharge power, current, voltage, and temperature are always active.
        if action is None and self._explicit_action is None and BATTERY_SOURCE == "simulation":
            prod_cand = max(0.0, float(0.0 if simulated_production_w is None else simulated_production_w))
            if prod_cand > measured_site_load_w and self.soc_pct < 100.0:
                self.current_action = "CHARGE"
            elif (measured_site_load_w > prod_cand or priority_load_w > 0) and self.soc_pct > 10.0:
                self.current_action = "DISCHARGE"
            elif self.soc_pct < 95.0:
                self.current_action = "CHARGE"
            else:
                self.current_action = "DISCHARGE"

        default_production = self.charge_power_w if self.current_action == "CHARGE" else 0.0
        production_w = max(0.0, float(
            default_production if simulated_production_w is None else simulated_production_w
        ))
        simulated_surplus_w = max(0.0, production_w - measured_site_load_w)
        p_charge = 0.0
        p_discharge = 0.0

        if self.current_action == "DISCHARGE":
            if BATTERY_SOURCE == "simulation":
                # Virtual generation serves load first; the battery model supplies
                # the remaining priority/site load when DISCHARGE is active.
                load_to_serve = priority_load_w if priority_load_w > 0 else measured_site_load_w
                if simulated_production_w is None:
                    net_discharge = load_to_serve
                else:
                    net_discharge = max(0.0, load_to_serve - production_w)
                    if net_discharge <= 0.0:
                        net_discharge = load_to_serve if load_to_serve > 0 else 150.0
                p_discharge = max(0.0, min(net_discharge, self.max_power_w))
                d_energy_wh = p_discharge * (dt_seconds / 3600.0)
                d_soc = (d_energy_wh / self.capacity_wh) * 100.0
                self.soc_pct = max(10.0, self.soc_pct - d_soc)  # 10% safety cutoff
            p_charge = 0.0

        elif self.current_action == "CHARGE":
            if BATTERY_SOURCE == "simulation":
                charge_input = simulated_surplus_w if simulated_surplus_w > 0 else self.charge_power_w
                p_charge = min(charge_input, self.max_power_w)
                d_energy_wh = p_charge * (dt_seconds / 3600.0)
                d_soc = (d_energy_wh / self.capacity_wh) * 100.0
                self.soc_pct = min(100.0, self.soc_pct + d_soc)
            p_discharge = 0.0

        else:  # STANDBY
            p_charge = 0.0
            p_discharge = 0.0
            # The proposal defines standby as maintaining the current SoC.

        # Realistic electrical modeling based on Thévenin 2RC DC pack (Jackery 36V nominal)
        # OCV ranges from ~33.0V (0% SoC) to ~38.6V (100% SoC), matching cloud model (38.54V at ~99.8%)
        soc_ratio = max(0.0, min(1.0, self.soc_pct / 100.0))
        ocv = (self.voltage_nominal - 3.0) + (soc_ratio * 5.6)
        droop = (p_discharge / max(1.0, self.max_power_w)) * 0.8
        boost = (p_charge / max(1.0, self.max_power_w)) * 0.8
        voltage = ocv - droop + boost

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
            simulated_production_w=production_w,
            simulated_surplus_w=simulated_surplus_w,
        )

    async def update_async(
        self,
        active_load_w: float = 0.0,
        action: Optional[str] = None,
        dt_seconds: float = 60.0,
        timestamp: Optional[datetime] = None,
        simulated_production_w: float | None = None,
        site_load_w: float | None = None,
        site_load_complete: bool = True,
    ) -> Optional[BatteryReading]:
        """Read configured physical telemetry or run simulation.

        A failed physical read returns ``None``. It never falls back to
        simulated data while ``BATTERY_SOURCE=socketry`` is configured.
        """
        if action:
            self.set_action(action)
        ts = timestamp or datetime.now(timezone.utc)

        if self._physical_driver is None:
            return self.update(
                active_load_w,
                dt_seconds=dt_seconds,
                timestamp=ts,
                simulated_production_w=simulated_production_w,
                site_load_w=site_load_w,
                site_load_complete=site_load_complete,
            )

        try:
            phys = await self._physical_driver.read_battery()
        except Exception as exc:
            logger.error("Physical Jackery read failed; skipping battery sample: %s", exc)
            return None

        return BatteryReading(
            timestamp=ts,
            stock_id=self.stock_id,
            name=self.name,
            soc_pct=phys["soc_pct"],
            power_charge_w=phys["power_charge_w"],
            power_discharge_w=phys["power_discharge_w"],
            voltage_v=phys["voltage_v"],
            current_a=phys["current_a"],
            temperature_c=phys["temperature_c"],
            is_simulated=False,
            action=self.current_action,
        )


# Global singleton instance
_battery_manager: Optional[BatteryManager] = None


def get_battery_manager() -> BatteryManager:
    global _battery_manager
    if _battery_manager is None:
        _battery_manager = BatteryManager()
    return _battery_manager
