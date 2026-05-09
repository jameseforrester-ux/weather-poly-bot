"""Ensemble temperature forecasting from the world's best NWP models.

Architecture (May 2026 rewrite):
  - GLOBAL ENSEMBLE: 8 NWP models weighted by long-term skill, ECMWF IFS heaviest.
  - REGIONAL HIGH-RES: HRRR (US), HRDPS (Canada), AROME (France), UKMO 2km (UK),
    ICON-D2 (Central Europe), ARPEGE Europe (wider Europe), JMA MSM (Japan).
    Auto-selected by lat/lon. When available, gets a ~30% weight, displacing
    the global ensemble proportionally for that day's prediction.
  - OPENWEATHER: when OPENWEATHER_API_KEY is set, fetched separately and added
    to the ensemble with a small (5%) weight as a cross-source check.
  - METAR FLOOR: for today's max only, observed METAR temps for hours that have
    already passed today form a hard lower bound — predicted_max ≥ observed_max.
"""
import logging
import math
import os
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import httpx

log = logging.getLogger("weather")

OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
AVIATION_WX_METAR = "https://aviationweather.gov/api/data/metar"
OPENWEATHER_ONECALL = "https://api.openweathermap.org/data/3.0/onecall"

OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "").strip()

# Global ensemble — weights sum to ~1.0. Regional model and OpenWeather get
# their slice taken from this pool when they're available, proportionally.
MODEL_WEIGHTS: Dict[str, float] = {
    "ecmwf_ifs025":         0.25,   # gold standard, slightly bumped
    "ecmwf_aifs025":        0.18,
    "ukmo_seamless":        0.13,
    "icon_seamless":        0.13,
    "gfs_seamless":         0.10,
    "jma_seamless":         0.07,
    "meteofrance_seamless": 0.09,
    "gem_seamless":         0.05,
}
MODELS: List[str] = list(MODEL_WEIGHTS.keys())

MODEL_DISPLAY: Dict[str, str] = {
    "ecmwf_ifs025":         "ECMWF IFS 🇪🇺",
    "ecmwf_aifs025":        "ECMWF AIFS (AI) 🇪🇺",
    "ukmo_seamless":        "UK Met Office 🇬🇧",
    "icon_seamless":        "DWD ICON 🇩🇪",
    "gfs_seamless":         "NOAA GFS 🇺🇸",
    "jma_seamless":         "JMA 🇯🇵",
    "meteofrance_seamless": "Météo-France 🇫🇷",
    "gem_seamless":         "Env. Canada GEM 🇨🇦",
    # regional / extra
    "ncep_hrrr_conus":              "NOAA HRRR 🇺🇸 (3km)",
    "gem_hrdps_continental":        "EC HRDPS 🇨🇦 (2.5km)",
    "ukmo_uk_deterministic_2km":    "UKMO 2km 🇬🇧",
    "meteofrance_arome_france_hd":  "Météo-France AROME HD 🇫🇷 (1.5km)",
    "icon_d2":                      "DWD ICON-D2 🇩🇪 (2km)",
    "meteofrance_arpege_europe":    "Météo-France ARPEGE EU (11km)",
    "jma_msm":                      "JMA MSM 🇯🇵 (5km)",
    "openweather":                  "OpenWeather (composite)",
}

# Regional high-resolution models. Selected by lat/lon. Each is much higher
# resolution and more accurate at short range than any global model.
REGIONAL_WEIGHT = 0.30  # weight given to regional model when present


def _select_regional_model(lat: float, lon: float) -> Optional[str]:
    """Pick the best regional Open-Meteo model for this location, or None.
    Order matters: more specific / better-resolution regions first.
    """
    # Toronto and other southern Canadian cities sit inside the CONUS bbox
    # but should use HRDPS (Canada's high-res model). Check Canada first.
    if 41.5 <= lat <= 70.0 and -141.0 <= lon <= -52.0:
        # Quick heuristic: if we're north of the Great Lakes line OR in eastern
        # Canada, use HRDPS. Border-region US cities (e.g. Detroit) stay HRRR.
        if lat > 41.7 and lon > -95.0 and lon < -52.0:
            # Likely southern Ontario / Quebec / Maritimes — Canada side
            # only if explicitly north of US border at that longitude
            if lat > 43.5 and lon > -80.0:
                return "gem_hrdps_continental"
        # General Canada bbox check (excludes most US border cities)
        if lat >= 49.0:
            return "gem_hrdps_continental"
    # CONUS (HRRR) — covers Lower 48 + parts of S. Canada
    if 24.0 <= lat <= 50.0 and -125.0 <= lon <= -66.0:
        return "ncep_hrrr_conus"
    # UK 2km — Britain & Ireland
    if 49.0 <= lat <= 61.0 and -11.0 <= lon <= 2.5:
        return "ukmo_uk_deterministic_2km"
    # AROME France HD — France + immediate borders
    if 41.0 <= lat <= 51.5 and -5.5 <= lon <= 10.5:
        return "meteofrance_arome_france_hd"
    # ICON-D2 Central Europe — Germany + neighbors
    if 45.0 <= lat <= 56.5 and 4.5 <= lon <= 19.5:
        return "icon_d2"
    # ARPEGE Europe — wider Europe (11km, but still better than global at short range)
    if 32.0 <= lat <= 72.0 and -25.0 <= lon <= 45.0:
        return "meteofrance_arpege_europe"
    # JMA MSM — Japan
    if 22.0 <= lat <= 47.5 and 122.0 <= lon <= 147.0:
        return "jma_msm"
    return None


@dataclass
class DayForecast:
    date: date
    predicted_max_c: int          # whole-number Celsius
    predicted_max_f: int          # whole-number Fahrenheit
    confidence: float             # 0..1
    confidence_level: str         # HIGH / MEDIUM / LOW
    high_confidence: bool         # green-flag eligible
    std_c: float                  # spread (Celsius) across models
    ensemble_mean_c: float
    model_values_c: Dict[str, float]
    probability_c: Dict[int, float]
    probability_f: Dict[int, float]


@dataclass
class CurrentObs:
    """Live observation, preferably from the airport's METAR station."""
    source: str                   # "METAR" or "Open-Meteo"
    temp_c: Optional[float]
    temp_f: Optional[float]
    wind_kt: Optional[float]
    wind_dir: Optional[int]
    wx: Optional[str]
    raw: Optional[str]
    observed_at: Optional[str]


# ─────────────────────────── ensemble forecast ──────────────────────────────
async def fetch_ensemble_forecast(
    lat: float,
    lon: float,
    days: int = 7,
    icao: Optional[str] = None,
) -> Optional[List[DayForecast]]:
    """Build the daily-max ensemble forecast.

    Augmentations vs. global-only ensemble:
      1. REGIONAL MODEL — one high-res regional NWP model auto-picked by
         lat/lon (HRRR for US, AROME for France, etc.). When it returns data
         for a day, it gets `REGIONAL_WEIGHT` (~30%) of the blend, with the
         rest of the weight redistributed proportionally among the global
         models that also returned data.
      2. OPENWEATHER — if OPENWEATHER_API_KEY env is set, OpenWeather's daily
         forecast is added as a synthetic model with a small weight.
      3. METAR FLOOR — if `icao` is provided and the day is today (in the
         location's local TZ), we fetch up to 12 hours of past METAR temps
         and use the max as a hard lower bound on today's predicted max.
    """
    regional = _select_regional_model(lat, lon)

    # Build the model list for the API request.
    request_models = list(MODELS)
    if regional and regional not in request_models:
        request_models.append(regional)

    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "temperature_unit": "celsius",
        "timezone": "auto",
        "forecast_days": max(1, min(days, 16)),
        "models": ",".join(request_models),
    }
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.get(OPEN_METEO_FORECAST, params=params)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            log.warning("ensemble fetch failed: %s", e)
            return None

    daily = data.get("daily") or {}
    times = daily.get("time") or []
    if not times:
        return None

    # OpenWeather pulls daily-max for up to 8 days, keyed by date.
    ow_max_by_date: Dict[date, float] = {}
    if OPENWEATHER_API_KEY:
        ow_max_by_date = await _fetch_openweather_daily_max(lat, lon)

    # METAR observed-so-far max — only relevant when we know the airport.
    metar_floor_today: Optional[float] = None
    today_local: Optional[date] = None
    if icao:
        # The /v1/forecast 'timezone=auto' aligns dates to the location's
        # local timezone, so the first time entry is "today" locally.
        try:
            today_local = date.fromisoformat(times[0])
        except ValueError:
            today_local = None
        metar_floor_today = await _fetch_metar_observed_max_today(icao, hours=12)

    forecasts: List[DayForecast] = []
    for i, t_str in enumerate(times):
        try:
            d = date.fromisoformat(t_str)
        except ValueError:
            continue

        # Collect all available model values for this day.
        model_values: Dict[str, float] = {}
        for m in request_models:
            arr = daily.get(f"temperature_2m_max_{m}")
            if arr and i < len(arr) and arr[i] is not None:
                try:
                    model_values[m] = float(arr[i])
                except (TypeError, ValueError):
                    continue
        # OpenWeather as a synthetic model
        if d in ow_max_by_date:
            model_values["openweather"] = ow_max_by_date[d]

        if not model_values:
            continue

        # Build effective weights: regional gets REGIONAL_WEIGHT, OpenWeather
        # gets a small slice, the rest of MODEL_WEIGHTS is normalized into
        # what's left.
        effective_weights = _build_effective_weights(
            model_values, regional_model=regional,
        )

        weight_sum = sum(effective_weights[m] for m in model_values)
        weighted_mean = sum(
            effective_weights[m] * v for m, v in model_values.items()
        ) / weight_sum

        # METAR floor only for today
        floor_applied = False
        if (today_local and d == today_local
                and metar_floor_today is not None
                and metar_floor_today > weighted_mean):
            log.info("metar floor: %.1f°C raised %s prediction from %.1f→%.1f",
                     metar_floor_today, d, weighted_mean, metar_floor_today)
            weighted_mean = metar_floor_today
            floor_applied = True

        # Spread (Celsius)
        if len(model_values) >= 2:
            std_c = statistics.stdev(model_values.values())
        else:
            std_c = 1.5
        # If METAR floor displaced the mean, that's a sign the forecast
        # under-shot reality — bump uncertainty slightly.
        if floor_applied:
            std_c = max(std_c, 0.8)

        confidence = max(0.0, min(1.0, 1.0 - std_c / 3.0))
        if confidence >= 0.75:
            level = "HIGH"
        elif confidence >= 0.50:
            level = "MEDIUM"
        else:
            level = "LOW"

        high_conf = std_c <= 1.0

        pred_c = int(round(weighted_mean))
        pred_f = int(round(weighted_mean * 9 / 5 + 32))

        prob_c = _integer_probs(weighted_mean, std_c)
        f_mean = weighted_mean * 9 / 5 + 32
        f_std = max(std_c * 9 / 5, 0.1)
        prob_f = _integer_probs(f_mean, f_std)

        forecasts.append(
            DayForecast(
                date=d,
                predicted_max_c=pred_c,
                predicted_max_f=pred_f,
                confidence=confidence,
                confidence_level=level,
                high_confidence=high_conf,
                std_c=std_c,
                ensemble_mean_c=weighted_mean,
                model_values_c=model_values,
                probability_c=prob_c,
                probability_f=prob_f,
            )
        )
    return forecasts


def _build_effective_weights(
    model_values: Dict[str, float],
    regional_model: Optional[str],
) -> Dict[str, float]:
    """Compute the actual blending weights for the models that returned data.

    Logic:
      - Regional model gets REGIONAL_WEIGHT (0.30) if present.
      - OpenWeather gets 0.05 if present.
      - The remainder is split among the global models proportionally to
        their declared MODEL_WEIGHTS.
    """
    weights: Dict[str, float] = {}
    used = 0.0
    if regional_model and regional_model in model_values:
        weights[regional_model] = REGIONAL_WEIGHT
        used += REGIONAL_WEIGHT
    if "openweather" in model_values:
        weights["openweather"] = 0.05
        used += 0.05
    remaining = max(0.0, 1.0 - used)
    # Sum of declared weights for the global models that returned data
    global_present = [m for m in model_values
                      if m in MODEL_WEIGHTS and m != regional_model]
    decl_sum = sum(MODEL_WEIGHTS[m] for m in global_present) or 1.0
    for m in global_present:
        weights[m] = MODEL_WEIGHTS[m] / decl_sum * remaining
    return weights


async def _fetch_openweather_daily_max(
    lat: float, lon: float
) -> Dict[date, float]:
    """Return {date: daily_max_c} from OpenWeather One Call (daily) — empty
    dict on any failure (silent fallback)."""
    if not OPENWEATHER_API_KEY:
        return {}
    params = {
        "lat": lat, "lon": lon,
        "appid": OPENWEATHER_API_KEY,
        "units": "metric",
        "exclude": "current,minutely,hourly,alerts",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(OPENWEATHER_ONECALL, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.debug("openweather fetch failed: %s", e)
        return {}
    out: Dict[date, float] = {}
    for entry in (data.get("daily") or [])[:8]:
        try:
            ts = int(entry.get("dt"))
            tmax = float(entry["temp"]["max"])
            d = datetime.utcfromtimestamp(ts).date()
            out[d] = tmax
        except (KeyError, TypeError, ValueError):
            continue
    return out


async def _fetch_metar_observed_max_today(
    icao: str, hours: int = 12
) -> Optional[float]:
    """Return the max observed temperature (°C) from METAR over the past
    `hours` hours. Used as a hard floor on today's predicted max."""
    params = {
        "ids": icao, "format": "json", "taf": "false", "hours": hours,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(AVIATION_WX_METAR, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.debug("metar floor fetch failed: %s", e)
        return None
    if not data:
        return None
    temps = []
    for ob in data:
        t = ob.get("temp")
        if isinstance(t, (int, float)):
            temps.append(float(t))
    return max(temps) if temps else None


def _integer_probs(mean: float, std: float, n_each_side: int = 3) -> Dict[int, float]:
    if std <= 0.05:
        return {int(round(mean)): 1.0}
    probs: Dict[int, float] = {}
    center = int(round(mean))
    for offset in range(-n_each_side, n_each_side + 1):
        T = center + offset
        z_lo = (T - 0.5 - mean) / std
        z_hi = (T + 0.5 - mean) / std
        p = _norm_cdf(z_hi) - _norm_cdf(z_lo)
        if p > 0.005:
            probs[T] = p
    return probs


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2)))


# ─────────────────────────── current observations ──────────────────────────
async def fetch_current_observation(icao: str, lat: float, lon: float) -> Optional[CurrentObs]:
    """Try METAR first (real airport weather station), fall back to Open-Meteo."""
    metar = await _fetch_metar(icao)
    if metar is not None:
        return metar
    return await _fetch_open_meteo_current(lat, lon)


async def _fetch_metar(icao: str) -> Optional[CurrentObs]:
    params = {"ids": icao, "format": "json", "taf": "false", "hours": 2}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(AVIATION_WX_METAR, params=params)
            r.raise_for_status()
            data = r.json()
        if not data:
            return None
        m = data[0]
        temp_c = m.get("temp")
        temp_f = (temp_c * 9 / 5 + 32) if isinstance(temp_c, (int, float)) else None
        return CurrentObs(
            source="METAR",
            temp_c=temp_c,
            temp_f=temp_f,
            wind_kt=m.get("wspd"),
            wind_dir=m.get("wdir"),
            wx=m.get("wxString"),
            raw=m.get("rawOb"),
            observed_at=m.get("reportTime"),
        )
    except Exception:
        return None


async def _fetch_open_meteo_current(lat: float, lon: float) -> Optional[CurrentObs]:
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "kn",
        "temperature_unit": "celsius",
        "timezone": "auto",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(OPEN_METEO_FORECAST, params=params)
            r.raise_for_status()
            data = r.json()
        c = data.get("current") or {}
        temp_c = c.get("temperature_2m")
        temp_f = (temp_c * 9 / 5 + 32) if isinstance(temp_c, (int, float)) else None
        return CurrentObs(
            source="Open-Meteo",
            temp_c=temp_c,
            temp_f=temp_f,
            wind_kt=c.get("wind_speed_10m"),
            wind_dir=c.get("wind_direction_10m"),
            wx=None,
            raw=None,
            observed_at=c.get("time"),
        )
    except Exception:
        return None


# ─────────────────────────── position-miss probability ────────────────────
@dataclass
class PositionRisk:
    """How likely a Polymarket position is to miss its target bucket today.

    This is a real-time estimate that fuses:
      - METAR-observed max so far today (hard floor on outcome)
      - HRRR (or regional equivalent) hourly projection for remaining hours
      - Global ensemble's predicted day-max as a sanity check
    """
    p_miss: float                # 0..1 — probability the actual high lies outside the bucket
    observed_max_c: Optional[float]
    projected_max_c: float       # blended estimate of today's actual max
    sigma_c: float               # uncertainty
    metar_temp_c: Optional[float]   # current METAR temp
    metar_trend: Optional[str]      # 'rising' | 'falling' | 'steady'
    hrrr_max_c: Optional[float]     # HRRR's projected remaining-day max
    bucket_low_c: float
    bucket_high_c: float


async def compute_position_risk(
    lat: float, lon: float, icao: str,
    bucket_low_c: float, bucket_high_c: float,
    target_date: date,
) -> Optional[PositionRisk]:
    """Compute live P(miss) for a position. Returns None if data is too stale
    or the date isn't today (we only do live signals for the current day)."""
    today_local = datetime.now().astimezone().date()
    # We don't have city's timezone here, but the caller filters to today
    # already; date alignment is done upstream.

    # 1) METAR — past 24 hours, find max-so-far and current temp + trend
    metar_max, metar_now, metar_trend = await _metar_max_and_trend(icao)

    # 2) HRRR / regional — hourly temps for the rest of today
    regional = _select_regional_model(lat, lon)
    hrrr_max = await _hrrr_remaining_max(lat, lon, regional, target_date)

    # 3) Global ensemble — daily max for today as a sanity check
    forecasts = await fetch_ensemble_forecast(lat, lon, days=1, icao=icao)
    ensemble_max = forecasts[0].ensemble_mean_c if forecasts else None
    ensemble_sigma = forecasts[0].std_c if forecasts else 1.5

    # Build the projected max estimate.
    components = []
    if metar_max is not None:
        components.append(("metar_floor", metar_max, 0.0))  # hard floor, no uncertainty
    if hrrr_max is not None:
        components.append(("hrrr", hrrr_max, 0.6))
    if ensemble_max is not None:
        components.append(("ensemble", ensemble_max, ensemble_sigma))

    if not components:
        return None

    # The projected max is the larger of (observed-so-far) and (max of remaining-day forecast).
    # Specifically: floor by metar_max, then weighted-blend the forecast components.
    floor = metar_max if metar_max is not None else -999
    forecast_components = [(name, v, s) for name, v, s in components if name != "metar_floor"]
    if forecast_components:
        # Weighted blend favoring HRRR when it's available
        w_hrrr = 0.7 if hrrr_max is not None else 0.0
        w_ens = 1.0 - w_hrrr
        if hrrr_max is not None and ensemble_max is not None:
            blended = w_hrrr * hrrr_max + w_ens * ensemble_max
            blended_sigma = max(0.5, ensemble_sigma * 0.6)  # HRRR shrinks σ
        elif hrrr_max is not None:
            blended = hrrr_max
            blended_sigma = 0.7
        else:
            blended = ensemble_max
            blended_sigma = ensemble_sigma
    else:
        # Only metar — past tense, day high already happened
        blended = floor
        blended_sigma = 0.5

    projected_max = max(floor, blended)

    # Compute P(actual max < bucket_low) + P(actual max > bucket_high), under
    # a Gaussian centered on projected_max. But: if metar_max is already inside
    # or above the bucket, certain branches are zero/one.
    p_below = 0.0
    p_above = 0.0
    if metar_max is not None and metar_max > bucket_high_c:
        # Already exceeded — definite miss above.
        p_above = 1.0
    elif metar_max is not None and metar_max >= bucket_low_c:
        # Already inside or above the floor of bucket — can only go up from here.
        # P(below) is 0; P(above) is P(future_max > bucket_high).
        p_above = _prob_max_exceeds(projected_max, blended_sigma, bucket_high_c)
    else:
        # Day still open below the bucket. Could miss low or high.
        p_below = _prob_max_below(projected_max, blended_sigma, bucket_low_c)
        p_above = _prob_max_exceeds(projected_max, blended_sigma, bucket_high_c)

    p_miss = max(0.0, min(1.0, p_below + p_above))

    return PositionRisk(
        p_miss=p_miss,
        observed_max_c=metar_max,
        projected_max_c=projected_max,
        sigma_c=blended_sigma,
        metar_temp_c=metar_now,
        metar_trend=metar_trend,
        hrrr_max_c=hrrr_max,
        bucket_low_c=bucket_low_c,
        bucket_high_c=bucket_high_c,
    )


def _prob_max_below(mu: float, sigma: float, threshold: float) -> float:
    """P(actual < threshold) under N(mu, sigma)."""
    if sigma <= 0.01:
        return 1.0 if mu < threshold else 0.0
    z = (threshold - mu) / sigma
    return _norm_cdf(z)


def _prob_max_exceeds(mu: float, sigma: float, threshold: float) -> float:
    return 1.0 - _prob_max_below(mu, sigma, threshold + 1.0)
    # +1 because the bucket is closed at the top: bucket "62-63°F" includes
    # values up to 63.99°F. Using threshold+1 gives us P(max >= threshold+1).


async def _metar_max_and_trend(
    icao: str, hours: int = 12
) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    """Returns (max_temp_so_far_c, latest_temp_c, trend)."""
    params = {"ids": icao, "format": "json", "taf": "false", "hours": hours}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(AVIATION_WX_METAR, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception:
        return None, None, None
    if not data:
        return None, None, None
    # METAR returns reverse-chronological. Get observations with valid temps.
    obs = []
    for ob in data:
        t = ob.get("temp")
        rt = ob.get("reportTime")
        if isinstance(t, (int, float)):
            obs.append((rt, float(t)))
    if not obs:
        return None, None, None
    temps = [t for _, t in obs]
    latest = temps[0]
    max_temp = max(temps)
    # Trend: compare latest 30-min mean to previous 30-min mean
    if len(temps) >= 4:
        recent = sum(temps[:2]) / 2
        prior = sum(temps[2:4]) / 2
        if recent > prior + 0.3:
            trend = "rising"
        elif recent < prior - 0.3:
            trend = "falling"
        else:
            trend = "steady"
    else:
        trend = None
    return max_temp, latest, trend


async def _hrrr_remaining_max(
    lat: float, lon: float, regional: Optional[str], target_date: date,
) -> Optional[float]:
    """HRRR (or regional) projected max for remaining hours of target_date."""
    if not regional:
        return None
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "temperature_2m",
        "temperature_unit": "celsius",
        "timezone": "auto",
        "forecast_days": 2,
        "models": regional,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(OPEN_METEO_FORECAST, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception:
        return None
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    temp_key = f"temperature_2m_{regional}"
    temps = hourly.get(temp_key) or hourly.get("temperature_2m")
    if not times or not temps:
        return None
    target_iso = target_date.isoformat()
    now_iso = datetime.now().astimezone().isoformat(timespec="hours")[:13]
    remaining = []
    for t_str, t_val in zip(times, temps):
        if not t_str.startswith(target_iso):
            continue
        if t_str < now_iso:
            continue
        if t_val is not None:
            try:
                remaining.append(float(t_val))
            except (TypeError, ValueError):
                continue
    return max(remaining) if remaining else None
