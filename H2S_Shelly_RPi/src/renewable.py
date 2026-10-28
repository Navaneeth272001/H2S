"""Renewable production digital twin — solar PV and wind.

The backend accepts a production reading exactly like any other device reading:

    { "source": "device", "device_id": <id>, "values": { "power_w": ... } }

There is no "production" field in the payload. What makes a reading count as
production (solar or wind) rather than consumption is the **role declared on the
site** for that device. This module therefore simulates one source per declared
device id, and publishes each under its own ``device_id``.

Why the numbers are what they are
---------------------------------
The two formulas are the ones of the client specification (§4.1.4), the same
ones ``agent/h2s_agent/energy_calculator.py`` implements:

    P_PV   = G × S × η_PV
    P_wind = ½ × ρ × A × V³ × η_wind          A = π × (D / 2)²

They are reproduced here, unchanged, because the digital twin must agree with
the theoretical production the agent computes from the same weather: two
different formulas for "what a panel of this size makes at this irradiance"
would put two different numbers on the same dashboard.

A wind formula in V³ knows nothing about a real machine: it does not start
below its cut-in speed, it plateaus at its rated power, and it furls above its
cut-out speed. Applied bare, it would promise kilowatts while the turbine sits
in feather. Those bounds are applied separately (``bound_to_machine``) and only
when the machine declares them — exactly as the agent does it.

What this is, and is not
------------------------
This is a **simulation**: no physical generator is read. The produced numbers
are plausible physics driven by a real weather forecast, never a measurement,
and the ``energy_total_wh`` counter they carry is simulated too. The moment real
inverters or meters are wired, their device ids take over and the simulation for
that source is switched off in configuration.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import (
    AIR_DENSITY_KG_M3,
    PRODUCTION_AC_VOLTAGE_V,
    PRODUCTION_MIN_OUTPUT_W,
    PRODUCTION_POWER_FACTOR,
    PRODUCTION_SOURCES,
)

logger = logging.getLogger("renewable")

#: Local timezone-free timestamps are published as UTC with a trailing « Z ».
SOURCE_TYPES = ("SOLAR", "WIND")


def solar_power_w(irradiance_w_m2: float | None, surface_m2: float | None,
                  efficiency: float = 0.22) -> float:
    """Photovoltaic power: ``P_PV = G × S × η_PV`` (W).

    Specification example: G = 600 W/m², S = 2.0 m², η = 0.22 → 264 W.
    Missing or negative inputs produce 0 W: a panel whose area is unknown does
    not produce "a little", it produces nothing computable.
    """
    if irradiance_w_m2 is None or surface_m2 is None or efficiency is None:
        return 0.0
    if irradiance_w_m2 <= 0 or surface_m2 <= 0 or efficiency <= 0:
        return 0.0
    return irradiance_w_m2 * surface_m2 * efficiency


def swept_area_m2(rotor_diameter_m: float) -> float:
    """Rotor swept area: ``A = π × (D / 2)²``. Specification: D = 1.8 m → 2.545 m²."""
    if rotor_diameter_m is None or rotor_diameter_m <= 0:
        return 0.0
    return math.pi * (rotor_diameter_m / 2.0) ** 2


def wind_power_w(wind_speed_m_s: float | None, rotor_diameter_m: float | None,
                 efficiency: float = 0.35, air_density_kg_m3: float = AIR_DENSITY_KG_M3) -> float:
    """Wind power: ``P_wind = ½ × ρ × A × V³ × η`` (W).

    Specification example: ρ = 1.225, D = 1.8 m, V = 5 m/s, η = 0.35 → ≈ 68.2 W.
    """
    if wind_speed_m_s is None or rotor_diameter_m is None or efficiency is None:
        return 0.0
    if wind_speed_m_s <= 0 or rotor_diameter_m <= 0 or efficiency <= 0:
        return 0.0
    area = swept_area_m2(rotor_diameter_m)
    return 0.5 * air_density_kg_m3 * area * (wind_speed_m_s ** 3) * efficiency


def bound_to_machine(power_w: float, wind_speed_m_s: float,
                     cut_in_m_s: float | None,
                     cut_out_m_s: float | None,
                     rated_power_w: float | None) -> float:
    """Apply a real turbine's limits to the theoretical V³ power.

    Every limit is optional: absent, it does not apply, and the result is
    exactly the specification's figure.
    """
    if power_w <= 0:
        return 0.0
    if cut_in_m_s is not None and wind_speed_m_s < cut_in_m_s:
        return 0.0
    if cut_out_m_s is not None and wind_speed_m_s >= cut_out_m_s:
        return 0.0
    if rated_power_w is not None and rated_power_w > 0:
        return min(power_w, rated_power_w)
    return power_w


@dataclass(frozen=True)
class ProductionReading:
    """One production sample, in the shape the agent's ``h2s_device`` parser reads."""

    timestamp: datetime
    device_id: int
    name: str
    source_type: str
    power_w: float
    voltage_v: float
    current_a: float
    energy_wh: float
    energy_total_wh: float
    power_factor: float

    def to_h2s_payload(self) -> dict:
        """Payload published on ``h2s/devices/<id>/status``.

        ``energy_total_wh`` is the counter the agent turns into window energy
        (it differentiates consecutive values); ``power_w`` is the averaged
        instant. Both stay inside the parser ranges: power ±1 MW, voltage
        0–1000 V, current ±5000 A, counter 0–1e12 Wh.
        """
        ts = self.timestamp.isoformat()
        if not ts.endswith("Z") and "+" not in ts:
            ts += "Z"
        return {
            "timestamp": ts,
            "device_id": self.device_id,
            "power_w": round(self.power_w, 2),
            "voltage_v": round(self.voltage_v, 1),
            "current_a": round(self.current_a, 3),
            "energy_wh": round(self.energy_wh, 3),
            "energy_total_wh": round(self.energy_total_wh, 3),
            "power_factor": round(self.power_factor, 3),
        }

    def to_dict(self) -> dict:
        payload = self.to_h2s_payload()
        payload.update({"name": self.name, "source_type": self.source_type})
        return payload


class ProductionSimulator:
    """Steps every declared production source against the current weather.

    State is the cumulative energy counter per source. It is persisted by the
    collector (``production_minute``) and resumed at restart so the counter never
    walks backwards — the agent derives window energy from its increments, and a
    counter that jumps back to zero would invent a full re-generation.
    """

    def __init__(self, sources: dict[int, dict] | None = None,
                 ac_voltage_v: float = PRODUCTION_AC_VOLTAGE_V,
                 power_factor: float = PRODUCTION_POWER_FACTOR,
                 min_output_w: float = PRODUCTION_MIN_OUTPUT_W):
        self.sources = dict(PRODUCTION_SOURCES if sources is None else sources)
        self.ac_voltage_v = ac_voltage_v
        self.power_factor = power_factor
        self.min_output_w = min_output_w
        self.energy_total_wh: dict[int, float] = {device_id: 0.0 for device_id in self.sources}
        self.last_readings: dict[int, ProductionReading] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.sources)

    def resume_counters(self, counters: dict[int, float]) -> None:
        """Restore the cumulative counters read back from the local database."""
        for device_id, value in counters.items():
            if device_id in self.energy_total_wh and value is not None and value > 0:
                self.energy_total_wh[device_id] = float(value)

    def step(self, irradiance_w_m2: float | None, wind_speed_m_s: float | None,
             timestamp: datetime | None = None, dt_seconds: float = 60.0) -> dict[int, ProductionReading]:
        """Advance every source one interval and return its reading, keyed by device id."""
        ts = timestamp or datetime.now(timezone.utc)
        readings: dict[int, ProductionReading] = {}
        for device_id, source in self.sources.items():
            power = self._source_power(source, irradiance_w_m2, wind_speed_m_s)
            if power < self.min_output_w:
                # Below the inverter's wake-up threshold: no output to report.
                power = 0.0
            energy_step_wh = power * max(0.0, dt_seconds) / 3600.0
            self.energy_total_wh[device_id] += energy_step_wh
            voltage, current = self._ac_output(power)
            pf = self.power_factor if power > 0 else 0.0
            readings[device_id] = ProductionReading(
                timestamp=ts,
                device_id=device_id,
                name=source["name"],
                source_type=source["type"],
                power_w=power,
                voltage_v=voltage,
                current_a=current,
                energy_wh=energy_step_wh,
                energy_total_wh=self.energy_total_wh[device_id],
                power_factor=pf,
            )
        self.last_readings = readings
        return readings

    def total_power_w(self) -> float:
        return round(sum(r.power_w for r in self.last_readings.values()), 2)

    def _source_power(self, source: dict, irradiance_w_m2: float | None,
                      wind_speed_m_s: float | None) -> float:
        if source["type"] == "SOLAR":
            return solar_power_w(irradiance_w_m2, source["surface_m2"], source["efficiency"])
        raw = wind_power_w(wind_speed_m_s, source["rotor_diameter_m"], source["efficiency"])
        return bound_to_machine(
            raw,
            wind_speed_m_s or 0.0,
            source.get("cut_in_m_s"),
            source.get("cut_out_m_s"),
            source.get("rated_power_w"),
        )

    def _ac_output(self, power_w: float) -> tuple[float, float]:
        """Inverter output seen by the meter: regulated AC voltage, current from P = U·I·cos φ."""
        if power_w <= 0:
            return self.ac_voltage_v, 0.0
        apparent = self.ac_voltage_v * max(1e-6, self.power_factor)
        return self.ac_voltage_v, min(5_000.0, power_w / apparent)
