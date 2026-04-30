"""
backtest_service.py
───────────────────
Evaluates ECMWF IFS, NOAA GFS, and DWD ICON against 30 days of
observed daily-high temperatures across 12 diverse locations
(airports + major cities on every continent).

Methodology
───────────
1. For each location and each model, pull the "historical forecast"
   from Open-Meteo's hindcast API — this returns what the model
   *actually predicted* at initialisation time for each day.
2. Pull observed temperatures from the Open-Meteo archive API.
3. Compute per-model MAE, RMSE, bias, and accuracy within ±1 °C / ±2 °C.
4. Derive ensemble weights as the softmax-inverse of MAE so that more
   accurate models receive proportionally higher weights.
5. Persist results to the database and return a summary dict.
"""

import asyncio
import math
from datetime import date, timedelta
from typing import Optional

import database as db
from config import BACKTEST_LOCATIONS, BACKTEST_DAYS, MODELS
from weather_service import fetch_historical_forecast, fetch_actual_temps


# ─────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────
def _mae(errors: list[float]) -> float:
    return sum(abs(e) for e in errors) / len(errors) if errors else float("inf")


def _rmse(errors: list[float]) -> float:
    return math.sqrt(sum(e ** 2 for e in errors) / len(errors)) if errors else float("inf")


def _bias(errors: list[float]) -> float:
    return sum(errors) / len(errors) if errors else 0.0


def _acc_within(errors: list[float], thr: float) -> float:
    """Fraction of predictions within ±thr °C of actual."""
    if not errors:
        return 0.0
    return sum(1 for e in errors if abs(e) <= thr) / len(errors) * 100


def _softmax_inverse_weights(maes: dict[str, float]) -> dict[str, float]:
    """
    Models with lower MAE get higher weight.
    w_i = exp(-mae_i) / Σ exp(-mae_j)
    Clamped to avoid extreme values.
    """
    vals = {m: math.exp(-v) for m, v in maes.items()}
    total = sum(vals.values())
    return {m: round(v / total, 4) for m, v in vals.items()}


# ─────────────────────────────────────────────────────────
#  Per-location, per-model evaluation
# ─────────────────────────────────────────────────────────
async def _evaluate_model(lat: float, lon: float, model_name: str,
                           model_code: str,
                           start: date, end: date) -> Optional[dict]:
    """
    Returns {"errors": [...], "mae": float, "rmse": float, ...} or None.
    """
    forecast_raw = await fetch_historical_forecast(lat, lon, model_code, start, end)
    actual_raw   = await fetch_actual_temps(lat, lon, start, end)

    if not forecast_raw or not actual_raw:
        return None

    f_dates = forecast_raw.get("daily", {}).get("time", [])
    f_vals  = forecast_raw.get("daily", {}).get("temperature_2m_max", [])
    a_dates = actual_raw.get("daily", {}).get("time", [])
    a_vals  = actual_raw.get("daily", {}).get("temperature_2m_max", [])

    # Build date → value maps
    f_map = {d: v for d, v in zip(f_dates, f_vals) if v is not None}
    a_map = {d: v for d, v in zip(a_dates, a_vals) if v is not None}

    common = sorted(set(f_map) & set(a_map))
    if not common:
        return None

    errors = [f_map[d] - a_map[d] for d in common]  # positive = over-predicted

    return {
        "model":    model_name,
        "n":        len(errors),
        "errors":   errors,
        "mae":      _mae(errors),
        "rmse":     _rmse(errors),
        "bias":     _bias(errors),
        "acc_1c":   _acc_within(errors, 1.0),
        "acc_2c":   _acc_within(errors, 2.0),
    }


# ─────────────────────────────────────────────────────────
#  Full backtest run
# ─────────────────────────────────────────────────────────
async def run_backtest(progress_cb=None) -> dict:
    """
    Run the full 30-day backtest across all locations and models.
    Returns {
        "by_model": {"ECMWF IFS": {"mae": .., "rmse": .., ...}, ...},
        "weights":  {"ECMWF IFS": 0.42, ...},
        "locations": [...],
        "n_total":  int,
    }
    Optional progress_cb(message: str) is called to report progress.
    """
    end   = date.today() - timedelta(days=2)   # archive has ~2-day lag
    start = end - timedelta(days=BACKTEST_DAYS - 1)

    all_rows: list[dict] = []
    model_errors: dict[str, list[float]] = {m: [] for m in MODELS}
    location_summaries: list[dict] = []

    total = len(BACKTEST_LOCATIONS) * len(MODELS)
    done  = 0

    for loc in BACKTEST_LOCATIONS:
        loc_name = loc["name"]
        loc_results = {}
        for model_name, model_code in MODELS.items():
            res = await _evaluate_model(
                loc["lat"], loc["lon"], model_name, model_code, start, end
            )
            done += 1
            if progress_cb:
                pct = int(done / total * 100)
                await progress_cb(
                    f"⏳ Backtesting… {pct}%  |  {loc_name}  |  {model_name}"
                )
            if res:
                model_errors[model_name].extend(res["errors"])
                loc_results[model_name] = res
                all_rows.append({
                    "location": loc_name,
                    "model":    model_name,
                    "mae":      round(res["mae"],   2),
                    "rmse":     round(res["rmse"],  2),
                    "bias":     round(res["bias"],  2),
                    "acc_1c":   round(res["acc_1c"], 1),
                    "acc_2c":   round(res["acc_2c"], 1),
                    "n_samples":res["n"],
                })
        location_summaries.append({
            "location": loc_name,
            "results":  loc_results,
        })

    # Aggregate per model
    by_model = {}
    global_maes: dict[str, float] = {}
    for model_name in MODELS:
        errs = model_errors[model_name]
        if errs:
            mae  = _mae(errs)
            rmse = _rmse(errs)
            bias = _bias(errs)
            by_model[model_name] = {
                "mae":    round(mae,  2),
                "rmse":   round(rmse, 2),
                "bias":   round(bias, 2),
                "acc_1c": round(_acc_within(errs, 1.0), 1),
                "acc_2c": round(_acc_within(errs, 2.0), 1),
                "n":      len(errs),
            }
            global_maes[model_name] = mae

    # Derive weights
    weights = _softmax_inverse_weights(global_maes) if global_maes else {}

    # Persist
    if all_rows:
        await db.save_backtest_rows(all_rows)
    if weights:
        await db.save_weights(weights)

    return {
        "by_model":  by_model,
        "weights":   weights,
        "locations": location_summaries,
        "n_total":   sum(len(e) for e in model_errors.values()),
        "period":    f"{start.isoformat()} → {end.isoformat()}",
    }


# ─────────────────────────────────────────────────────────
#  Format summary for Telegram
# ─────────────────────────────────────────────────────────
def format_backtest_summary(bt: dict) -> str:
    by_model = bt.get("by_model", {})
    weights  = bt.get("weights", {})
    n_total  = bt.get("n_total", 0)
    period   = bt.get("period", "")

    medal = ["🥇", "🥈", "🥉"]
    ranked = sorted(by_model.items(), key=lambda x: x[1]["mae"])

    lines = [
        "📊 <b>BACKTEST RESULTS</b>",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📅 Period: <code>{period}</code>",
        f"📍 Locations: {len(BACKTEST_LOCATIONS)} (airports + cities)",
        f"🔢 Data points: {n_total}",
        "",
        "🏆 <b>MODEL ACCURACY RANKING</b>",
        "<code>",
        f"{'Model':<14} {'MAE':>5} {'RMSE':>5} {'Bias':>5} {'±1°C':>5} {'±2°C':>5}",
        f"{'─'*14} {'─'*5} {'─'*5} {'─'*5} {'─'*5} {'─'*5}",
    ]
    for i, (model, stats) in enumerate(ranked):
        m_str = medal[i] if i < 3 else "  "
        lines.append(
            f"{m_str}{model:<12} {stats['mae']:>5.2f} {stats['rmse']:>5.2f} "
            f"{stats['bias']:>+5.2f} {stats['acc_1c']:>4.0f}% {stats['acc_2c']:>4.0f}%"
        )
    lines.append("</code>")

    lines += [
        "",
        "⚖️ <b>DERIVED ENSEMBLE WEIGHTS</b>",
        "<code>",
    ]
    for model, wt in sorted(weights.items(), key=lambda x: -x[1]):
        bar = "█" * int(wt * 20)
        lines.append(f"{model:<12} {wt*100:>5.1f}%  {bar}")
    lines += ["</code>", "",
              "ℹ️ Weights = softmax-inverse of global MAE across all locations"]

    return "\n".join(lines)
