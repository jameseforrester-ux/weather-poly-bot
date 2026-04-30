"""
keyboards.py — All InlineKeyboardMarkup layouts
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


# ─────────────────────────────────────────────────────────
#  Main menu
# ─────────────────────────────────────────────────────────
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌡️ Predict Temperature",  callback_data="menu_predict"),
            InlineKeyboardButton("📡 Track Location",       callback_data="menu_track"),
        ],
        [
            InlineKeyboardButton("🎯 Market Scanner",       callback_data="menu_markets"),
            InlineKeyboardButton("⭐ Favourites",            callback_data="menu_favourites"),
        ],
        [
            InlineKeyboardButton("💼 My Positions",         callback_data="menu_positions"),
            InlineKeyboardButton("📊 Backtest Results",     callback_data="menu_backtest"),
        ],
        [
            InlineKeyboardButton("🔄 Run New Backtest",     callback_data="menu_run_backtest"),
            InlineKeyboardButton("ℹ️ Help",                  callback_data="menu_help"),
        ],
    ])


def back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🏠 Main Menu", callback_data="menu_main"),
    ]])


def back_and_refresh(refresh_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Refresh",  callback_data=refresh_cb),
        InlineKeyboardButton("🏠 Menu",     callback_data="menu_main"),
    ]])


# ─────────────────────────────────────────────────────────
#  Prediction result
# ─────────────────────────────────────────────────────────
def prediction_actions(lat: float, lon: float, tz: str,
                       location: str) -> InlineKeyboardMarkup:
    loc_enc = location.replace(" ", "_")[:40]
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎯 Scan Markets",
                                 callback_data=f"scan|{lat}|{lon}|{tz}|{loc_enc}"),
            InlineKeyboardButton("📡 Track this location",
                                 callback_data=f"track|{lat}|{lon}|{tz}|{loc_enc}"),
        ],
        [
            InlineKeyboardButton("🔄 Refresh",
                                 callback_data=f"predict|{lat}|{lon}|{tz}|{loc_enc}"),
            InlineKeyboardButton("🏠 Menu",       callback_data="menu_main"),
        ],
    ])


# ─────────────────────────────────────────────────────────
#  Market scanner
# ─────────────────────────────────────────────────────────
def market_actions(market_id: str, market_url: str,
                   lat: float, lon: float,
                   pred_c: float) -> InlineKeyboardMarkup:
    mid_safe = market_id[:40]
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔗 Open Market",  url=market_url),
        ],
        [
            InlineKeyboardButton("💼 Add Position",
                                 callback_data=f"addpos|{mid_safe}|{lat}|{lon}|{pred_c}"),
            InlineKeyboardButton("⭐ Add to Favs",
                                 callback_data=f"addfav|{mid_safe}"),
        ],
        [
            InlineKeyboardButton("🔙 Back to Results", callback_data="back_markets"),
            InlineKeyboardButton("🏠 Menu",             callback_data="menu_main"),
        ],
    ])


# ─────────────────────────────────────────────────────────
#  Positions
# ─────────────────────────────────────────────────────────
def position_list(positions: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for p in positions[:8]:          # max 8 buttons
        label = f"#{p['id']} {p['outcome'][:20]}"
        rows.append([
            InlineKeyboardButton(label, callback_data=f"pos_detail|{p['id']}"),
        ])
    rows.append([
        InlineKeyboardButton("➕ New Position", callback_data="menu_predict"),
        InlineKeyboardButton("🏠 Menu",          callback_data="menu_main"),
    ])
    return InlineKeyboardMarkup(rows)


def position_detail(pos_id: int, market_url: str | None) -> InlineKeyboardMarkup:
    rows = []
    if market_url:
        rows.append([InlineKeyboardButton("🔗 Open Market", url=market_url)])
    rows.append([
        InlineKeyboardButton("🔄 Update Price", callback_data=f"pos_update|{pos_id}"),
        InlineKeyboardButton("✅ Close",         callback_data=f"pos_close|{pos_id}"),
    ])
    rows.append([
        InlineKeyboardButton("🗑️ Delete",        callback_data=f"pos_delete|{pos_id}"),
        InlineKeyboardButton("🔙 Positions",     callback_data="menu_positions"),
    ])
    return InlineKeyboardMarkup(rows)


# ─────────────────────────────────────────────────────────
#  Tracked locations
# ─────────────────────────────────────────────────────────
def tracked_list(locations: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for loc in locations[:8]:
        label = f"📍 {loc['display_name'] or loc['name']}"
        rows.append([
            InlineKeyboardButton(label,
                                 callback_data=f"predict|{loc['latitude']}|{loc['longitude']}|{loc['timezone']}|{loc['name'][:30]}"),
            InlineKeyboardButton("🗑️", callback_data=f"untrack|{loc['id']}"),
        ])
    rows.append([
        InlineKeyboardButton("➕ Track New", callback_data="menu_track"),
        InlineKeyboardButton("🏠 Menu",       callback_data="menu_main"),
    ])
    return InlineKeyboardMarkup(rows)


# ─────────────────────────────────────────────────────────
#  Favourites
# ─────────────────────────────────────────────────────────
def favourites_list(favs: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for fav in favs[:8]:
        label = f"⭐ {fav['market_name'][:30]}"
        rows.append([
            InlineKeyboardButton(label,     url=fav["market_url"]),
            InlineKeyboardButton("🔄",      callback_data=f"fav_refresh|{fav['id']}|{fav['market_id']}"),
            InlineKeyboardButton("🗑️",      callback_data=f"fav_remove|{fav['id']}"),
        ])
    rows.append([
        InlineKeyboardButton("🏠 Menu", callback_data="menu_main"),
    ])
    return InlineKeyboardMarkup(rows)


# ─────────────────────────────────────────────────────────
#  Confirm dialogs
# ─────────────────────────────────────────────────────────
def confirm(yes_cb: str, no_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data=yes_cb),
        InlineKeyboardButton("❌ No",  callback_data=no_cb),
    ]])
