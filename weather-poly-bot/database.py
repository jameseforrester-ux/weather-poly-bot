"""
database.py — Async SQLite operations (aiosqlite)
"""
import json
import aiosqlite
from datetime import datetime
from config import DB_PATH


# ─────────────────────────────────────────────────────────
#  Schema
# ─────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS tracked_locations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    name          TEXT    NOT NULL,
    latitude      REAL    NOT NULL,
    longitude     REAL    NOT NULL,
    timezone      TEXT    NOT NULL DEFAULT 'UTC',
    display_name  TEXT,
    pm_market_id  TEXT,
    pm_slug       TEXT,
    added_at      TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    location        TEXT    NOT NULL,
    market_id       TEXT,
    market_name     TEXT    NOT NULL,
    market_url      TEXT,
    outcome         TEXT    NOT NULL,
    shares          REAL    NOT NULL,
    entry_price     REAL    NOT NULL,
    current_price   REAL,
    predicted_c     REAL,
    predicted_f     REAL,
    entry_time      TEXT    DEFAULT (datetime('now')),
    closed          INTEGER DEFAULT 0,
    close_price     REAL,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS favorite_markets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    market_id   TEXT    NOT NULL,
    market_name TEXT    NOT NULL,
    market_url  TEXT,
    question    TEXT,
    outcome     TEXT,
    added_at    TEXT    DEFAULT (datetime('now')),
    UNIQUE(user_id, market_id)
);

CREATE TABLE IF NOT EXISTS backtest_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at      TEXT    DEFAULT (datetime('now')),
    location    TEXT    NOT NULL,
    model       TEXT    NOT NULL,
    mae         REAL,
    rmse        REAL,
    bias        REAL,
    acc_1c      REAL,
    acc_2c      REAL,
    n_samples   INTEGER
);

CREATE TABLE IF NOT EXISTS ensemble_weights (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    updated_at  TEXT    DEFAULT (datetime('now')),
    weights_json TEXT   NOT NULL
);
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


# ─────────────────────────────────────────────────────────
#  Tracked locations
# ─────────────────────────────────────────────────────────
async def add_tracked(user_id: int, name: str, lat: float, lon: float,
                      tz: str, display: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO tracked_locations (user_id,name,latitude,longitude,timezone,display_name) "
            "VALUES (?,?,?,?,?,?)",
            (user_id, name, lat, lon, tz, display)
        )
        await db.commit()
        return cur.lastrowid


async def get_tracked(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tracked_locations WHERE user_id=? ORDER BY added_at DESC", (user_id,)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def remove_tracked(user_id: int, loc_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM tracked_locations WHERE id=? AND user_id=?", (loc_id, user_id)
        )
        await db.commit()


async def update_tracked_market(loc_id: int, market_id: str, slug: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tracked_locations SET pm_market_id=?, pm_slug=? WHERE id=?",
            (market_id, slug, loc_id)
        )
        await db.commit()


# ─────────────────────────────────────────────────────────
#  Positions
# ─────────────────────────────────────────────────────────
async def add_position(user_id: int, location: str, market_id: str,
                       market_name: str, market_url: str, outcome: str,
                       shares: float, entry_price: float,
                       pred_c: float, pred_f: float, notes: str = "") -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO positions
               (user_id,location,market_id,market_name,market_url,outcome,
                shares,entry_price,current_price,predicted_c,predicted_f,notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (user_id, location, market_id, market_name, market_url, outcome,
             shares, entry_price, entry_price, pred_c, pred_f, notes)
        )
        await db.commit()
        return cur.lastrowid


async def get_open_positions(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM positions WHERE user_id=? AND closed=0 ORDER BY entry_time DESC",
            (user_id,)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_all_positions(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM positions WHERE user_id=? ORDER BY entry_time DESC", (user_id,)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def update_position_price(pos_id: int, current_price: float) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE positions SET current_price=? WHERE id=?", (current_price, pos_id)
        )
        await db.commit()


async def close_position(pos_id: int, close_price: float) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE positions SET closed=1, close_price=?, current_price=? WHERE id=?",
            (close_price, close_price, pos_id)
        )
        await db.commit()


async def delete_position(user_id: int, pos_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM positions WHERE id=? AND user_id=?", (pos_id, user_id)
        )
        await db.commit()


# ─────────────────────────────────────────────────────────
#  Favorite markets
# ─────────────────────────────────────────────────────────
async def add_favorite(user_id: int, market_id: str, market_name: str,
                       market_url: str, question: str, outcome: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR IGNORE INTO favorite_markets
               (user_id,market_id,market_name,market_url,question,outcome)
               VALUES (?,?,?,?,?,?)""",
            (user_id, market_id, market_name, market_url, question, outcome)
        )
        await db.commit()


async def get_favorites(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM favorite_markets WHERE user_id=? ORDER BY added_at DESC", (user_id,)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def remove_favorite(user_id: int, fav_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM favorite_markets WHERE id=? AND user_id=?", (fav_id, user_id)
        )
        await db.commit()


# ─────────────────────────────────────────────────────────
#  Ensemble weights (persisted after each backtest)
# ─────────────────────────────────────────────────────────
async def save_weights(weights: dict) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO ensemble_weights (weights_json) VALUES (?)",
            (json.dumps(weights),)
        )
        await db.commit()


async def load_latest_weights() -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT weights_json FROM ensemble_weights ORDER BY updated_at DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    if row:
        return json.loads(row[0])
    return None


# ─────────────────────────────────────────────────────────
#  Backtest results
# ─────────────────────────────────────────────────────────
async def save_backtest_rows(rows: list[dict]) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        for r in rows:
            await db.execute(
                """INSERT INTO backtest_results
                   (location,model,mae,rmse,bias,acc_1c,acc_2c,n_samples)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (r["location"], r["model"], r["mae"], r["rmse"],
                 r["bias"], r["acc_1c"], r["acc_2c"], r["n_samples"])
            )
        await db.commit()


async def load_latest_backtest() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Get the most-recent run_at timestamp and return all rows for that run
        async with db.execute(
            "SELECT MAX(run_at) FROM backtest_results"
        ) as cur:
            row = await cur.fetchone()
        if not row or not row[0]:
            return []
        run_at = row[0]
        async with db.execute(
            "SELECT * FROM backtest_results WHERE run_at=?", (run_at,)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]
