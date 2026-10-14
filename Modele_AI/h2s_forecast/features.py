"""Feature engineering for every model family (report §3.3).

Every builder returns ``(X, Y, meta)`` where row *t* only uses information available at the
forecast origin *t* (strictly causal), plus deterministic calendar / solar-geometry values and
NWP forecasts for the target time (which are published before the origin).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config as C
from .data import clear_sky_ghi, simulate_production, solar_elevation, wind_power


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _cyclic(df: pd.DataFrame, idx: pd.DatetimeIndex, prefix: str = "") -> None:
    hour = idx.hour + idx.minute / 60.0
    df[f"{prefix}hour_sin"] = np.sin(2 * np.pi * hour / 24)
    df[f"{prefix}hour_cos"] = np.cos(2 * np.pi * hour / 24)
    df[f"{prefix}dow_sin"] = np.sin(2 * np.pi * idx.dayofweek / 7)
    df[f"{prefix}dow_cos"] = np.cos(2 * np.pi * idx.dayofweek / 7)
    df[f"{prefix}doy_sin"] = np.sin(2 * np.pi * idx.dayofyear / 365.25)
    df[f"{prefix}doy_cos"] = np.cos(2 * np.pi * idx.dayofyear / 365.25)
    df[f"{prefix}is_weekend"] = (idx.dayofweek >= 5).astype(int)


def _easter(year: int) -> pd.Timestamp:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = ((h + l_ - 7 * m + 114) % 31) + 1
    return pd.Timestamp(year, month, day)


def french_holidays(years=range(2023, 2028)) -> set:
    out = set()
    for y in years:
        e = _easter(y)
        for md in ["01-01", "05-01", "05-08", "07-14", "08-15", "11-01", "11-11", "12-25"]:
            out.add(pd.Timestamp(f"{y}-{md}").date())
        for off in (1, 39, 50):  # Easter Monday, Ascension, Whit Monday
            out.add((e + pd.Timedelta(days=off)).date())
    return out


HOLIDAYS = french_holidays()


def _is_holiday(idx: pd.DatetimeIndex) -> np.ndarray:
    return np.array([d in HOLIDAYS for d in idx.date], dtype=int)


# ---------------------------------------------------------------------------
# Consumption (minute-resolution forecast origins)
# ---------------------------------------------------------------------------
def consumption_dataset(wide: pd.DataFrame, valid: pd.Series, weather: pd.DataFrame,
                        horizon_min: int = 60) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    """Target: energy (Wh) consumed in the window (t + h - 60 min, t + h].

    Forecast origins are taken every minute (≈ 60x more training samples than hourly origins);
    evaluation is reported on origins aligned on the hour.
    """
    h = horizon_min
    devs = list(wide.columns)
    p = wide.sum(axis=1)
    e = p / 60.0
    # energy in (t-60, t]
    e60 = e.rolling(60, min_periods=60).sum()
    v60 = valid.astype(float).rolling(60, min_periods=60).mean()
    y = e60.shift(-h)
    y_valid = v60.shift(-h) >= 0.95

    X = pd.DataFrame(index=wide.index)
    for w in [1, 5, 15, 30, 60, 180, 360, 720]:
        X[f"p_mean_{w}m"] = p.rolling(w, min_periods=1).mean()
    X["p_std_60m"] = p.rolling(60, min_periods=2).std()
    X["p_max_60m"] = p.rolling(60, min_periods=1).max()
    X["p_ema_60m"] = p.ewm(span=60).mean()
    X["p_ema_240m"] = p.ewm(span=240).mean()
    X["e_last_60m"] = e.rolling(60, min_periods=1).sum()
    X["e_trend_60m"] = X["e_last_60m"] - e.shift(60).rolling(60, min_periods=1).sum()
    X["e_last_24h"] = e.rolling(1440, min_periods=60).sum()
    # same target window one day / one week earlier (always in the past for h <= 24 h)
    if h <= 1440:
        X["e_target_lag24h"] = e60.shift(1440 - h)
    X["e_target_lag168h"] = e60.shift(10080 - h)
    X["e_target_lag48h"] = e60.shift(2880 - h)

    for d in devs:
        on = (wide[d] > C.DEVICE_ON_THRESHOLD_W).astype(int)
        grp = (on != on.shift()).cumsum()
        X[f"dev_{d}_p15m"] = wide[d].rolling(15, min_periods=1).mean()
        X[f"dev_{d}_on"] = on
        X[f"dev_{d}_since_change_m"] = on.groupby(grp).cumcount()
        X[f"dev_{d}_e60m"] = (wide[d] / 60.0).rolling(60, min_periods=1).sum()
        X[f"dev_{d}_ontime_24h"] = on.rolling(1440, min_periods=1).mean()
    X["n_devices_on"] = sum(X[f"dev_{d}_on"] for d in devs)
    X["n_devices_changed_recently"] = sum((X[f"dev_{d}_since_change_m"] < 15).astype(int) for d in devs)

    # Typical daily profile of the target slot, computed from *strictly past* days
    start = X.index + pd.Timedelta(minutes=h - 60)
    slot = start.hour * 4 + start.minute // 15
    day = start.normalize()
    tab = pd.DataFrame({"v": e60.shift(-60).values, "day": e60.index.normalize(),
                        "slot": e60.index.hour * 4 + e60.index.minute // 15})
    daily = tab.groupby(["day", "slot"])["v"].mean().unstack()
    k = 1 + (h - 60 + 1439) // 1440
    prof_all = daily.shift(k).expanding().mean().stack().rename("profile_all")
    prof_7d = daily.shift(k).rolling(7, min_periods=1).mean().stack().rename("profile_7d")
    keys = pd.MultiIndex.from_arrays([day, slot])
    X["profile_all"] = prof_all.reindex(keys).values
    X["profile_7d"] = prof_7d.reindex(keys).values

    # Calendar of the target window + daylight (lighting loads)
    tgt = X.index + pd.Timedelta(minutes=h - 30)
    _cyclic(X, tgt, "tgt_")
    X["tgt_hour_sin_weekend"] = X["tgt_hour_sin"] * X["tgt_is_weekend"]
    X["tgt_hour_cos_weekend"] = X["tgt_hour_cos"] * X["tgt_is_weekend"]
    X["tgt_sun_elev"] = solar_elevation(pd.DatetimeIndex(tgt) + pd.Timedelta(minutes=30))
    X["now_sun_elev"] = solar_elevation(X.index + pd.Timedelta(minutes=30))
    w = weather.reindex(X.index.floor("h"))
    X["temperature_now"] = w["temperature"].values
    X["heating_demand"] = np.maximum(18.0 - X["temperature_now"], 0.0)
    X["cooling_demand"] = np.maximum(X["temperature_now"] - 24.0, 0.0)
    X["radiation_now"] = w["radiation"].values

    meta = pd.DataFrame({"y_valid": y_valid.fillna(False), "hourly_origin": X.index.minute == 0}, index=X.index)
    keep = y.notna()
    return X[keep], y[keep].rename("energy_total_wh"), meta[keep]


# ---------------------------------------------------------------------------
# Renewable production (hourly)
# ---------------------------------------------------------------------------
def production_dataset(obs: pd.DataFrame, fc: pd.DataFrame, horizon_h: int,
                       oracle: bool = False) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    """Target: simulated production (Wh) at T+h computed from *observed* weather.

    ``oracle=False`` (operational): weather at the target hour comes from archived NWP forecasts
    (lead day-0 for 1 h, day-1 for 24 h). ``oracle=True``: observed weather is used instead,
    which isolates the model error from the forecast error (upper bound, §5.5).
    """
    lead = "fc0" if horizon_h <= 6 else "fc1"
    prod = simulate_production(obs)["production_wh"]
    idx = obs.index
    tgt_idx = idx + pd.Timedelta(hours=horizon_h)
    X = pd.DataFrame(index=idx)

    src = obs if oracle else fc.rename(columns=lambda c: c.replace(f"_{lead}", "") if c.endswith(lead) else f"__{c}")
    tw = src.reindex(tgt_idx)
    for v in ["radiation", "wind", "cloud_cover", "temperature", "humidity", "rain"]:
        X[f"tgt_{v}_fc"] = tw[v].values
    X["tgt_prod_physics_fc"] = (C.P_PV_W * tw["radiation"].clip(lower=0) / 1000.0).values + wind_power(tw["wind"].values)
    X["tgt_wind_cubed_fc"] = np.clip(tw["wind"].values, 0, C.V_NOM) ** 3
    X["tgt_clear_sky"] = clear_sky_ghi(tgt_idx)
    X["tgt_sun_elev"] = solar_elevation(tgt_idx)
    X["tgt_clearness_fc"] = X["tgt_radiation_fc"] / np.maximum(X["tgt_clear_sky"], 20)
    _cyclic(X, tgt_idx, "tgt_")

    # Recent observations (nowcasting) and forecast error persistence
    X["prod_now"] = prod.values
    X["prod_lag1"] = prod.shift(1).values
    X["prod_same_hour_yday"] = prod.shift(24 - horizon_h % 24).values if horizon_h % 24 else prod.values
    X["prod_mean_24h"] = prod.rolling(24, min_periods=1).mean().values
    X["radiation_now"] = obs["radiation"].values
    X["wind_now"] = obs["wind"].values
    X["cloud_now"] = obs["cloud_cover"].values
    X["clearness_now"] = obs["radiation"].values / np.maximum(clear_sky_ghi(idx), 20)
    if not oracle:
        now_fc = fc.reindex(idx)
        X["radiation_fc_err_now"] = obs["radiation"].values - now_fc[f"radiation_{lead}"].values
        X["wind_fc_err_now"] = obs["wind"].values - now_fc[f"wind_{lead}"].values
        X["wind_fc_err_mean6h"] = pd.Series(X["wind_fc_err_now"].values, idx).rolling(6, min_periods=1).mean().values

    y = prod.reindex(tgt_idx)
    y.index = idx
    keep = y.notna()
    meta = pd.DataFrame({"target_time": tgt_idx}, index=idx)
    return X[keep], y[keep].rename("production_wh"), meta[keep]


# ---------------------------------------------------------------------------
# Weather (hourly MOS: NWP forecast + local history + clear-sky prior)
# ---------------------------------------------------------------------------
WEATHER_TARGETS = ["temperature", "humidity", "wind", "radiation"]


def weather_dataset(obs: pd.DataFrame, fc: pd.DataFrame, horizon_h: int) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Model Output Statistics: corrects the NWP forecast with local observations.

    NWP columns (``*_nwp*``) are randomly masked during training so that the same model also
    works as an autoregressive fallback when the weather API is unavailable (degraded mode).
    """
    lead = "fc0" if horizon_h <= 6 else "fc1"
    idx = obs.index
    tgt_idx = idx + pd.Timedelta(hours=horizon_h)
    X = pd.DataFrame(index=idx)
    cs_now = clear_sky_ghi(idx)
    tw = fc.reindex(tgt_idx)
    nw = fc.reindex(idx)
    for v in WEATHER_TARGETS + ["cloud_cover", "rain"]:
        s = obs[v]
        for lag in [0, 1, 2, 3, 6, 12, 24]:
            X[f"{v}_lag{lag}"] = s.shift(lag).values
        X[f"{v}_mean6h"] = s.rolling(6, min_periods=1).mean().values
        X[f"{v}_mean24h"] = s.rolling(24, min_periods=1).mean().values
        X[f"{v}_diff1"] = s.diff(1).values
        X[f"{v}_diff3"] = s.diff(3).values
        if horizon_h % 24:
            X[f"{v}_same_hour_tgt_yday"] = s.shift(24 - horizon_h % 24).values
        X[f"tgt_{v}_nwp"] = tw[f"{v}_{lead}"].values
        X[f"{v}_nwp_err_now"] = s.values - nw[f"{v}_{lead}"].values
    X["clearness_now"] = obs["radiation"].values / np.maximum(cs_now, 20)
    X["clearness_mean6h"] = pd.Series(X["clearness_now"].values, idx).where(cs_now > 20).rolling(6, min_periods=1).mean().ffill().values
    X["tgt_clear_sky"] = clear_sky_ghi(tgt_idx)
    X["tgt_sun_elev"] = solar_elevation(tgt_idx)
    _cyclic(X, tgt_idx, "tgt_")
    Y = obs[WEATHER_TARGETS].reindex(tgt_idx)
    Y.index = idx
    keep = Y.notna().all(axis=1)
    meta = pd.DataFrame({"target_time": tgt_idx}, index=idx)
    return X[keep], Y[keep], meta[keep]


def nwp_columns(X: pd.DataFrame) -> list[str]:
    """Columns that depend on the external weather API (masked in degraded mode)."""
    return [c for c in X.columns if "_nwp" in c or c.endswith("_fc") or "_fc_" in c]


def recent_sensor_columns(X: pd.DataFrame) -> list[str]:
    """Consumption columns that depend on the last 60 min of plug readings (masked in degraded mode)."""
    return [c for c in X.columns if c.startswith("dev_") or c in
            ("p_mean_1m", "p_mean_5m", "p_mean_15m", "p_mean_30m", "p_mean_60m", "p_std_60m", "p_max_60m",
             "e_last_60m", "e_trend_60m", "n_devices_on", "n_devices_changed_recently")]


# ---------------------------------------------------------------------------
# Day-ahead spot price (hourly)
# ---------------------------------------------------------------------------
def price_dataset(prices: pd.DataFrame, obs: pd.DataFrame, fc: pd.DataFrame,
                  horizon_h: int) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    """Strict setting: only prices up to the origin are used (no published day-ahead values)."""
    lead = "fc0" if horizon_h <= 6 else "fc1"
    s = prices["price"].interpolate(limit=3)
    idx = s.index
    tgt_idx = idx + pd.Timedelta(hours=horizon_h)
    X = pd.DataFrame(index=idx)
    for lag in [0, 1, 2, 3, 6, 12]:
        X[f"price_lag{lag}"] = s.shift(lag).values
    for back in [24, 48, 168]:   # same hour as the target, `back` hours before the target
        X[f"price_tgt_minus{back}h"] = s.shift(back - horizon_h).values
    for w in [6, 24, 168]:
        X[f"price_mean{w}h"] = s.rolling(w, min_periods=1).mean().values
    X["price_std24h"] = s.rolling(24, min_periods=2).std().values
    X["price_min24h"] = s.rolling(24, min_periods=1).min().values
    X["price_max24h"] = s.rolling(24, min_periods=1).max().values
    X["price_diff1"] = s.diff(1).values
    X["price_neg_share24h"] = (s < 0).astype(float).rolling(24, min_periods=1).mean().values
    tw = fc.reindex(tgt_idx)
    X["tgt_temperature_fc"] = tw[f"temperature_{lead}"].values
    X["tgt_wind_fc"] = tw[f"wind_{lead}"].values
    X["tgt_radiation_fc"] = tw[f"radiation_{lead}"].values
    X["temperature_now"] = obs["temperature"].reindex(idx).values
    X["temperature_mean24h"] = obs["temperature"].reindex(idx).rolling(24, min_periods=1).mean().values
    _cyclic(X, tgt_idx, "tgt_")
    X["tgt_holiday"] = _is_holiday(tgt_idx)
    X["tgt_month"] = tgt_idx.month
    y = s.reindex(tgt_idx)
    y.index = idx
    keep = y.notna()
    meta = pd.DataFrame({"target_time": tgt_idx}, index=idx)
    return X[keep], y[keep].rename("price_eur_mwh"), meta[keep]
