"""External data sources with on-disk caching.

* Weather observations (reanalysis)  : Open-Meteo Archive API
* Archived NWP forecasts            : Open-Meteo Previous-Runs API (lead day 0 and day 1)
* Day-ahead spot prices (EPEX, FR)  : Energy-Charts API (Fraunhofer ISE, CC BY 4.0)

All series are returned as hourly, timezone-naive UTC ``DataFrame`` indexed by ``time``.
"""
from __future__ import annotations

import time

import pandas as pd
import requests

from . import config as C

WEATHER_VARS = {
    "temperature_2m": "temperature",
    "relative_humidity_2m": "humidity",
    "cloud_cover": "cloud_cover",
    "wind_speed_10m": "wind",
    "shortwave_radiation": "radiation",
    "precipitation": "rain",
}


def _get(url: str, params: dict, retries: int = 6) -> dict:
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=120)
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(5 * 2 ** attempt)   # 5, 10, 20, 40, 80 s – APIs rate-limit with HTTP 429
    raise RuntimeError("unreachable")


def _cached(name: str, builder) -> pd.DataFrame:
    C.EXTERNAL_DIR.mkdir(parents=True, exist_ok=True)
    path = C.EXTERNAL_DIR / f"{name}.csv"
    if path.exists():
        return pd.read_csv(path, parse_dates=["time"], index_col="time")
    df = builder()
    df.to_csv(path)
    return df


def _chunks(start: str, end: str, months: int):
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    while s <= e:
        n = min(s + pd.DateOffset(months=months) - pd.Timedelta(days=1), e)
        yield s.strftime("%Y-%m-%d"), n.strftime("%Y-%m-%d")
        s = n + pd.Timedelta(days=1)


def weather_observed(start: str = C.HISTORY_START, end: str = C.HISTORY_END) -> pd.DataFrame:
    """Hourly observed weather (ERA5-based reanalysis). Wind in m/s, radiation in W/m²."""

    def build():
        js = _get(
            "https://archive-api.open-meteo.com/v1/archive",
            {
                "latitude": C.SITE_LAT, "longitude": C.SITE_LON,
                "start_date": start, "end_date": end,
                "hourly": ",".join(WEATHER_VARS), "wind_speed_unit": "ms", "timezone": "GMT",
            },
        )
        df = pd.DataFrame(js["hourly"]).rename(columns=WEATHER_VARS)
        df["time"] = pd.to_datetime(df["time"])
        return df.set_index("time")

    return _cached(f"weather_observed_{start}_{end}", build)


def weather_forecast(start: str = C.HISTORY_START, end: str = C.HISTORY_END) -> pd.DataFrame:
    """Archived NWP forecasts. Columns ``<var>_fc0`` (latest run, ~0-6 h lead) and ``<var>_fc1`` (issued day-1)."""

    def build():
        frames = []
        hourly = []
        for v in WEATHER_VARS:
            hourly += [v, f"{v}_previous_day1"]
        for s, e in _chunks(start, end, months=6):
            js = _get(
                "https://previous-runs-api.open-meteo.com/v1/forecast",
                {
                    "latitude": C.SITE_LAT, "longitude": C.SITE_LON,
                    "start_date": s, "end_date": e, "hourly": ",".join(hourly),
                    "wind_speed_unit": "ms", "timezone": "GMT",
                },
            )
            frames.append(pd.DataFrame(js["hourly"]))
        df = pd.concat(frames, ignore_index=True)
        ren = {}
        for v, short in WEATHER_VARS.items():
            ren[v] = f"{short}_fc0"
            ren[f"{v}_previous_day1"] = f"{short}_fc1"
        df = df.rename(columns=ren)
        df["time"] = pd.to_datetime(df["time"])
        return df.set_index("time")

    return _cached(f"weather_forecast_{start}_{end}", build)


def spot_prices(start: str = C.HISTORY_START, end: str = C.HISTORY_END, zone: str = C.PRICE_ZONE) -> pd.DataFrame:
    """Hourly day-ahead spot price (EUR/MWh). 15-min products (since Oct. 2025) are averaged to 1 h."""

    def build():
        frames = []
        for s, e in _chunks(start, end, months=6):
            js = _get("https://api.energy-charts.info/price", {"bzn": zone, "start": s, "end": e})
            time.sleep(3)
            frames.append(pd.DataFrame({"time": pd.to_datetime(js["unix_seconds"], unit="s"), "price": js["price"]}))
        df = pd.concat(frames).drop_duplicates("time").set_index("time").sort_index()
        return df.resample("h").mean()

    return _cached(f"spot_prices_{zone}_{start}_{end}", build)
