"""SQLite tracking store: who is tracking what, with the last seen forecast.

Two tables:
  - `tracking`   — airport-level tracking (existing)
  - `positions`  — Polymarket position-level tracking (new in v9). Each row
    is (user, city, target_date, bucket-low, bucket-high, last alert state).
"""
import sqlite3
from datetime import datetime
from typing import List, Optional, Tuple


class TrackingDB:
    def __init__(self, path: str) -> None:
        self.path = path
        self._init()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path)
        c.execute("PRAGMA journal_mode=WAL;")
        return c

    def _init(self) -> None:
        with self._conn() as c:
            # Existing airport tracking table
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS tracking (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    INTEGER NOT NULL,
                    chat_id    INTEGER NOT NULL,
                    airport    TEXT    NOT NULL,
                    last_c     REAL,
                    last_f     REAL,
                    last_check TEXT,
                    created_at TEXT    NOT NULL,
                    UNIQUE(user_id, airport)
                )
                """
            )
            cols = {row[1] for row in c.execute("PRAGMA table_info(tracking)")}
            if "last_bucket" not in cols:
                c.execute("ALTER TABLE tracking ADD COLUMN last_bucket TEXT")

            # New positions table for Polymarket position tracking
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id        INTEGER NOT NULL,
                    chat_id        INTEGER NOT NULL,
                    city_key       TEXT NOT NULL,
                    target_date    TEXT NOT NULL,         -- ISO 'YYYY-MM-DD'
                    bucket_kind    TEXT NOT NULL,         -- 'exact'|'range'|'gte'|'lte'
                    bucket_low     INTEGER NOT NULL,      -- in market unit
                    bucket_high    INTEGER NOT NULL,      -- == low for non-range
                    bucket_label   TEXT NOT NULL,         -- display label
                    market_unit    TEXT NOT NULL,         -- 'C' or 'F'
                    last_alert_at  TEXT,                  -- ISO timestamp of last alert
                    last_alert_p   REAL,                  -- last P(miss) we alerted on
                    created_at     TEXT NOT NULL,
                    UNIQUE(user_id, city_key, target_date, bucket_low, bucket_high)
                )
                """
            )

    # ── airport tracking (existing) ──
    def add(self, user_id: int, chat_id: int, airport: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO tracking "
                "(user_id, chat_id, airport, created_at) VALUES (?,?,?,?)",
                (user_id, chat_id, airport.upper(), datetime.utcnow().isoformat()),
            )
            return cur.rowcount > 0

    def remove(self, user_id: int, airport: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM tracking WHERE user_id=? AND airport=?",
                (user_id, airport.upper()),
            )
            return cur.rowcount > 0

    def list_user(
        self, user_id: int
    ) -> List[Tuple[str, Optional[float], Optional[float], Optional[str]]]:
        with self._conn() as c:
            return c.execute(
                "SELECT airport, last_c, last_f, last_check "
                "FROM tracking WHERE user_id=? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()

    def list_all(
        self,
    ) -> List[Tuple[int, int, int, str, Optional[float], Optional[float], Optional[str]]]:
        with self._conn() as c:
            return c.execute(
                "SELECT id, user_id, chat_id, airport, last_c, last_f, last_bucket "
                "FROM tracking"
            ).fetchall()

    def update_last(
        self,
        row_id: int,
        temp_c: float,
        temp_f: float,
        bucket: Optional[str] = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE tracking "
                "SET last_c=?, last_f=?, last_bucket=?, last_check=? WHERE id=?",
                (temp_c, temp_f, bucket, datetime.utcnow().isoformat(), row_id),
            )

    # ── position tracking (new in v9) ──
    def add_position(
        self, user_id: int, chat_id: int,
        city_key: str, target_date: str,
        bucket_kind: str, bucket_low: int, bucket_high: int,
        bucket_label: str, market_unit: str,
    ) -> bool:
        with self._conn() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO positions
                   (user_id, chat_id, city_key, target_date,
                    bucket_kind, bucket_low, bucket_high, bucket_label,
                    market_unit, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (user_id, chat_id, city_key, target_date,
                 bucket_kind, bucket_low, bucket_high, bucket_label,
                 market_unit, datetime.utcnow().isoformat()),
            )
            return cur.rowcount > 0

    def remove_position(self, user_id: int, position_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM positions WHERE id=? AND user_id=?",
                (position_id, user_id),
            )
            return cur.rowcount > 0

    def list_user_positions(self, user_id: int) -> List[Tuple]:
        with self._conn() as c:
            return c.execute(
                """SELECT id, city_key, target_date, bucket_kind,
                          bucket_low, bucket_high, bucket_label, market_unit,
                          last_alert_at, last_alert_p
                   FROM positions WHERE user_id=?
                   ORDER BY target_date, created_at""",
                (user_id,),
            ).fetchall()

    def list_all_positions(self) -> List[Tuple]:
        with self._conn() as c:
            return c.execute(
                """SELECT id, user_id, chat_id, city_key, target_date,
                          bucket_kind, bucket_low, bucket_high, bucket_label,
                          market_unit, last_alert_at, last_alert_p
                   FROM positions"""
            ).fetchall()

    def purge_expired_positions(self, today_iso: str) -> int:
        """Remove positions whose target_date is before today (any timezone)."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM positions WHERE target_date < ?",
                (today_iso,),
            )
            return cur.rowcount

    def update_position_alert(
        self, position_id: int, p_miss: float
    ) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE positions SET last_alert_at=?, last_alert_p=? WHERE id=?",
                (datetime.utcnow().isoformat(), p_miss, position_id),
            )
