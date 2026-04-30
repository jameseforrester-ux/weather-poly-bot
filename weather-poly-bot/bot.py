#!/usr/bin/env python3
"""
WeatherPolyBot — main entry point
──────────────────────────────────
A Telegram bot that:
  • Predicts daily high temperatures using a weighted ensemble of
    ECMWF IFS, NOAA GFS, and DWD ICON (weights derived from a 30-day
    backtest across 12 diverse airport/city locations)
  • Searches Polymarket for temperature markets and detects ≥40¢ edges
  • Tracks positions with live P&L
  • Monitors favourite markets and tracked locations in the background
  • Runs as a systemd service so it stays alive when PuTTY is closed
"""

import asyncio
import logging
from datetime import datetime

import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import database as db
import keyboards as kb
import backtest_service
import polymarket_service
import weather_service
from config import (
    TELEGRAM_TOKEN,
    DEFAULT_WEIGHTS,
    EDGE_THRESHOLD,
    TRACKED_REFRESH_MIN,
)

# ─────────────────────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
#  Conversation states
# ─────────────────────────────────────────────────────────
(
    AWAIT_LOCATION_PREDICT,
    AWAIT_LOCATION_TRACK,
    AWAIT_POS_OUTCOME,
    AWAIT_POS_SHARES,
    AWAIT_POS_PRICE,
    AWAIT_POS_NOTES,
    AWAIT_UPDATE_PRICE,
    AWAIT_CLOSE_PRICE,
) = range(8)


# ─────────────────────────────────────────────────────────
#  Formatting helpers
# ─────────────────────────────────────────────────────────

HEADER = "🌦️  <b>WeatherPolyBot</b>"
DIV    = "━━━━━━━━━━━━━━━━━━━━━━━━━━"


def _temp_bar(c: float) -> str:
    """Visual thermometer bar."""
    # Maps roughly -20°C to +50°C → 0 to 30 blocks
    blocks = max(0, min(30, int((c + 20) / 70 * 30)))
    return "🌡️ " + "█" * blocks + "░" * (30 - blocks)


def _pnl_icon(pnl: float) -> str:
    if pnl > 0:
        return "📈"
    if pnl < 0:
        return "📉"
    return "➡️"


def format_prediction(pred: dict, display_name: str) -> str:
    wts = pred.get("weights", {})
    models = pred.get("models", {})

    lines = [
        HEADER,
        DIV,
        f"📍 <b>{display_name}</b>",
        f"🕐 Local time: <i>{pred['local_time']}</i>",
        "",
        f"🗓️ <b>TODAY</b>  ({pred['today_label']})",
        f"   🔥 High: <b>{pred['today_c']}°C  /  {pred['today_f']}°F</b>",
        _temp_bar(pred['today_c']),
        "",
        f"🗓️ <b>TOMORROW</b>  ({pred['tomorrow_label']})",
        f"   🔥 High: <b>{pred['tomorrow_c']}°C  /  {pred['tomorrow_f']}°F</b>",
        _temp_bar(pred['tomorrow_c']),
        "",
        DIV,
        "📡 <b>MODEL BREAKDOWN</b>",
        "<code>",
        f"{'Model':<13} {'Today':>6} {'Tom.':>6}  {'Wt':>5}",
        f"{'─'*13} {'─'*6} {'─'*6}  {'─'*5}",
    ]
    icons = {"ECMWF IFS": "🔵", "NOAA GFS": "🟢", "DWD ICON": "🟡"}
    for name, d in models.items():
        ic = icons.get(name, "⚫")
        wt = wts.get(name, 0)
        lines.append(
            f"{ic}{name:<11} {d['today_high']:>5.1f}° {d['tomorrow_high']:>5.1f}°  {wt*100:>4.0f}%"
        )
    lines += [
        "</code>",
        "",
        f"⚡ <b>Best estimate:</b>",
        f"   Today    → <b>{pred['today_c']}°C / {pred['today_f']}°F</b>",
        f"   Tomorrow → <b>{pred['tomorrow_c']}°C / {pred['tomorrow_f']}°F</b>",
        DIV,
        "🎯 Tap <i>Scan Markets</i> to find Polymarket edges",
    ]
    return "\n".join(lines)


def format_position(p: dict) -> str:
    cost     = p["shares"] * p["entry_price"]
    cur_val  = p["shares"] * (p["current_price"] or p["entry_price"])
    pnl      = cur_val - cost
    pnl_pct  = (pnl / cost * 100) if cost else 0
    icon     = _pnl_icon(pnl)
    status   = "🔒 CLOSED" if p["closed"] else "🟢 OPEN"

    lines = [
        f"{DIV}",
        f"<b>Position #{p['id']}</b>  {status}",
        f"📍 {p['location']}",
        f"📋 Market: {p['market_name'][:60]}",
        f"🎲 Outcome: <b>{p['outcome']}</b>",
        f"",
        f"📊 Shares: <b>{p['shares']:.2f}</b>",
        f"💰 Entry:  <b>${p['entry_price']:.3f}</b>/share",
        f"💱 Current: <b>${(p['current_price'] or p['entry_price']):.3f}</b>/share",
        f"",
        f"{icon} P&L: <b>${pnl:+.2f}</b> ({pnl_pct:+.1f}%)",
        f"📅 Entered: {p['entry_time'][:16]}",
    ]
    if p.get("predicted_c"):
        lines.append(f"🌡️ Prediction was: {p['predicted_c']}°C / {p['predicted_f']}°F")
    if p.get("notes"):
        lines.append(f"📝 Notes: {p['notes']}")
    if p.get("market_url"):
        lines.append(f"🔗 <a href=\"{p['market_url']}\">Open Market</a>")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────
#  Command: /start
# ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    text = (
        f"{HEADER}\n"
        f"{DIV}\n"
        f"👋 Welcome, <b>{user.first_name}</b>!\n\n"
        f"I combine three top-tier NWP models into a single precise "
        f"temperature forecast, then scan Polymarket for profitable edges.\n\n"
        f"<b>Models used:</b>\n"
        f"  🔵 ECMWF IFS  — World's #1 global model\n"
        f"  🟢 NOAA GFS   — NOAA's flagship model\n"
        f"  🟡 DWD ICON   — German Weather Service\n\n"
        f"Weights are auto-calibrated from a 30-day backtest across\n"
        f"12 airports &amp; cities on every continent.\n"
        f"{DIV}\n"
        f"Choose an option below 👇"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML,
                                    reply_markup=kb.main_menu())


# ─────────────────────────────────────────────────────────
#  Menu router
# ─────────────────────────────────────────────────────────
async def menu_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q   = update.callback_query
    await q.answer()
    data = q.data

    if data == "menu_main":
        await q.edit_message_text(
            f"{HEADER}\n{DIV}\nChoose an option 👇",
            parse_mode=ParseMode.HTML,
            reply_markup=kb.main_menu(),
        )
        return ConversationHandler.END

    elif data == "menu_predict":
        ctx.user_data["action"] = "predict"
        await q.edit_message_text(
            f"{HEADER}\n{DIV}\n📍 <b>Temperature Prediction</b>\n\n"
            "Enter a city, address, or airport (e.g. <code>JFK Airport</code>, "
            "<code>London Heathrow</code>, <code>Tokyo</code>):",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Cancel", callback_data="menu_main")
            ]]),
        )
        return AWAIT_LOCATION_PREDICT

    elif data == "menu_track":
        ctx.user_data["action"] = "track"
        await q.edit_message_text(
            f"{HEADER}\n{DIV}\n📡 <b>Track a Location</b>\n\n"
            "Enter the location to track (I'll auto-refresh every hour):",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 View Tracked", callback_data="show_tracked"),
                InlineKeyboardButton("🏠 Cancel",        callback_data="menu_main"),
            ]]),
        )
        return AWAIT_LOCATION_TRACK

    elif data == "show_tracked" or data == "menu_tracked":
        return await show_tracked(update, ctx)

    elif data == "menu_markets":
        ctx.user_data["action"] = "markets"
        await q.edit_message_text(
            f"{HEADER}\n{DIV}\n🎯 <b>Market Scanner</b>\n\n"
            "Enter a location to scan Polymarket for temperature markets:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Cancel", callback_data="menu_main")
            ]]),
        )
        return AWAIT_LOCATION_PREDICT      # reuse same state

    elif data == "menu_positions":
        return await show_positions(update, ctx)

    elif data == "menu_favourites":
        return await show_favourites(update, ctx)

    elif data == "menu_backtest":
        return await show_backtest(update, ctx)

    elif data == "menu_run_backtest":
        return await trigger_backtest(update, ctx)

    elif data == "menu_help":
        return await show_help(update, ctx)

    elif data.startswith("predict|"):
        parts = data.split("|")
        lat, lon, tz, loc_name = float(parts[1]), float(parts[2]), parts[3], parts[4].replace("_", " ")
        return await do_predict(update, ctx, lat, lon, tz, loc_name)

    elif data.startswith("scan|"):
        parts = data.split("|")
        lat, lon, tz, loc = float(parts[1]), float(parts[2]), parts[3], parts[4].replace("_", " ")
        return await do_scan(update, ctx, lat, lon, tz, loc)

    elif data.startswith("track|"):
        parts = data.split("|")
        lat, lon, tz, loc = float(parts[1]), float(parts[2]), parts[3], parts[4].replace("_", " ")
        return await do_track(update, ctx, lat, lon, tz, loc)

    elif data.startswith("untrack|"):
        loc_id = int(data.split("|")[1])
        user_id = update.effective_user.id
        await db.remove_tracked(user_id, loc_id)
        await q.answer("Location removed ✅")
        return await show_tracked(update, ctx)

    elif data.startswith("pos_detail|"):
        pos_id = int(data.split("|")[1])
        return await show_pos_detail(update, ctx, pos_id)

    elif data.startswith("pos_update|"):
        pos_id = int(data.split("|")[1])
        ctx.user_data["pos_update_id"] = pos_id
        await q.edit_message_text(
            f"Enter new current price for Position #{pos_id} (e.g. <code>0.62</code>):",
            parse_mode=ParseMode.HTML,
        )
        return AWAIT_UPDATE_PRICE

    elif data.startswith("pos_close|"):
        pos_id = int(data.split("|")[1])
        ctx.user_data["pos_close_id"] = pos_id
        await q.edit_message_text(
            f"Enter the closing price for Position #{pos_id} (e.g. <code>0.85</code>):",
            parse_mode=ParseMode.HTML,
        )
        return AWAIT_CLOSE_PRICE

    elif data.startswith("pos_delete|"):
        pos_id = int(data.split("|")[1])
        user_id = update.effective_user.id
        await db.delete_position(user_id, pos_id)
        await q.answer("Position deleted 🗑️")
        return await show_positions(update, ctx)

    elif data.startswith("addpos|"):
        parts = data.split("|")
        ctx.user_data["addpos_market_id"] = parts[1]
        ctx.user_data["addpos_lat"]        = float(parts[2])
        ctx.user_data["addpos_lon"]        = float(parts[3])
        ctx.user_data["addpos_pred_c"]     = float(parts[4])
        await q.edit_message_text(
            "📋 <b>New Position</b>\n\n"
            "Enter the outcome you're buying (e.g. <code>YES - High > 75°F</code>):",
            parse_mode=ParseMode.HTML,
        )
        return AWAIT_POS_OUTCOME

    elif data.startswith("addfav|"):
        market_id = data.split("|")[1]
        return await add_favourite(update, ctx, market_id)

    elif data.startswith("fav_refresh|"):
        parts = data.split("|")
        fav_id, market_id = int(parts[1]), parts[2]
        return await refresh_favourite(update, ctx, fav_id, market_id)

    elif data.startswith("fav_remove|"):
        fav_id = int(data.split("|")[1])
        user_id = update.effective_user.id
        await db.remove_favourite(user_id, fav_id)
        await q.answer("Removed from favourites ✅")
        return await show_favourites(update, ctx)

    elif data == "back_markets":
        # Re-show last market scan stored in user_data
        cached = ctx.user_data.get("last_scan_text", "No recent scan. Use 🎯 Market Scanner.")
        await q.edit_message_text(cached, parse_mode=ParseMode.HTML,
                                  reply_markup=kb.back_to_menu(),
                                  disable_web_page_preview=True)
        return ConversationHandler.END

    return ConversationHandler.END


# ─────────────────────────────────────────────────────────
#  Location input handler (shared for predict & track)
# ─────────────────────────────────────────────────────────
async def handle_location_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text    = update.message.text.strip()
    action  = ctx.user_data.get("action", "predict")
    uid     = update.effective_user.id

    msg = await update.message.reply_text(
        "🔍 Geocoding location…", parse_mode=ParseMode.HTML
    )

    geo = await weather_service.geocode(text)
    if not geo:
        await msg.edit_text(
            "❌ Could not find that location. Try a city name, airport name, or IATA code.",
            reply_markup=kb.back_to_menu(),
        )
        return ConversationHandler.END

    lat, lon, tz = geo["lat"], geo["lon"], geo["timezone"]
    display      = geo["display"]

    if action == "track":
        loc_id = await db.add_tracked(uid, geo["name"], lat, lon, tz, display)
        await msg.edit_text(
            f"✅ <b>Tracking added!</b>\n📍 {display}\n\n"
            f"I'll refresh this location every {TRACKED_REFRESH_MIN} minutes.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb.back_and_refresh(
                f"predict|{lat}|{lon}|{tz}|{geo['name'][:30]}"
            ),
        )
        return ConversationHandler.END

    elif action == "markets":
        await msg.edit_text("📡 Fetching forecast &amp; scanning markets…",
                            parse_mode=ParseMode.HTML)
        return await do_scan_from_geo(update, ctx, geo, msg)

    else:  # predict
        await msg.edit_text("📡 Fetching multi-model forecast…",
                            parse_mode=ParseMode.HTML)
        return await do_predict_from_geo(update, ctx, geo, msg)


# ─────────────────────────────────────────────────────────
#  Prediction logic
# ─────────────────────────────────────────────────────────
async def do_predict(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                     lat: float, lon: float, tz: str, loc_name: str) -> int:
    q = update.callback_query
    await q.edit_message_text("📡 Fetching forecast…", parse_mode=ParseMode.HTML)

    weights = await db.load_latest_weights() or DEFAULT_WEIGHTS
    pred    = await weather_service.ensemble_prediction(lat, lon, tz, weights)

    if not pred:
        await q.edit_message_text(
            "❌ Could not retrieve forecast data. Please try again.",
            reply_markup=kb.back_to_menu(),
        )
        return ConversationHandler.END

    text = format_prediction(pred, loc_name)
    ctx.user_data["last_pred"] = pred
    ctx.user_data["last_loc"]  = loc_name

    await q.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=kb.prediction_actions(lat, lon, tz, loc_name),
    )
    return ConversationHandler.END


async def do_predict_from_geo(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                               geo: dict, msg) -> int:
    weights = await db.load_latest_weights() or DEFAULT_WEIGHTS
    pred    = await weather_service.ensemble_prediction(
        geo["lat"], geo["lon"], geo["timezone"], weights
    )
    if not pred:
        await msg.edit_text("❌ Forecast unavailable. Try again.",
                            reply_markup=kb.back_to_menu())
        return ConversationHandler.END

    text = format_prediction(pred, geo["display"])
    ctx.user_data["last_pred"] = pred
    ctx.user_data["last_loc"]  = geo["display"]

    await msg.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=kb.prediction_actions(
            geo["lat"], geo["lon"], geo["timezone"], geo["name"][:40]
        ),
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────
#  Market scan logic
# ─────────────────────────────────────────────────────────
async def do_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                  lat: float, lon: float, tz: str, loc: str) -> int:
    q = update.callback_query
    await q.edit_message_text("🔍 Fetching forecast + scanning Polymarket…",
                               parse_mode=ParseMode.HTML)

    weights = await db.load_latest_weights() or DEFAULT_WEIGHTS
    pred    = await weather_service.ensemble_prediction(lat, lon, tz, weights)
    if not pred:
        await q.edit_message_text("❌ Forecast unavailable.", reply_markup=kb.back_to_menu())
        return ConversationHandler.END

    scan = await polymarket_service.search_temperature_markets(loc, pred["today_c"])
    text = _format_scan(loc, pred, scan)
    ctx.user_data["last_scan_text"] = text

    await q.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=kb.back_to_menu(),
        disable_web_page_preview=True,
    )
    return ConversationHandler.END


async def do_scan_from_geo(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                            geo: dict, msg) -> int:
    weights = await db.load_latest_weights() or DEFAULT_WEIGHTS
    pred    = await weather_service.ensemble_prediction(
        geo["lat"], geo["lon"], geo["timezone"], weights
    )
    if not pred:
        await msg.edit_text("❌ Forecast unavailable.", reply_markup=kb.back_to_menu())
        return ConversationHandler.END

    scan = await polymarket_service.search_temperature_markets(geo["name"], pred["today_c"])
    text = _format_scan(geo["display"], pred, scan)
    ctx.user_data["last_scan_text"] = text

    await msg.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=kb.back_to_menu(),
        disable_web_page_preview=True,
    )
    return ConversationHandler.END


def _format_scan(location: str, pred: dict, scan: dict) -> str:
    edge_markets = scan["edge_markets"]
    all_markets  = scan["all_markets"]

    lines = [
        f"{HEADER}",
        f"{DIV}",
        f"🎯 <b>MARKET SCANNER</b>",
        f"📍 {location}",
        f"🌡️ Predicted today: <b>{pred['today_c']}°C / {pred['today_f']}°F</b>",
        f"{DIV}",
    ]

    if not all_markets:
        lines += [
            "⚠️ No temperature markets found for this location on Polymarket.",
            "",
            "💡 <i>Try searching by city name (e.g. New York, Chicago)</i>",
        ]
    else:
        if edge_markets:
            lines.append(f"🚀 <b>{len(edge_markets)} EDGE(S) ≥ {int(EDGE_THRESHOLD*100)}¢</b>")
        else:
            lines.append(f"🔍 Found {len(all_markets)} market(s) — no edges ≥ {int(EDGE_THRESHOLD*100)}¢ right now")
        lines.append("")
        shown = (edge_markets or all_markets)[:5]
        for i, m in enumerate(shown):
            lines.append(polymarket_service.format_market_card(m, i))
            lines.append("")

    lines += [DIV,
              f"<i>Edge = our probability − market price  (threshold ≥ +{int(EDGE_THRESHOLD*100)}¢)</i>"]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────
#  Track location
# ─────────────────────────────────────────────────────────
async def do_track(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                   lat: float, lon: float, tz: str, loc: str) -> int:
    uid = update.effective_user.id
    q   = update.callback_query
    await db.add_tracked(uid, loc, lat, lon, tz, loc)
    await q.answer(f"✅ Tracking {loc}")
    await q.edit_message_text(
        f"✅ <b>Now tracking: {loc}</b>\n\n"
        f"Refreshed every {TRACKED_REFRESH_MIN} min. View in 📡 Tracked Locations.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb.back_to_menu(),
    )
    return ConversationHandler.END


async def show_tracked(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid  = update.effective_user.id
    q    = update.callback_query
    locs = await db.get_tracked(uid)

    if not locs:
        text = (f"{HEADER}\n{DIV}\n📡 <b>Tracked Locations</b>\n\n"
                "You haven't tracked any locations yet.\n"
                "Use <i>📡 Track Location</i> to add one.")
        markup = kb.back_to_menu()
    else:
        text = (f"{HEADER}\n{DIV}\n📡 <b>Tracked Locations</b>  ({len(locs)})\n\n"
                "Tap a location to see its latest forecast:")
        markup = kb.tracked_list(locs)

    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────
#  Positions
# ─────────────────────────────────────────────────────────
async def show_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid       = update.effective_user.id
    q         = update.callback_query
    positions = await db.get_open_positions(uid)

    if not positions:
        await q.edit_message_text(
            f"{HEADER}\n{DIV}\n💼 <b>My Positions</b>\n\nNo open positions yet.\n\n"
            "Use 🌡️ Predict → 🎯 Scan Markets → 💼 Add Position to get started.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb.back_to_menu(),
        )
        return ConversationHandler.END

    total_pnl  = 0.0
    total_cost = 0.0
    for p in positions:
        cost  = p["shares"] * p["entry_price"]
        cur   = p["shares"] * (p["current_price"] or p["entry_price"])
        total_pnl  += cur - cost
        total_cost += cost

    icon = _pnl_icon(total_pnl)
    text = (
        f"{HEADER}\n{DIV}\n"
        f"💼 <b>Open Positions</b>  ({len(positions)})\n\n"
        f"{icon} Total P&amp;L: <b>${total_pnl:+.2f}</b>"
        f"  ({total_pnl / total_cost * 100:+.1f}%)\n\n"
        "Tap a position for details:"
    )
    await q.edit_message_text(text, parse_mode=ParseMode.HTML,
                               reply_markup=kb.position_list(positions))
    return ConversationHandler.END


async def show_pos_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                          pos_id: int) -> int:
    q    = update.callback_query
    uid  = update.effective_user.id
    all_pos = await db.get_all_positions(uid)
    pos  = next((p for p in all_pos if p["id"] == pos_id), None)

    if not pos:
        await q.answer("Position not found")
        return ConversationHandler.END

    text   = format_position(pos)
    markup = kb.position_detail(pos_id, pos.get("market_url"))
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                               disable_web_page_preview=True)
    return ConversationHandler.END


# Position entry flow ─────────────────────────────────────

async def handle_pos_outcome(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["addpos_outcome"] = update.message.text.strip()
    await update.message.reply_text(
        "💼 How many shares did you buy? (e.g. <code>50</code>):",
        parse_mode=ParseMode.HTML,
    )
    return AWAIT_POS_SHARES


async def handle_pos_shares(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        ctx.user_data["addpos_shares"] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Please enter a number (e.g. 50).")
        return AWAIT_POS_SHARES
    await update.message.reply_text(
        "💰 Entry price per share? (e.g. <code>0.42</code>):",
        parse_mode=ParseMode.HTML,
    )
    return AWAIT_POS_PRICE


async def handle_pos_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        ctx.user_data["addpos_price"] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Please enter a decimal (e.g. 0.42).")
        return AWAIT_POS_PRICE
    await update.message.reply_text(
        "📝 Any notes? (optional — type <code>skip</code> to skip):",
        parse_mode=ParseMode.HTML,
    )
    return AWAIT_POS_NOTES


async def handle_pos_notes(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    notes = update.message.text.strip()
    if notes.lower() == "skip":
        notes = ""

    uid     = update.effective_user.id
    ud      = ctx.user_data
    mid     = ud.get("addpos_market_id", "")
    pred_c  = ud.get("addpos_pred_c", 0.0)

    # Lookup market details from cache
    cached_scan = ud.get("last_scan_text", "")
    market_name = f"Market {mid[:8]}…"
    market_url  = f"https://polymarket.com/market/{mid}"

    pos_id = await db.add_position(
        user_id=uid,
        location=ud.get("last_loc", "Unknown"),
        market_id=mid,
        market_name=market_name,
        market_url=market_url,
        outcome=ud.get("addpos_outcome", ""),
        shares=ud.get("addpos_shares", 0),
        entry_price=ud.get("addpos_price", 0),
        pred_c=pred_c,
        pred_f=weather_service.c_to_f(pred_c),
        notes=notes,
    )

    cost = ud["addpos_shares"] * ud["addpos_price"]
    await update.message.reply_text(
        f"✅ <b>Position #{pos_id} saved!</b>\n\n"
        f"📋 {ud['addpos_outcome']}\n"
        f"💼 {ud['addpos_shares']:.0f} shares @ ${ud['addpos_price']:.3f}\n"
        f"💰 Cost basis: ${cost:.2f}\n\n"
        f"View in 💼 My Positions.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb.back_to_menu(),
    )
    return ConversationHandler.END


# Update / close price ────────────────────────────────────

async def handle_update_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        price = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Enter a decimal price.")
        return AWAIT_UPDATE_PRICE
    pos_id = ctx.user_data.get("pos_update_id")
    await db.update_position_price(pos_id, price)
    await update.message.reply_text(
        f"✅ Price updated to ${price:.3f}",
        reply_markup=kb.back_to_menu(),
    )
    return ConversationHandler.END


async def handle_close_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        price = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Enter a decimal price.")
        return AWAIT_CLOSE_PRICE
    pos_id = ctx.user_data.get("pos_close_id")
    await db.close_position(pos_id, price)
    await update.message.reply_text(
        f"🔒 Position #{pos_id} closed @ ${price:.3f}",
        reply_markup=kb.back_to_menu(),
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────
#  Favourites
# ─────────────────────────────────────────────────────────
async def show_favourites(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid  = update.effective_user.id
    q    = update.callback_query
    favs = await db.get_favorites(uid)

    if not favs:
        text = (f"{HEADER}\n{DIV}\n⭐ <b>Favourite Markets</b>\n\n"
                "No favourites yet.\n\n"
                "When browsing market results, tap ⭐ Add to Favs.")
        markup = kb.back_to_menu()
    else:
        text = (f"{HEADER}\n{DIV}\n⭐ <b>Favourite Markets</b>  ({len(favs)})\n\n"
                "Tap a market to open | 🔄 to refresh price | 🗑️ to remove:")
        markup = kb.favourites_list(favs)

    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    return ConversationHandler.END


async def add_favourite(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                        market_id: str) -> int:
    q   = update.callback_query
    uid = update.effective_user.id

    raw = await polymarket_service.get_market_by_id(market_id)
    if not raw:
        await q.answer("❌ Could not fetch market data")
        return ConversationHandler.END

    slug     = raw.get("slug", "")
    question = raw.get("question", "")
    name     = question[:60] or f"Market {market_id[:8]}"
    url      = polymarket_service._market_url(raw)

    await db.add_favorite(uid, market_id, name, url, question, "")
    await q.answer("⭐ Added to favourites!")
    return ConversationHandler.END


async def refresh_favourite(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                            fav_id: int, market_id: str) -> int:
    q = update.callback_query
    prices = await polymarket_service.get_current_price(market_id)
    if prices:
        y, n = prices
        await q.answer(f"YES: ${y:.2f}  NO: ${n:.2f}")
    else:
        await q.answer("Could not fetch price")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────
#  Backtest
# ─────────────────────────────────────────────────────────
async def show_backtest(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q    = update.callback_query
    rows = await db.load_latest_backtest()

    if not rows:
        text = (f"{HEADER}\n{DIV}\n📊 <b>Backtest Results</b>\n\n"
                "No backtest has been run yet.\n\n"
                "Tap 🔄 Run New Backtest to start (~2 min).")
        await q.edit_message_text(text, parse_mode=ParseMode.HTML,
                                   reply_markup=InlineKeyboardMarkup([[
                                       InlineKeyboardButton("🔄 Run Backtest",
                                                            callback_data="menu_run_backtest"),
                                       InlineKeyboardButton("🏠 Menu",
                                                            callback_data="menu_main"),
                                   ]]))
        return ConversationHandler.END

    # Aggregate rows by model
    by_model: dict[str, list] = {}
    for r in rows:
        by_model.setdefault(r["model"], []).append(r)

    weights = await db.load_latest_weights() or DEFAULT_WEIGHTS
    medal   = ["🥇", "🥈", "🥉"]
    ranked  = sorted(by_model.items(),
                     key=lambda x: sum(r["mae"] for r in x[1]) / len(x[1]))

    lines = [
        f"{HEADER}", f"{DIV}", "📊 <b>BACKTEST RESULTS</b>", "",
        "<code>",
        f"{'Model':<13} {'MAE':>5} {'RMSE':>5} {'Bias':>5} {'±1°C':>5} {'±2°C':>5}",
        f"{'─'*13} {'─'*5} {'─'*5} {'─'*5} {'─'*5} {'─'*5}",
    ]
    for i, (model, rs) in enumerate(ranked):
        mae  = sum(r["mae"]   for r in rs) / len(rs)
        rmse = sum(r["rmse"]  for r in rs) / len(rs)
        bias = sum(r["bias"]  for r in rs) / len(rs)
        a1   = sum(r["acc_1c"] for r in rs) / len(rs)
        a2   = sum(r["acc_2c"] for r in rs) / len(rs)
        m    = medal[i] if i < 3 else "  "
        lines.append(
            f"{m}{model:<11} {mae:>5.2f} {rmse:>5.2f} {bias:>+5.2f} {a1:>4.0f}% {a2:>4.0f}%"
        )
    lines += ["</code>", "", "⚖️ <b>Active Ensemble Weights</b>", "<code>"]
    for model, wt in sorted(weights.items(), key=lambda x: -x[1]):
        bar = "█" * int(wt * 20)
        lines.append(f"{model:<12} {wt*100:>5.1f}%  {bar}")
    lines += ["</code>", "", f"📍 {len(rows)} records  •  30-day window  •  12 locations"]

    await q.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Re-run Backtest", callback_data="menu_run_backtest"),
            InlineKeyboardButton("🏠 Menu",             callback_data="menu_main"),
        ]]),
    )
    return ConversationHandler.END


async def trigger_backtest(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.edit_message_text(
        f"{HEADER}\n{DIV}\n🔬 <b>Running Backtest…</b>\n\n"
        "Fetching 30 days of forecast + observed data\n"
        "across 12 airports &amp; cities.\n\n"
        "⏳ This takes ~90 seconds. Please wait…",
        parse_mode=ParseMode.HTML,
    )

    last_text = [None]

    async def progress(msg: str):
        if msg != last_text[0]:
            last_text[0] = msg
            try:
                await q.edit_message_text(
                    f"{HEADER}\n{DIV}\n🔬 <b>Backtest Running</b>\n\n{msg}",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

    try:
        bt = await backtest_service.run_backtest(progress_cb=progress)
        summary = backtest_service.format_backtest_summary(bt)
        await q.edit_message_text(
            summary,
            parse_mode=ParseMode.HTML,
            reply_markup=kb.back_to_menu(),
        )
    except Exception as e:
        log.exception("Backtest failed: %s", e)
        await q.edit_message_text(
            f"❌ Backtest failed: {e}\n\nTry again later.",
            reply_markup=kb.back_to_menu(),
        )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────
#  Help
# ─────────────────────────────────────────────────────────
async def show_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    text = (
        f"{HEADER}\n{DIV}\n"
        "ℹ️ <b>HELP &amp; COMMANDS</b>\n\n"
        "<b>🌡️ Predict Temperature</b>\n"
        "  Enter any city, address, or airport name/code.\n"
        "  Shows today &amp; tomorrow high in °C and °F.\n\n"
        "<b>📡 Track Location</b>\n"
        "  Save a location for hourly auto-refresh.\n\n"
        "<b>🎯 Market Scanner</b>\n"
        "  Finds Polymarket temperature markets near your location\n"
        "  and computes trading edge (our prob − market price).\n"
        f"  Flags any edge ≥ {int(EDGE_THRESHOLD*100)}¢/share as a trade opportunity.\n\n"
        "<b>💼 My Positions</b>\n"
        "  Track shares, entry price, and live P&amp;L.\n"
        "  Update or close positions as markets move.\n\n"
        "<b>⭐ Favourites</b>\n"
        "  Pin specific markets for instant price refresh.\n\n"
        "<b>📊 Backtest</b>\n"
        "  View or re-run the 30-day accuracy evaluation\n"
        "  that calibrates model weights.\n\n"
        f"{DIV}\n"
        "<b>Models</b>: ECMWF IFS · NOAA GFS · DWD ICON\n"
        "<b>Data</b>: Open-Meteo API (free, no key needed)\n"
        "<b>Markets</b>: Polymarket Gamma API\n"
        "<b>Edge formula</b>: P_model(YES) − Market_price(YES)\n"
    )
    await q.edit_message_text(text, parse_mode=ParseMode.HTML,
                               reply_markup=kb.back_to_menu())
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────
#  Cancel / fallback
# ─────────────────────────────────────────────────────────
async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "✅ Cancelled.", reply_markup=kb.back_to_menu()
    )
    return ConversationHandler.END


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"{HEADER}\n{DIV}\nChoose an option 👇",
        parse_mode=ParseMode.HTML,
        reply_markup=kb.main_menu(),
    )


# ─────────────────────────────────────────────────────────
#  Background job: auto-refresh tracked locations
# ─────────────────────────────────────────────────────────
async def job_refresh_tracked(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs every TRACKED_REFRESH_MIN minutes — sends update to all tracked users."""
    log.info("Job: refreshing tracked locations")
    # We don't have a user_id list handy in the job context, so we query all
    async with __import__("aiosqlite").connect(__import__("config").DB_PATH) as conn:
        conn.row_factory = __import__("aiosqlite").Row
        async with conn.execute("SELECT DISTINCT user_id FROM tracked_locations") as cur:
            uids = [r["user_id"] for r in await cur.fetchall()]

    weights = await db.load_latest_weights() or DEFAULT_WEIGHTS

    for uid in uids:
        locs = await db.get_tracked(uid)
        for loc in locs:
            try:
                pred = await weather_service.ensemble_prediction(
                    loc["latitude"], loc["longitude"], loc["timezone"], weights
                )
                if pred:
                    text = (
                        f"🔔 <b>Auto-Refresh</b>\n"
                        + format_prediction(pred, loc["display_name"] or loc["name"])
                    )
                    await ctx.bot.send_message(
                        uid, text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=kb.prediction_actions(
                            loc["latitude"], loc["longitude"],
                            loc["timezone"], loc["name"],
                        ),
                    )
            except Exception as e:
                log.warning("Auto-refresh error for %s: %s", loc["name"], e)


# ─────────────────────────────────────────────────────────
#  Application setup
# ─────────────────────────────────────────────────────────
def build_application() -> Application:
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    # Conversation handler wires all multi-step interactions together
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CommandHandler("menu",  cmd_menu),
            CallbackQueryHandler(menu_router),
        ],
        states={
            AWAIT_LOCATION_PREDICT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_location_input),
                CallbackQueryHandler(menu_router),
            ],
            AWAIT_LOCATION_TRACK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_location_input),
                CallbackQueryHandler(menu_router),
            ],
            AWAIT_POS_OUTCOME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pos_outcome),
            ],
            AWAIT_POS_SHARES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pos_shares),
            ],
            AWAIT_POS_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pos_price),
            ],
            AWAIT_POS_NOTES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pos_notes),
            ],
            AWAIT_UPDATE_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_update_price),
            ],
            AWAIT_CLOSE_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_close_price),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_message=False,
    )
    app.add_handler(conv)

    # Background job
    app.job_queue.run_repeating(
        job_refresh_tracked,
        interval=TRACKED_REFRESH_MIN * 60,
        first=10,
    )

    return app


# ─────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────
async def post_init(app: Application) -> None:
    await db.init_db()
    log.info("Database initialised ✅")
    # Load or run first backtest
    weights = await db.load_latest_weights()
    if weights:
        log.info("Loaded cached weights: %s", weights)
    else:
        log.info("No cached weights — running initial backtest…")
        try:
            bt = await backtest_service.run_backtest()
            log.info("Initial backtest complete. Weights: %s", bt["weights"])
        except Exception as e:
            log.warning("Initial backtest failed (will use defaults): %s", e)


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in .env")

    app = build_application()
    app.post_init = post_init

    log.info("WeatherPolyBot starting… 🌦️")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
