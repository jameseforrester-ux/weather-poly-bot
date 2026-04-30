"""
WeatherPolyBot — Configuration
All tuneable constants live here.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ──────────────────────────────────────────────
TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

# ── Open-Meteo endpoints (all free, no key needed) ────────
OM_FORECAST_URL        = "https://api.open-meteo.com/v1/forecast"
OM_ARCHIVE_URL         = "https://archive-api.open-meteo.com/v1/archive"
OM_HIST_FORECAST_URL   = "https://historical-forecast-api.open-meteo.com/v1/forecast"
OM_GEOCODE_URL         = "https://geocoding-api.open-meteo.com/api/v1/search"

# ── Weather models used in the ensemble ───────────────────
# Selection rationale (backed by ECMWF verification stats,
# WMO assessments, and peer-reviewed evaluation studies):
#
#  ECMWF IFS  — Consistently ranked #1 global NWP model by the
#               WMO Lead Centre for Deterministic NWP Verification
#               (2015-2024).  RMSE for 500hPa at day-2 ~5% better
#               than GFS.  ~92% accuracy within 2°C for max-temp.
#
#  NOAA GFS   — Gold-standard US model, global coverage, updated
#               4× daily.  After 2022 upgrades (FV3 core) reaches
#               ~88% accuracy within 2°C.  Excels for N-America.
#
#  DWD ICON   — German Weather Service global model.  Independent
#               numerical core (icosahedral grid) provides genuine
#               diversity.  WMO rankings put it 3rd globally
#               (~89% accuracy within 2°C).  Strong in Europe &
#               mid-latitudes.
#
# Initial weights are derived from published MAE stats; the bot
# recalculates them automatically after every backtest run.
# ─────────────────────────────────────────────────────────
MODELS: dict[str, str] = {
    "ECMWF IFS":  "ecmwf_ifs025",
    "NOAA GFS":   "gfs_global",
    "DWD ICON":   "icon_global",
}

DEFAULT_WEIGHTS: dict[str, float] = {
    "ECMWF IFS":  0.40,
    "NOAA GFS":   0.30,
    "DWD ICON":   0.30,
}

# ── Polymarket endpoints ───────────────────────────────────
PM_GAMMA_URL  = "https://gamma-api.polymarket.com"
PM_MARKET_URL = "https://polymarket.com/event/{slug}"

# ── Edge threshold (dollars per share) ────────────────────
EDGE_THRESHOLD: float = 0.40   # flag if our edge ≥ 40 ¢/share

# ── Prediction uncertainty (σ used in prob estimate) ──────
PRED_UNCERTAINTY_C: float = 1.5   # °C standard deviation

# ── Database ──────────────────────────────────────────────
DB_PATH: str = os.getenv("DB_PATH", "weatherpoly.db")

# ── Backtest locations (airports + random cities) ─────────
BACKTEST_LOCATIONS: list[dict] = [
    {"name": "JFK Airport, New York",    "lat": 40.6413,  "lon": -73.7781},
    {"name": "LAX, Los Angeles",         "lat": 33.9425,  "lon": -118.4081},
    {"name": "London Heathrow (LHR)",    "lat": 51.4700,  "lon": -0.4543},
    {"name": "Tokyo Haneda (HND)",       "lat": 35.5494,  "lon": 139.7798},
    {"name": "Sydney Airport (SYD)",     "lat": -33.9461, "lon": 151.1772},
    {"name": "Dubai Airport (DXB)",      "lat": 25.2532,  "lon": 55.3657},
    {"name": "Chicago O'Hare (ORD)",     "lat": 41.9742,  "lon": -87.9073},
    {"name": "Paris CDG Airport",        "lat": 49.0097,  "lon": 2.5479},
    {"name": "Toronto Pearson (YYZ)",    "lat": 43.6777,  "lon": -79.6248},
    {"name": "Miami International",      "lat": 25.7959,  "lon": -80.2870},
    {"name": "Denver International",     "lat": 39.8561,  "lon": -104.6737},
    {"name": "São Paulo Guarulhos (GRU)","lat": -23.4356, "lon": -46.4731},
]

BACKTEST_DAYS: int = 30   # days of history to evaluate

# ── Misc ──────────────────────────────────────────────────
HTTP_TIMEOUT: int   = 30
TRACKED_REFRESH_MIN = 60   # minutes between auto-refresh for tracked locs
