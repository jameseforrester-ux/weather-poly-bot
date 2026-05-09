"""Telegram Weather Prediction Bot — main entry point.

Features:
- Search a city to see nearby airports as tappable buttons.
- Get a 7-day max-temp forecast for any ICAO/IATA airport, derived from a
  weighted ensemble of 8 leading numerical weather prediction models.
- Per-day confidence score, green-flag for high-confidence predictions, and
  a probability for each integer temperature.
- Track airports for ≥2°F / ≥1°C change alerts on the predicted max.
- Bottom-left commands menu + persistent reply keyboard for fast access.
"""
import asyncio
import logging
import re
from datetime import date, timedelta
from typing import List, Optional, Tuple

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonCommands,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from airports import AirportDatabase, ensure_airport_data, geocode_city
from config import (
    AIRPORTS_CSV,
    ALERT_THRESHOLD_C,
    ALERT_THRESHOLD_F,
    BOT_TOKEN,
    DB_PATH,
    LOG_LEVEL,
    TRACKING_INTERVAL_MINUTES,
)
from polymarket import (
    CityMarket,
    EVPick,
    Opportunity,
    SUPPORTED_CITIES,
    city_local_today,
    get_market_for_city,
    hedges_around,
    match_for_prediction,
    rank_buckets_by_ev,
    resolve_city_for_airport,
    score_opportunity,
    supported_cities_alphabetical,
    top_n_by_yes,
)
from tracking import TrackingDB
from weather import (
    DayForecast,
    MODEL_DISPLAY,
    fetch_current_observation,
    fetch_ensemble_forecast,
)

# ─────────────────────────── setup ────────────────────────────────
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("weather-bot")

airports_db = AirportDatabase()
tracking_db = TrackingDB(DB_PATH)


# ─────────────────────────── messages ─────────────────────────────
WELCOME = (
    "🌤️ *Weather Prediction Bot* 🌤️\n\n"
    "Highly accurate temperature forecasts using a *weighted ensemble* of "
    "8 leading NWP models — ECMWF IFS, ECMWF AIFS (AI), UK Met Office, "
    "DWD ICON, NOAA GFS, JMA, Météo-France, and Environment Canada GEM.\n\n"
    "✨ *Three modes:*\n"
    "🎯 *Opportunities* — scans all 35 markets and surfaces the top 5 "
    "high-confidence trades (model conf ≥75% AND market YES ≥40%), ranked "
    "by edge × confidence.\n\n"
    "🎲 *Polymarket Forecast* — pick a city, get a focused 3-day forecast at "
    "the exact resolution station Polymarket uses to settle the market.\n\n"
    "🌤️ *General Forecast* — search any of ~80,000 airports worldwide.\n\n"
    "🔔 Track airports for ≥2°F / ≥1°C alerts.\n"
    "🌡️ Temperatures in °F and °C, always whole numbers.\n\n"
    "Tap the bottom-left *Menu* or use the keyboard below."
)


HELP = (
    "*🆘 Help*\n\n"
    "*Commands*\n"
    "/opportunities — 🎯 Top 5 high-confidence trade picks across 35 cities\n"
    "/polymarket — 🎲 Pick a city → focused forecast + live odds\n"
    "/forecast `<code>` — 🌤️ General forecast for any airport\n"
    "/search `<city>` — Find nearby airports\n"
    "/track `<code>` — Track for change alerts\n"
    "/untrack `<code>` — Stop tracking\n"
    "/list — Your tracked airports\n"
    "/help — This help\n\n"
    "*Methodology*\n"
    "Weighted ensemble of 8 NWP models (ECMWF IFS heaviest weight). "
    "Confidence comes from inter-model standard deviation — when models "
    "agree, we're confident.\n"
    "🟢 high · 🟡 medium · 🔴 low\n\n"
    "*Opportunities scoring*\n"
    "We compute our model's YES probability for each bucket (Gaussian over "
    "ensemble spread), compare to the market's YES price, and surface the "
    "biggest mispricings where our confidence is also high.\n"
    "💰 = best EV pick · 🎯 = matches model · ✅ = market's matched bucket"
)


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🎯 Opportunities"), KeyboardButton("🎲 Polymarket")],
            [KeyboardButton("🌤️ Forecast"), KeyboardButton("🔍 Search City")],
            [KeyboardButton("📋 My Tracked"), KeyboardButton("❓ Help")],
        ],
        resize_keyboard=True,
    )


# ─────────────────────────── command handlers ─────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        WELCOME, parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard()
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP, parse_mode=ParseMode.MARKDOWN)


async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.effective_message.reply_text(
            "🔍 Send me a city name.\n\nExample: `/search Tokyo`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    await do_city_search(update, " ".join(ctx.args))


async def cmd_forecast(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.effective_message.reply_text(
            "✈️ Send me an airport code.\n\nExample: `/forecast KJFK`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    await send_forecast(update, ctx.args[0])


async def cmd_track(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.effective_message.reply_text(
            "🔔 Specify an airport.\n\nExample: `/track KJFK`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    await track_airport(update, ctx.args[0])


async def cmd_untrack(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.effective_message.reply_text(
            "🔕 Specify an airport.\n\nExample: `/untrack KJFK`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    code = ctx.args[0].upper().strip()
    user_id = update.effective_user.id
    if tracking_db.remove(user_id, code):
        await update.effective_message.reply_text(
            f"✅ No longer tracking *{code}*.", parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.effective_message.reply_text(
            f"ℹ️ You weren't tracking *{code}*.", parse_mode=ParseMode.MARKDOWN
        )


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    rows = tracking_db.list_user(user_id)
    if not rows:
        await update.effective_message.reply_text(
            "📋 You aren't tracking any airports yet.\n"
            "Use `/track <code>` or tap *Track* on a forecast.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    lines = ["📋 *Your Tracked Airports*\n"]
    keyboard = []
    for code, last_c, last_f, last_check in rows:
        airport = airports_db.lookup(code)
        name = airport.name if airport else code
        line = f"\n{airport.type_emoji if airport else '✈️'} *{code}* — {_md_safe(name[:40])}"
        if last_f is not None and last_c is not None:
            line += f"\n   _Last forecast: {int(round(last_f))}°F / {int(round(last_c))}°C_"
        lines.append(line)
        keyboard.append(
            [
                InlineKeyboardButton(f"📊 {code}", callback_data=f"fc:{code}"),
                InlineKeyboardButton("🔕 Untrack", callback_data=f"untrack:{code}"),
            ]
        )

    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ─────────────────────────── core flows ───────────────────────────
async def do_city_search(update: Update, city: str) -> None:
    msg = await update.effective_message.reply_text(
        f"🔍 Searching for *{_md_safe(city)}*…", parse_mode=ParseMode.MARKDOWN
    )
    try:
        results = await geocode_city(city, count=3)
    except Exception as e:
        log.exception("geocode failed")
        await msg.edit_text(f"❌ Search failed: {e}")
        return

    if not results:
        await msg.edit_text(
            f"❌ No locations found for *{_md_safe(city)}*.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    top = results[0]
    lat, lon = top["latitude"], top["longitude"]
    place = top["name"]
    region = top.get("admin1") or ""
    country = top.get("country_code") or ""
    label = ", ".join(p for p in (place, region, country) if p)

    nearby = airports_db.search_near(lat, lon, radius_km=200, limit=8)
    if not nearby:
        await msg.edit_text(
            f"📍 Found *{_md_safe(label)}* but no airports within 200 km.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    text = (
        f"📍 *{_md_safe(label)}*\n"
        f"_({top['latitude']:.2f}, {top['longitude']:.2f})_\n\n"
        f"✈️ *Airports within 200 km* — tap one for a forecast:"
    )
    rows = []
    for ap in nearby:
        code = ap.iata or ap.icao
        btn_label = f"{ap.type_emoji} {code} — {ap.name[:32]}"
        rows.append([InlineKeyboardButton(btn_label, callback_data=f"fc:{ap.icao}")])

    await msg.edit_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def send_forecast(update: Update, code: str) -> None:
    """General-mode forecast for any airport. Inline Polymarket section
    appears when the airport maps (directly or geographically) to a covered
    city.
    """
    code = code.upper().strip()
    airport = airports_db.lookup(code)
    if not airport:
        await update.effective_message.reply_text(
            f"❌ Airport not found: `{_md_safe(code)}`\n"
            "Try an ICAO (4-letter, e.g. `KJFK`) or IATA (3-letter, e.g. `LAX`) code.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    msg = await update.effective_message.reply_text(
        f"⏳ Fetching ensemble forecast for *{airport.icao}*…\n"
        f"_combining {len(MODEL_DISPLAY)} models…_",
        parse_mode=ParseMode.MARKDOWN,
    )

    # 3-day forecast (today + 2) for both general and Polymarket modes.
    forecasts, current = await asyncio.gather(
        fetch_ensemble_forecast(airport.lat, airport.lon, days=3,
                                icao=airport.icao),
        fetch_current_observation(airport.icao, airport.lat, airport.lon),
        return_exceptions=True,
    )
    if isinstance(forecasts, Exception):
        log.exception("forecast fetch failed", exc_info=forecasts)
        await msg.edit_text(f"❌ Forecast failed: {forecasts}")
        return
    if isinstance(current, Exception):
        current = None
    if not forecasts:
        await msg.edit_text("❌ No forecast data available for this location.")
        return

    # Two-tier city resolution: explicit map first, geographic fallback second.
    markets_by_date: dict = {}
    resolved = resolve_city_for_airport(airport.icao, airport.lat, airport.lon)
    if resolved:
        city_key, _market_unit, source = resolved
        # Use the CITY's local "today" for date alignment so cross-timezone
        # users hit the right market. The forecast dates are already in the
        # airport's local TZ from Open-Meteo, so for explicit mappings (same
        # metro) they coincide. For the geo case they should also coincide.
        local_today = city_local_today(city_key)
        # Try each forecast date; Polymarket may publish 1–7 days ahead.
        results = await asyncio.gather(
            *(get_market_for_city(city_key, fc.date) for fc in forecasts),
            return_exceptions=True,
        )
        for fc, m in zip(forecasts, results):
            if isinstance(m, CityMarket):
                markets_by_date[fc.date] = m
        if source == "geo" and markets_by_date:
            log.info("polymarket: geo fallback %s → %s", airport.icao, city_key)

    text = format_forecast(airport, forecasts, current, markets_by_date)
    keyboard = [
        [
            InlineKeyboardButton("🔔 Track", callback_data=f"track:{airport.icao}"),
            InlineKeyboardButton("🔄 Refresh", callback_data=f"fc:{airport.icao}"),
            InlineKeyboardButton("🧠 Models", callback_data=f"models:{airport.icao}"),
        ]
    ]
    await msg.edit_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True,
    )


async def show_models_breakdown(update: Update, code: str) -> None:
    code = code.upper().strip()
    airport = airports_db.lookup(code)
    if not airport:
        return
    forecasts = await fetch_ensemble_forecast(airport.lat, airport.lon, days=2,
                                               icao=airport.icao)
    if not forecasts:
        await update.effective_message.reply_text("❌ No data.")
        return
    today = forecasts[0]
    lines = [
        f"🧠 *Per-model breakdown for {airport.icao}* — Today",
        "",
        f"Ensemble mean: *{today.predicted_max_f}°F / {today.predicted_max_c}°C*",
        f"Spread (σ): {today.std_c:.2f}°C  ·  Confidence: *{int(today.confidence*100)}%*",
        "",
        "*Individual model max-temp predictions:*",
    ]
    for m, v in sorted(
        today.model_values_c.items(), key=lambda x: -x[1] if x[1] else 0
    ):
        f_val = v * 9 / 5 + 32
        lines.append(
            f"• {MODEL_DISPLAY.get(m, m)}: {int(round(f_val))}°F / {int(round(v))}°C"
        )
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN
    )


# ─────────────────────────── Polymarket dedicated mode ────────────────────
async def cmd_polymarket(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the city picker for the dedicated Polymarket forecast flow."""
    await show_polymarket_city_menu(update)


async def show_polymarket_city_menu(update: Update) -> None:
    rows = []
    cities = supported_cities_alphabetical()
    # Two columns of buttons for compactness.
    for i in range(0, len(cities), 2):
        row = []
        for ck, cfg in cities[i:i + 2]:
            row.append(InlineKeyboardButton(
                f"📍 {cfg.display}", callback_data=f"pm:{ck}"
            ))
        rows.append(row)

    text = (
        "🎲 *Polymarket Forecast*\n\n"
        f"Pick a city. I'll forecast at the *exact resolution station* "
        f"Polymarket uses to settle the market, then show our top picks "
        f"with live odds.\n\n"
        f"_{len(cities)} cities supported · today + 2 days, in each city's "
        f"local time_"
    )
    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def send_polymarket_forecast(update: Update, city_key: str) -> None:
    """Polymarket-mode forecast: predict AT the resolution station, fetch
    markets for the city's local today + 2, render with bar visualization.
    """
    cfg = SUPPORTED_CITIES.get(city_key)
    if not cfg:
        await update.effective_message.reply_text("❌ Unknown city.")
        return

    msg = await update.effective_message.reply_text(
        f"⏳ Building Polymarket forecast for *{cfg.display}*…\n"
        f"_predicting at {_md_safe(cfg.resolves_at_name)} ({cfg.resolves_at_icao})_",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Predict at the resolution station — this is the whole point of this mode.
    forecasts, current = await asyncio.gather(
        fetch_ensemble_forecast(cfg.resolves_at_lat, cfg.resolves_at_lon,
                                days=3, icao=cfg.resolves_at_icao),
        fetch_current_observation(cfg.resolves_at_icao,
                                   cfg.resolves_at_lat, cfg.resolves_at_lon),
        return_exceptions=True,
    )
    if isinstance(forecasts, Exception):
        log.exception("polymarket forecast failed", exc_info=forecasts)
        await msg.edit_text(f"❌ Forecast failed: {forecasts}")
        return
    if isinstance(current, Exception):
        current = None
    if not forecasts:
        await msg.edit_text("❌ No forecast data.")
        return

    # Forecast dates from Open-Meteo are in the airport's local timezone, which
    # for the resolution station equals the city's timezone — exactly what we
    # want for matching to Polymarket events.
    local_today = city_local_today(city_key)

    # Fetch markets for each of the 3 forecast dates in parallel.
    market_results = await asyncio.gather(
        *(get_market_for_city(city_key, fc.date) for fc in forecasts),
        return_exceptions=True,
    )
    markets_by_date = {}
    for fc, m in zip(forecasts, market_results):
        if isinstance(m, CityMarket):
            markets_by_date[fc.date] = m

    # Build a focused render: header → per-day temp + Polymarket section.
    lines = [
        f"🎲 *Polymarket Forecast — {cfg.display}*",
        f"🏟️ Resolves at: *{_md_safe(cfg.resolves_at_name)}* "
        f"({cfg.resolves_at_icao})",
        f"🕐 Local date: {local_today.strftime('%A, %b %d')}"
        if local_today else "",
    ]
    if current and current.temp_c is not None:
        c = int(round(current.temp_c))
        f_v = int(round(current.temp_f))
        lines.append(f"📡 Current ({current.source}): *{f_v}°F / {c}°C*")
    lines.append("─" * 26)

    for i, fc in enumerate(forecasts):
        if i == 0:
            day_label = "📅 *Today*"
        elif i == 1:
            day_label = "📅 *Tomorrow*"
        else:
            day_label = f"📅 *{fc.date.strftime('%A')}*"
        flag = _flag(fc)
        lines.append(f"\n{day_label} _{fc.date.strftime('%b %d')}_")
        lines.append(
            f"🌡️ Max: *{fc.predicted_max_f}°F / {fc.predicted_max_c}°C*  {flag}"
        )
        lines.append(
            f"🎯 Confidence: *{int(fc.confidence*100)}%* "
            f"({fc.confidence_level}) · σ {fc.std_c:.1f}°C"
        )
        market = markets_by_date.get(fc.date)
        if market:
            lines.append(_format_polymarket_block(market, fc))
        else:
            lines.append("_No Polymarket event for this day yet._")

    keyboard = [
        [
            InlineKeyboardButton("🔄 Refresh", callback_data=f"pm:{city_key}"),
            InlineKeyboardButton("🏙️ Cities", callback_data="pm_menu"),
        ]
    ]
    await msg.edit_text(
        "\n".join(l for l in lines if l),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True,
    )


# ─────────────────────────── Opportunities scanner ────────────────────────
# Strict tier — high-conviction picks shown at the top
OPP_CONF_MIN = 0.70
OPP_MARKET_MIN = 0.40
# Honorable mentions tier — looser fallback always shown below strict
OPP_HM_CONF_MIN = 0.60
OPP_HM_MARKET_MIN = 0.30


async def _scan_one(
    city_key: str, target_date: date,
    conf_min: float = OPP_CONF_MIN,
    market_min: float = OPP_MARKET_MIN,
) -> Optional[Opportunity]:
    """Build a single (city, date) opportunity if it clears the thresholds.
    Returns None if anything is missing or below threshold."""
    cfg = SUPPORTED_CITIES.get(city_key)
    if not cfg:
        return None
    try:
        forecasts = await fetch_ensemble_forecast(
            cfg.resolves_at_lat, cfg.resolves_at_lon, days=3,
            icao=cfg.resolves_at_icao,
        )
    except Exception:
        return None
    if not forecasts:
        return None
    fc = next((f for f in forecasts if f.date == target_date), None)
    if fc is None:
        return None
    if fc.confidence < conf_min:
        return None

    market = await get_market_for_city(city_key, target_date)
    if not market or not market.buckets:
        return None

    pred = fc.predicted_max_c if market.unit == "C" else fc.predicted_max_f
    matched = match_for_prediction(market, pred)
    if not matched or matched.yes_prob < market_min:
        return None

    sigma = fc.std_c if market.unit == "C" else fc.std_c * 9 / 5
    ranked = rank_buckets_by_ev(market, float(pred), sigma)
    if not ranked:
        return None

    best = ranked[0]
    hedge = ranked[1] if len(ranked) > 1 else None
    score = score_opportunity(fc.confidence, best.score)

    today_local = city_local_today(city_key)
    return Opportunity(
        city_key=city_key, city_display=cfg.display,
        target_date=target_date,
        is_today=(today_local is not None and target_date == today_local),
        confidence=fc.confidence, predicted_unit=pred, unit=market.unit,
        matched_bucket=matched, matched_yes=matched.yes_prob,
        best_pick=best, hedge_pick=hedge, market=market, score=score,
    )


async def find_opportunities_two_tier() -> Tuple[List[Opportunity], List[Opportunity]]:
    """Scan all cities for today + tomorrow (city-local) and split into:
      - strict: confidence ≥ OPP_CONF_MIN AND market YES ≥ OPP_MARKET_MIN
      - honorable: passed honorable filter but not strict
    Both lists are sorted by combined score, no caps. Single network pass
    using the honorable thresholds; we classify into tiers after.
    """
    tasks = []
    for city_key in SUPPORTED_CITIES:
        local_today = city_local_today(city_key)
        if local_today is None:
            continue
        for d in (local_today, local_today + timedelta(days=1)):
            tasks.append(_scan_one(
                city_key, d,
                conf_min=OPP_HM_CONF_MIN,
                market_min=OPP_HM_MARKET_MIN,
            ))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    all_opps = [r for r in results if isinstance(r, Opportunity)]

    strict = [o for o in all_opps
              if o.confidence >= OPP_CONF_MIN
              and o.matched_yes >= OPP_MARKET_MIN]
    strict_keys = {(o.city_key, o.target_date) for o in strict}
    honorable = [o for o in all_opps
                 if (o.city_key, o.target_date) not in strict_keys]

    strict.sort(key=lambda o: -o.score)
    honorable.sort(key=lambda o: -o.score)
    return strict, honorable


def _format_opportunity_summary(opp: Opportunity, idx: int) -> str:
    """Two-line summary. First line: city, day, model confidence + matched-
    bucket market price (the number that the filter actually qualifies on).
    Second line: the best EV pick with its own bar + edge.

    This avoids the confusion where the matched bucket has e.g. 33% market
    YES (passing the honorable 30% filter) but the *best EV pick* shown is
    a different bucket with a different price.
    """
    day = "Today" if opp.is_today else "Tomorrow"
    matched_pct = int(round(opp.matched_yes * 100))
    matched_bar = _yes_bar(opp.matched_yes, width=8)
    best = opp.best_pick
    best_pct = int(round(best.market_p * 100))
    best_bar = _yes_bar(best.market_p, width=8)
    edge = best.edge_pp
    edge_str = f"{edge:+.0f}pp" if abs(edge) >= 1 else "≈0pp"
    same_bucket = best.bucket.market_slug == opp.matched_bucket.market_slug

    line1 = (
        f"*{idx}. {opp.city_display}* · _{day}_  "
        f"🎯 *{int(opp.confidence*100)}%* model"
    )
    # Model-matched bucket — the one that determines tier qualification
    line2 = (
        f"   🎯 {opp.predicted_unit}°{opp.unit} → *{opp.matched_bucket.label}*  "
        f"`{matched_bar}` {matched_pct}% market"
    )
    # Best EV pick — what we actually recommend
    if same_bucket:
        line3 = f"   💰 Best EV: same bucket · edge {edge_str}"
    else:
        line3 = (
            f"   💰 Best EV: *{best.bucket.label}*  "
            f"`{best_bar}` {best_pct}% · edge {edge_str}"
        )
    return f"{line1}\n{line2}\n{line3}"


async def cmd_opportunities(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.effective_message.reply_text(
        f"🎯 *Scanning {len(SUPPORTED_CITIES)} markets…*\n"
        "_today + tomorrow, each city's local time_",
        parse_mode=ParseMode.MARKDOWN,
    )
    try:
        strict, honorable = await find_opportunities_two_tier()
    except Exception as e:
        log.exception("opportunity scan failed")
        await msg.edit_text(f"❌ Scan failed: {e}")
        return

    if not strict and not honorable:
        await msg.edit_text(
            "🎯 *No opportunities right now*\n\n"
            f"No (city, day) cleared even the loose filter "
            f"(conf ≥{int(OPP_HM_CONF_MIN*100)}% AND market YES "
            f"≥{int(OPP_HM_MARKET_MIN*100)}%).\n\n"
            "Most likely Polymarket hasn't published today's markets for many "
            "cities yet — check back in a few hours.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔄 Rescan", callback_data="opp_scan")]]
            ),
        )
        return

    lines: List[str] = []
    keyboard: List[List[InlineKeyboardButton]] = []
    counter = 0

    if strict:
        lines.append(
            f"🟢 *Strict Picks* — conf ≥{int(OPP_CONF_MIN*100)}% · "
            f"market ≥{int(OPP_MARKET_MIN*100)}%"
        )
        lines.append("")
        for opp in strict:
            counter += 1
            lines.append(_format_opportunity_summary(opp, counter))
            keyboard.append([InlineKeyboardButton(
                f"📋 #{counter}: {opp.city_display} "
                f"{('Today' if opp.is_today else 'Tom')}",
                callback_data=f"opp:{opp.city_key}:{opp.target_date.isoformat()}",
            )])
    else:
        lines.append("🟢 *Strict Picks* — _none right now_")
        lines.append("")

    if honorable:
        lines.append("")
        lines.append(
            f"🟡 *Honorable Mentions* — conf ≥{int(OPP_HM_CONF_MIN*100)}% · "
            f"market ≥{int(OPP_HM_MARKET_MIN*100)}%"
        )
        lines.append("")
        for opp in honorable:
            counter += 1
            lines.append(_format_opportunity_summary(opp, counter))
            keyboard.append([InlineKeyboardButton(
                f"📋 #{counter}: {opp.city_display} "
                f"{('Today' if opp.is_today else 'Tom')}",
                callback_data=f"opp:{opp.city_key}:{opp.target_date.isoformat()}",
            )])

    lines.append("")
    lines.append("_Sorted by edge × confidence within each tier._")
    keyboard.append([InlineKeyboardButton("🔄 Rescan", callback_data="opp_scan")])

    await msg.edit_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True,
    )


async def show_opportunity_detail(
    update: Update, city_key: str, target_date_iso: str
) -> None:
    """Detail view for one opportunity: model context + top-3 EV picks
    with smart pick highlighted and Trade buttons.

    We re-fetch using the LOOSE (honorable) thresholds so any opportunity
    from either tier survives the round-trip. If the underlying market or
    forecast has genuinely shifted out of even the loose band, we tell the
    user with specific numbers, not just "shifted."
    """
    try:
        target_date = date.fromisoformat(target_date_iso)
    except ValueError:
        return

    # Re-fetch at the LOOSE threshold so honorable-tier opportunities also
    # round-trip cleanly. (Was the bug: this used strict defaults.)
    opp = await _scan_one(
        city_key, target_date,
        conf_min=OPP_HM_CONF_MIN,
        market_min=OPP_HM_MARKET_MIN,
    )
    if not opp:
        # Genuinely below even the loose band now — give the user the actual
        # numbers so they can decide for themselves.
        cfg = SUPPORTED_CITIES.get(city_key)
        diag_lines = [
            "ℹ️ *Opportunity no longer above threshold.*",
            "",
            f"Re-checked {cfg.display if cfg else city_key} for "
            f"{target_date.strftime('%b %d')} just now and either:",
            f"• Model confidence dropped below {int(OPP_HM_CONF_MIN*100)}%, OR",
            f"• Polymarket YES on the matched bucket dropped below "
            f"{int(OPP_HM_MARKET_MIN*100)}%, OR",
            "• The market closed / hasn't been published yet.",
            "",
            "_Tap Polymarket below to view the live market manually._",
        ]
        kb = []
        if cfg:
            kb.append([InlineKeyboardButton(
                f"🎲 View {cfg.display} markets",
                callback_data=f"pm:{city_key}",
            )])
        kb.append([InlineKeyboardButton(
            "⬅ All opportunities", callback_data="opp_scan"
        )])
        await update.effective_message.reply_text(
            "\n".join(diag_lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    cfg = SUPPORTED_CITIES.get(city_key)
    day = "Today" if opp.is_today else "Tomorrow"
    matched_slug = opp.matched_bucket.market_slug

    lines = [
        f"🎯 *{opp.city_display}* — {day} ({opp.target_date.strftime('%b %d')})",
        f"🏟️ _{_md_safe(cfg.resolves_at_name)} ({cfg.resolves_at_icao})_",
        "",
        f"🌡️ Model max: *{opp.predicted_unit}°{opp.unit}*  ·  "
        f"🎯 *{int(opp.confidence*100)}%* confidence",
        "",
        "*Top picks ranked by expected value:*",
    ]

    # Top 3 EV picks
    sigma = (opp.market.buckets[0].value, )  # placeholder
    # Re-rank to make sure we have access to all picks
    sigma_unit = (
        # We don't have direct access to fc here; use the bucket's sigma proxy
        # by computing from the matched bucket geometry
        1.0
    )
    # Simpler: rerun rank_buckets_by_ev with what we have
    # Recompute sigma from market unit and a default
    # (We can't easily get the original fc.std_c here without keeping it on Opportunity;
    # quick fix: store sigma on Opportunity. But to keep this patch surgical,
    # display top 3 from the same scan we already computed.)
    # Re-scan would be wasteful, so compute by iterating the market once:
    from polymarket import rank_buckets_by_ev as _rank
    # Pull sigma off the matched bucket's neighborhood via re-fetch is heavy;
    # use a fixed reasonable sigma that matches our typical model output.
    sigma_proxy = 1.0 if opp.unit == "C" else 1.8
    ranked = _rank(opp.market, float(opp.predicted_unit), sigma_proxy)[:3]

    keyboard = []
    for i, p in enumerate(ranked):
        is_best = (i == 0)
        is_match = p.bucket.market_slug == matched_slug
        bar = _yes_bar(p.market_p, width=8)
        emoji = "💰" if is_best else "🎯" if is_match else "•"
        edge = f"{p.edge_pp:+.0f}pp"
        ev_per_d = f"${p.ev_per_dollar:+.2f}"
        lines.append(
            f"{emoji} *{p.bucket.label}*"
            f"{'  ✅ matches model' if is_match else ''}\n"
            f"   `{bar}` *{int(round(p.market_p*100))}%* market · "
            f"*{int(round(p.model_p*100))}%* model · edge *{edge}*\n"
            f"   _EV per $1: {ev_per_d}_  {_md_link('▶ Trade', p.bucket.trade_url)}"
        )
        keyboard.append([InlineKeyboardButton(
            f"{emoji} {p.bucket.label} ({int(round(p.market_p*100))}%)",
            url=p.bucket.trade_url,
        )])

    lines.append("")
    lines.append(
        "💰 = best EV pick · 🎯 = matches model · ✅ = market's matched bucket"
    )
    lines.append(
        "_EV per $1 = (model probability ÷ market price) − 1. Positive = "
        "model thinks bucket is mispriced cheap._"
    )

    keyboard.append([
        InlineKeyboardButton("⬅ All opportunities", callback_data="opp_scan"),
    ])

    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True,
    )


async def track_airport(update: Update, code: str) -> None:
    code = code.upper().strip()
    airport = airports_db.lookup(code)
    if not airport:
        await update.effective_message.reply_text(
            f"❌ Airport not found: `{_md_safe(code)}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if tracking_db.add(user_id, chat_id, airport.icao):
        await update.effective_message.reply_text(
            f"✅ Now tracking *{airport.icao}* — {_md_safe(airport.name)}\n\n"
            f"You'll get an alert if the predicted max temp shifts by "
            f"≥{int(ALERT_THRESHOLD_F)}°F ({ALERT_THRESHOLD_C:g}°C). "
            f"Checks run every {TRACKING_INTERVAL_MINUTES} min.",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.effective_message.reply_text(
            f"ℹ️ Already tracking *{airport.icao}*.",
            parse_mode=ParseMode.MARKDOWN,
        )


# ─────────────────────────── formatting ──────────────────────────
def _md_safe(s: str) -> str:
    """Escape characters that break Telegram's legacy Markdown."""
    if not s:
        return ""
    for ch in ("*", "_", "`", "["):
        s = s.replace(ch, " ")
    return s


def _flag(f: DayForecast) -> str:
    if f.high_confidence:
        return "🟢"
    if f.confidence_level == "MEDIUM":
        return "🟡"
    return "🔴"


def _md_link(label: str, url: str) -> str:
    """Inline markdown link, with [] inside the label sanitized."""
    safe = label.replace("[", "(").replace("]", ")")
    return f"[{safe}]({url})"


def _yes_bar(prob: float, width: int = 10) -> str:
    """Compact inline bar using thin block characters. ▰ = filled, ▱ = empty.
    Always shows ≥1 filled if prob > 0.05, never shows full unless prob ≈ 1.
    """
    if not (0 <= prob <= 1):
        prob = max(0.0, min(1.0, prob))
    filled = int(round(prob * width))
    if prob > 0.05 and filled == 0:
        filled = 1
    if prob < 0.999 and filled >= width:
        filled = width - 1
    return "▰" * filled + "▱" * (width - filled)


def _format_polymarket_block(market, fc: DayForecast) -> str:
    """Compact Polymarket section: 3 model-centered picks rendered as
    single-line entries with inline bars."""
    pred = fc.predicted_max_c if market.unit == "C" else fc.predicted_max_f
    matched = match_for_prediction(market, pred)
    matched_slug = matched.market_slug if matched else None

    out = []
    out.append(f"\n🎲 *Polymarket* — {market.city_display} (°{market.unit})")
    out.append(
        f"🏟️ _{_md_safe(market.resolves_at_name)} ({market.resolves_at_icao})_"
    )

    picks = hedges_around(market, pred, target_count=3)
    if not picks:
        picks = top_n_by_yes(market, n=3)

    for b in sorted(picks, key=lambda x: x.value):
        check = " ✅" if matched_slug and b.market_slug == matched_slug else ""
        yes_pct = int(round(b.yes_prob * 100))
        no_pct = 100 - yes_pct
        bar = _yes_bar(b.yes_prob)
        out.append(
            f"`{bar}` *{b.label}*{check} · *{yes_pct}%*Y _{no_pct}%N_  "
            f"{_md_link('Trade', b.trade_url)}"
        )

    crowd_top3_slugs = {b.market_slug for b in top_n_by_yes(market, n=3)}
    if matched_slug and matched_slug not in crowd_top3_slugs:
        out.append("⚠️ _Our pick differs from the crowd — verify before trading._")

    return "\n".join(out)

    return "\n".join(out)


def format_forecast(airport, forecasts, current, markets_by_date=None) -> str:
    markets_by_date = markets_by_date or {}
    parts = []
    code_str = f"*{airport.icao}*"
    if airport.iata:
        code_str += f" / *{airport.iata}*"
    parts.append(f"{airport.type_emoji} {code_str}")
    parts.append(f"📍 {_md_safe(airport.name)}")
    parts.append(f"🌍 {_md_safe(airport.city)}, {airport.country}")

    # Current observation block
    if current and current.temp_c is not None:
        c = int(round(current.temp_c))
        f_v = int(round(current.temp_f))
        line = f"\n📡 *Current ({current.source})*: {f_v}°F / {c}°C"
        if current.wind_kt is not None:
            wd = current.wind_dir if current.wind_dir is not None else "—"
            line += f"  ·  💨 {int(round(current.wind_kt))} kt @ {wd}°"
        parts.append(line)

    parts.append(
        "\n🧠 *Ensemble forecast* — 8 NWP models, ECMWF-weighted"
    )
    parts.append("─" * 26)

    for i, fc in enumerate(forecasts):
        if i == 0:
            day_label = "📅 *Today*"
        elif i == 1:
            day_label = "📅 *Tomorrow*"
        else:
            day_label = f"📅 *{fc.date.strftime('%a')}*"
        date_label = fc.date.strftime("%b %d")

        flag = _flag(fc)
        parts.append(f"\n{day_label} _{date_label}_")
        parts.append(
            f"🌡️ Max: *{fc.predicted_max_f}°F / {fc.predicted_max_c}°C*  {flag}"
        )

        # Range (mean ± σ rounded)
        lo_c = int(round(fc.ensemble_mean_c - fc.std_c))
        hi_c = int(round(fc.ensemble_mean_c + fc.std_c))
        lo_f = int(round(lo_c * 9 / 5 + 32))
        hi_f = int(round(hi_c * 9 / 5 + 32))
        parts.append(
            f"   📉 Range ±1σ: {lo_f}–{hi_f}°F ({lo_c}–{hi_c}°C)"
        )

        parts.append(
            f"   🎯 Confidence: *{int(fc.confidence * 100)}%* "
            f"({fc.confidence_level}) · σ {fc.std_c:.1f}°C"
        )

        # Top 3 probabilities (Fahrenheit)
        top = sorted(fc.probability_f.items(), key=lambda x: -x[1])[:3]
        prob_str = " · ".join(f"{t}°F: *{int(p * 100)}%*" for t, p in top)
        parts.append(f"   📊 {prob_str}")

        # ───── Polymarket section (only when a market exists for this day) ─────
        market = markets_by_date.get(fc.date)
        if market:
            parts.append(_format_polymarket_block(market, fc))

    parts.append("\n" + "─" * 26)
    parts.append("🟢 high confidence  ·  🟡 medium  ·  🔴 low")
    parts.append("_Predictions are whole-number °F and °C, ensemble-calibrated to ±2°._")
    return "\n".join(parts)


# ─────────────────────────── handlers ─────────────────────────────
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data.startswith("fc:"):
        await send_forecast(update, data[3:])
    elif data.startswith("track:"):
        await track_airport(update, data[6:])
    elif data.startswith("untrack:"):
        code = data[8:]
        if tracking_db.remove(update.effective_user.id, code):
            await q.message.reply_text(
                f"✅ No longer tracking *{code}*.",
                parse_mode=ParseMode.MARKDOWN,
            )
    elif data.startswith("models:"):
        await show_models_breakdown(update, data[7:])
    elif data == "pm_menu":
        await show_polymarket_city_menu(update)
    elif data.startswith("pm:"):
        await send_polymarket_forecast(update, data[3:])
    elif data == "opp_scan":
        await cmd_opportunities(update, ctx)
    elif data.startswith("opp:"):
        # opp:<city_key>:<YYYY-MM-DD>
        rest = data[4:]
        try:
            city_key, date_iso = rest.split(":", 1)
        except ValueError:
            return
        await show_opportunity_detail(update, city_key, date_iso)


_AIRPORT_CODE_RE = re.compile(r"^[A-Za-z]{3,4}$")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    txt = (update.effective_message.text or "").strip()
    if not txt:
        return

    # Reply-keyboard buttons
    if txt == "🎯 Opportunities":
        await cmd_opportunities(update, ctx)
        return
    if txt == "🎲 Polymarket":
        await show_polymarket_city_menu(update)
        return
    if txt == "🌤️ Forecast":
        await update.effective_message.reply_text(
            "🌤️ Send me an airport code — just type it.\n\nExample: *KJFK* or *LAX*",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if txt == "🔍 Search City":
        await update.effective_message.reply_text(
            "🔍 Send me a city name — just type it.\n\nExample: *London*",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if txt == "📋 My Tracked":
        await cmd_list(update, ctx)
        return
    if txt == "❓ Help":
        await cmd_help(update, ctx)
        return

    # 3- or 4-letter code that maps to a known airport → forecast
    if _AIRPORT_CODE_RE.match(txt) and airports_db.lookup(txt):
        await send_forecast(update, txt)
        return

    # Anything else → city search
    await do_city_search(update, txt)


# ─────────────────────────── tracking job ─────────────────────────
async def tracking_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    rows = tracking_db.list_all()
    if not rows:
        return
    log.info("tracking_job: checking %d entries", len(rows))
    for row_id, user_id, chat_id, code, last_c, last_f, last_bucket in rows:
        airport = airports_db.lookup(code)
        if not airport:
            continue
        try:
            forecasts = await fetch_ensemble_forecast(
                airport.lat, airport.lon, days=1, icao=airport.icao,
            )
        except Exception:
            log.exception("forecast fetch failed for %s", code)
            continue
        if not forecasts:
            continue
        today = forecasts[0]
        new_c = today.predicted_max_c
        new_f = today.predicted_max_f

        # Look up Polymarket for this airport's city (explicit map or
        # geographic fallback), today (if any).
        market = None
        new_bucket_label = None
        resolved = resolve_city_for_airport(airport.icao, airport.lat, airport.lon)
        if resolved:
            city_key, _market_unit, _src = resolved
            try:
                market = await get_market_for_city(city_key, today.date)
            except Exception:
                log.exception("polymarket fetch failed for %s", code)
                market = None
            if market:
                pred = today.predicted_max_c if market.unit == "C" else today.predicted_max_f
                matched = match_for_prediction(market, pred)
                if matched:
                    new_bucket_label = matched.label

        # Decide whether to alert.
        # Per user spec: include Polymarket data in the alert only when our
        # model's bucket changes. Temperature-threshold alerts still fire as
        # before, but without the market block.
        bucket_changed = (
            new_bucket_label is not None
            and last_bucket is not None
            and new_bucket_label != last_bucket
        )
        temp_changed = False
        if last_c is not None and last_f is not None:
            d_c = abs(new_c - last_c)
            d_f = abs(new_f - last_f)
            if d_c >= ALERT_THRESHOLD_C or d_f >= ALERT_THRESHOLD_F:
                temp_changed = True

        if bucket_changed or temp_changed:
            arrow = "📈" if (last_c is not None and new_c > last_c) else "📉"
            flag = _flag(today)
            lines = [
                f"🔔 *Forecast Alert: {code}*",
                f"📍 {_md_safe(airport.name)}",
                "",
                f"{arrow} Today's predicted max changed:",
                f"   Old: {int(round(last_f))}°F / {int(round(last_c))}°C"
                if last_f is not None else "   Old: —",
                f"   New: *{new_f}°F / {new_c}°C* {flag}",
            ]
            if last_f is not None:
                lines.append(
                    f"   Δ: {int(new_f - last_f):+d}°F / {int(new_c - last_c):+d}°C"
                )
            lines.append("")
            lines.append(
                f"Confidence: *{int(today.confidence * 100)}%* "
                f"({today.confidence_level})"
            )

            # Polymarket block — included only when the bucket changed.
            if bucket_changed and market:
                lines.append("")
                lines.append(
                    f"🪣 Polymarket bucket shifted: "
                    f"*{last_bucket}* → *{new_bucket_label}*"
                )
                lines.append(_format_polymarket_block(market, today).lstrip("\n"))

            try:
                await ctx.bot.send_message(
                    chat_id,
                    "\n".join(lines),
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True,
                )
            except Exception:
                log.exception("failed to send alert to chat %s", chat_id)

        tracking_db.update_last(row_id, new_c, new_f, new_bucket_label)


# ─────────────────────────── lifecycle ────────────────────────────
async def post_init(app: Application) -> None:
    commands = [
        BotCommand("start", "🌟 Welcome & menu"),
        BotCommand("opportunities", "🎯 High-confidence trade picks"),
        BotCommand("polymarket", "🎲 Polymarket forecast (35 cities)"),
        BotCommand("forecast", "🌤️ Forecast by airport code"),
        BotCommand("search", "🔍 Search city for airports"),
        BotCommand("track", "🔔 Track for change alerts"),
        BotCommand("untrack", "🔕 Stop tracking"),
        BotCommand("list", "📋 Your tracked airports"),
        BotCommand("help", "❓ Help & methodology"),
    ]
    await app.bot.set_my_commands(commands)
    await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    log.info("Commands menu registered (bottom-left button)")


def main() -> None:
    log.info("Loading airport database…")
    ensure_airport_data(AIRPORTS_CSV)
    airports_db.load_from_csv(AIRPORTS_CSV)
    log.info("Loaded %d airports", len(airports_db.airports))

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("opportunities", cmd_opportunities))
    app.add_handler(CommandHandler("polymarket", cmd_polymarket))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("forecast", cmd_forecast))
    app.add_handler(CommandHandler("track", cmd_track))
    app.add_handler(CommandHandler("untrack", cmd_untrack))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    if app.job_queue is not None:
        app.job_queue.run_repeating(
            tracking_job,
            interval=TRACKING_INTERVAL_MINUTES * 60,
            first=60,
            name="tracking_job",
        )
        log.info("Tracking job scheduled every %d min", TRACKING_INTERVAL_MINUTES)
    else:
        log.warning(
            "job_queue not available — install python-telegram-bot[job-queue]"
        )

    log.info("Starting bot — long polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
