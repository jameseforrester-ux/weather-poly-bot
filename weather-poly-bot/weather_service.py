"""
weather_service.py — Open-Meteo multi-model ensemble
Fetches ECMWF IFS, NOAA GFS, and DWD ICON forecasts, then blends
them using backtested weights to produce a single best-guess temperature.
"""
import asyncio
import math
from datetime import date, datetime, timedelta
from typing import Optional

import httpx
import pytz

from config import (
    DEFAULT_WEIGHTS, HTTP_TIMEOUT, MODELS,
    OM_FORECAST_URL, OM_GEOCODE_URL,
    OM_ARCHIVE_URL, OM_HIST_FORECAST_URL,
    PRED_UNCERTAINTY_C,
)


# ─────────────────────────────────────────────────────────
#  Utility: temp conversion & normal CDF (no scipy)
# ─────────────────────────────────────────────────────────
def c_to_f(c: float) -> float:
    return round(c * 9 / 5 + 32, 1)


def f_to_c(f: float) -> float:
    return round((f - 32) * 5 / 9, 1)


def _norm_cdf(x: float, mu: float, sigma: float) -> float:
    """Cumulative distribution function of N(mu, sigma)."""
    z = (x - mu) / (sigma * math.sqrt(2))
    return (1.0 + math.erf(z)) / 2.0


def prob_exceeds(predicted_c: float, threshold_c: float,
                 uncertainty_c: float = PRED_UNCERTAINTY_C) -> float:
    """P(actual_high > threshold) given predicted_high ~ N(pred, σ)."""
    return 1.0 - _norm_cdf(threshold_c, predicted_c, uncertainty_c)


# ─────────────────────────────────────────────────────────
#  Geocoding
# ─────────────────────────────────────────────────────────
async def geocode(location: str) -> Optional[dict]:
    """Return first geocoding result or None."""
    params = {
        "name": location,
        "count": 5,
        "language": "en",
        "format": "json",
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(OM_GEOCODE_URL, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception:
        return None

    results = data.get("results", [])
    if not results:
        return None

    # Prefer airport results if the query looks like an ICAO/IATA code
    best = results[0]
    name   = best.get("name", "")
    admin1 = best.get("admin1", "")
    admin2 = best.get("admin2", "")
    country = best.get("country", "")
    tz     = best.get("timezone", "UTC")

    parts = [p for p in [name, admin1, country] if p]
    display = ", ".join(parts)

    return {
        "name":     name,
        "country":  country,
        "lat":      best["latitude"],
        "lon":      best["longitude"],
        "timezone": tz,
        "display":  display,
    }


# ─────────────────────────────────────────────────────────
#  Single-model forecast fetch
# ─────────────────────────────────────────────────────────
async def _fetch_model(client: httpx.AsyncClient,
                       lat: float, lon: float, model_code: str) -> Optional[dict]:
    params = {
        "latitude":         lat,
        "longitude":        lon,
        "daily":            "temperature_2m_max,temperature_2m_min",
        "temperature_unit": "celsius",
        "forecast_days":    2,
        "models":           model_code,
        "timezone":         "auto",
    }
    try:
        r = await client.get(OM_FORECAST_URL, params=params)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


# ─────────────────────────────────────────────────────────
#  Ensemble prediction
# ─────────────────────────────────────────────────────────
async def ensemble_prediction(lat: float, lon: float, tz: str,
                              weights: dict = None) -> Optional[dict]:
    """
    Fetch all three models concurrently and return a weighted ensemble.
    Returns dict with today/tomorrow highs in °C and °F, plus model breakdown.
    """
    w = weights if weights else DEFAULT_WEIGHTS

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        tasks = {
            name: _fetch_model(client, lat, lon, code)
            for name, code in MODELS.items()
        }
        results = {name: await task for name, task in tasks.items()}

    # Parse per-model predictions
    model_data: dict[str, dict] = {}
    for model_name, raw in results.items():
        if not raw:
            continue
        daily = raw.get("daily", {})
        maxes = daily.get("temperature_2m_max", [None, None])
        mins  = daily.get("temperature_2m_min", [None, None])
        dates = daily.get("time", [])
        if len(maxes) >= 2 and maxes[0] is not None and maxes[1] is not None:
            model_data[model_name] = {
                "today_high":    maxes[0],
                "tomorrow_high": maxes[1],
                "today_low":     mins[0] if mins else None,
                "tomorrow_low":  mins[1] if mins else None,
                "dates":         dates,
            }

    if not model_data:
        return None

    # Weighted average (re-normalise for missing models)
    def wavg(key: str) -> float:
        total_w, total_v = 0.0, 0.0
        for m, d in model_data.items():
            if d.get(key) is not None:
                wt = w.get(m, 1 / len(MODELS))
                total_v += d[key] * wt
                total_w += wt
        return total_v / total_w if total_w else 0.0

    today_c    = round(wavg("today_high"),    1)
    tomorrow_c = round(wavg("tomorrow_high"), 1)

    # Local date labels
    local_tz  = pytz.timezone(tz)
    local_now = datetime.now(local_tz)
    today_label    = local_now.strftime("%a %b %-d")
    tomorrow_label = (local_now + timedelta(days=1)).strftime("%a %b %-d")

    return {
        "today_c":       today_c,
        "today_f":       c_to_f(today_c),
        "tomorrow_c":    tomorrow_c,
        "tomorrow_f":    c_to_f(tomorrow_c),
        "today_label":   today_label,
        "tomorrow_label":tomorrow_label,
        "local_time":    local_now.strftime("%I:%M %p %Z"),
        "models":        model_data,
        "weights":       w,
    }


# ─────────────────────────────────────────────────────────
#  Historical data helpers (used by backtest_service)
# ─────────────────────────────────────────────────────────
async def fetch_historical_forecast(lat: float, lon: float,
                                    model_code: str,
                                    start: date, end: date) -> Optional[dict]:
    """What did <model> predict for daily max-temp over [start, end]?"""
    params = {
        "latitude":         lat,
        "longitude":        lon,
        "start_date":       start.isoformat(),
        "end_date":         end.isoformat(),
        "daily":            "temperature_2m_max",
        "temperature_unit": "celsius",
        "models":           model_code,
        "timezone":         "UTC",
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(OM_HIST_FORECAST_URL, params=params)
            r.raise_for_status()
            return r.json()
    except Exception:
        return None


async def fetch_actual_temps(lat: float, lon: float,
                             start: date, end: date) -> Optional[dict]:
    """Observed daily max-temp over [start, end] from archive."""
    params = {
        "latitude":         lat,
        "longitude":        lon,
        "start_date":       start.isoformat(),
        "end_date":         end.isoformat(),
        "daily":            "temperature_2m_max",
        "temperature_unit": "celsius",
        "timezone":         "UTC",
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(OM_ARCHIVE_URL, params=params)
            r.raise_for_status()
            return r.json()
    except Exception:
        return None
