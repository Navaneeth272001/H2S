"""Site data loading, cleaning, imputation and physical simulation.

Pipeline (see report §3):
1. Raw Shelly plug readings (~60 s sampling, 8 devices) -> 1-minute grid per device.
2. Short gaps (<= 5 min) are linearly interpolated; longer gaps are flagged invalid and
   excluded from targets (never silently filled with zeros as in the v1 POC).
3. Hourly energy is derived from the cleaned minute grid; hours with < 90 % coverage are flagged.
4. Renewable production is *simulated* from observed weather (200 W PV + 200 W wind turbine),
   exactly as in the v1 POC, because the pilot site has no production meter yet.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config as C


# ---------------------------------------------------------------------------
# Consumption (Shelly plugs)
# ---------------------------------------------------------------------------
def load_raw_energy() -> pd.DataFrame:
    df = pd.concat([pd.read_csv(f) for f in C.RAW_ENERGY_FILES], ignore_index=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.drop_duplicates(["timestamp", "device_id"]).sort_values("timestamp")
    return df


def minute_power(df_raw: pd.DataFrame | None = None, max_gap_min: int = 5) -> tuple[pd.DataFrame, pd.Series]:
    """Per-device active power on a regular 1-minute grid and the validity mask."""
    df_raw = load_raw_energy() if df_raw is None else df_raw
    wide = df_raw.pivot_table(index="timestamp", columns="device_name", values="power_w", aggfunc="mean")
    wide = wide.resample("1min").mean()
    wide = wide.interpolate(limit=max_gap_min, limit_area="inside")
    valid = wide.notna().all(axis=1)
    return wide.fillna(0.0), valid


def hourly_consumption(wide: pd.DataFrame, valid: pd.Series, min_coverage: float = 0.9) -> pd.DataFrame:
    """Hourly energy per device and total (Wh) with a coverage flag."""
    e = wide / 60.0
    hourly = e.resample("h").sum()
    hourly["energy_total_wh"] = hourly.sum(axis=1)
    hourly["coverage"] = valid.resample("h").mean()
    hourly["valid"] = hourly["coverage"] >= min_coverage
    return hourly


def ema_impute(s: pd.Series, mask: pd.Series, span: int = 24) -> pd.Series:
    """Replace invalid points with an exponential moving average of the valid past (EMA imputation)."""
    clean = s.where(mask)
    ema = clean.ewm(span=span, ignore_na=True).mean().shift(1)
    return clean.fillna(ema).fillna(s)


# ---------------------------------------------------------------------------
# Renewable production & battery (physics-based simulation)
# ---------------------------------------------------------------------------
def wind_power(v: pd.Series | np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=float)
    p = np.where(v <= C.V_NOM, C.P_WIND_W * (v / C.V_NOM) ** 3, C.P_WIND_W)
    return np.where((v < C.V_CUT_IN) | (v > C.V_CUT_OUT), 0.0, p)


def simulate_production(weather: pd.DataFrame, rad_col: str = "radiation", wind_col: str = "wind") -> pd.DataFrame:
    out = pd.DataFrame(index=weather.index)
    out["solar_wh"] = (C.P_PV_W * weather[rad_col] / 1000.0).clip(lower=0)
    out["wind_wh"] = wind_power(weather[wind_col])
    out["production_wh"] = out["solar_wh"] + out["wind_wh"]
    return out


def simulate_battery(production: pd.Series, consumption: pd.Series) -> pd.DataFrame:
    """Greedy battery dispatch used by the digital twin (Brick 5); SOC is *derived*, not learned."""
    soc = C.BATTERY_SOC_INIT_WH
    rows = []
    for p, c in zip(production.values, consumption.values):
        net = p - c
        if net >= 0:
            ch = min(net, C.BATTERY_CAPACITY_WH - soc)
            soc += ch
            rows.append((soc, ch, 0.0, 0.0))
        else:
            dis = min(-net, soc)
            soc -= dis
            rows.append((soc, 0.0, dis, -net - dis))
    return pd.DataFrame(rows, index=production.index,
                        columns=["battery_soc", "battery_charge", "battery_discharge", "grid_needed"])


# ---------------------------------------------------------------------------
# Solar geometry (clear-sky prior used by production and weather models)
# ---------------------------------------------------------------------------
def solar_elevation(idx: pd.DatetimeIndex, lat: float = C.SITE_LAT, lon: float = C.SITE_LON) -> np.ndarray:
    """Approximate solar elevation (degrees) at the middle of the preceding hour (Open-Meteo convention)."""
    t = idx - pd.Timedelta(minutes=30)
    doy = t.dayofyear.values
    hour = t.hour.values + t.minute.values / 60.0
    decl = np.deg2rad(23.44) * np.sin(2 * np.pi * (284 + doy) / 365.0)
    b = 2 * np.pi * (doy - 81) / 364.0
    eot = 9.87 * np.sin(2 * b) - 7.53 * np.cos(b) - 1.5 * np.sin(b)          # minutes
    solar_time = hour + (4 * lon + eot) / 60.0
    ha = np.deg2rad(15 * (solar_time - 12))
    phi = np.deg2rad(lat)
    sin_el = np.sin(phi) * np.sin(decl) + np.cos(phi) * np.cos(decl) * np.cos(ha)
    return np.rad2deg(np.arcsin(np.clip(sin_el, -1, 1)))


def clear_sky_ghi(idx: pd.DatetimeIndex) -> np.ndarray:
    """Haurwitz clear-sky GHI model (W/m²)."""
    el = solar_elevation(idx)
    cz = np.sin(np.deg2rad(el))
    return np.where(cz > 0, 1098 * cz * np.exp(-0.057 / np.maximum(cz, 1e-3)), 0.0)
