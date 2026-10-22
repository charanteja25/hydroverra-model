"""Weather input for the HydroVerra model.

Real data: Open-Meteo archive (ERA5 reanalysis). No API key. Needs internet.
Fallback: SYNTHETIC data for tests only. The app shows a warning when it is used.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).parent / "data"
ABERDEEN = (57.15, -2.09)


def _url(lat: float, lon: float, year: int) -> str:
    q = {
        "latitude": lat,
        "longitude": lon,
        "start_date": f"{year}-01-01",
        "end_date": f"{year}-12-31",
        "hourly": "shortwave_radiation,wind_speed_100m,temperature_2m",
        "wind_speed_unit": "ms",
        "timezone": "UTC",
    }
    return "https://archive-api.open-meteo.com/v1/archive?" + urllib.parse.urlencode(q)


def parse_open_meteo(payload: dict) -> pd.DataFrame:
    h = payload["hourly"]
    df = pd.DataFrame(
        {
            "ghi_wm2": h["shortwave_radiation"],
            "wind_ms": h["wind_speed_100m"],
            "temp_c": h["temperature_2m"],
        },
        index=pd.to_datetime(h["time"], utc=True),
    )
    df.index.name = "time"
    return df.ffill().bfill()


def fetch_open_meteo(lat: float, lon: float, year: int) -> pd.DataFrame:
    with urllib.request.urlopen(_url(lat, lon, year), timeout=60) as r:
        return parse_open_meteo(json.loads(r.read()))


def synthetic(lat: float, year: int, seed: int = 1) -> pd.DataFrame:
    """SYNTHETIC weather. Not real. For tests and offline demos only."""
    idx = pd.date_range(f"{year}-01-01", f"{year}-12-31 23:00", freq="h", tz="UTC")
    doy = idx.dayofyear.to_numpy()
    hod = idx.hour.to_numpy()
    decl = 23.45 * np.sin(np.radians(360 * (284 + doy) / 365))
    elev = np.sin(np.radians(lat)) * np.sin(np.radians(decl)) + np.cos(
        np.radians(lat)
    ) * np.cos(np.radians(decl)) * np.cos(np.radians(15 * (hod - 12)))
    rng = np.random.default_rng(seed)
    cloud = np.clip(rng.normal(0.55, 0.25, len(idx)), 0.1, 1.0)
    ghi = np.clip(elev, 0, None) * 900 * cloud
    season = 1 + 0.25 * np.cos(np.radians(360 * (doy - 15) / 365))
    wind = np.clip(rng.weibull(2.0, len(idx)) * 8.0 * season, 0, 30)
    temp = 8 - 6 * np.cos(np.radians(360 * (doy - 15) / 365))
    df = pd.DataFrame(
        {"ghi_wm2": ghi, "wind_ms": wind, "temp_c": temp}, index=idx
    )
    df.index.name = "time"
    return df


def load_weather(lat: float, lon: float, year: int, allow_fallback: bool = True):
    """Return (dataframe, source_label). Cache real data in data/."""
    cache = DATA_DIR / f"weather_{lat:.2f}_{lon:.2f}_{year}.csv"
    if cache.exists():
        df = pd.read_csv(cache, index_col=0, parse_dates=True)
        return df, f"cache: {cache.name}"
    try:
        df = fetch_open_meteo(lat, lon, year)
        DATA_DIR.mkdir(exist_ok=True)
        df.to_csv(cache)
        return df, "Open-Meteo ERA5 (real data)"
    except Exception as exc:  # no internet, API change, etc.
        if not allow_fallback:
            raise
        return synthetic(lat, year), f"SYNTHETIC (fetch failed: {type(exc).__name__})"
