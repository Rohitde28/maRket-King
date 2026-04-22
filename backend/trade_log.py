"""
trade_log.py
============
Persistent trade journal using SQLite (no external server needed).
Stores all signals, logged trades, and TP/SL hit outcomes.
"""

import os
import sqlite3
import json
import logging
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "data" / "trades.db"


def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist."""
    with _get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS signals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT NOT NULL,
            symbol          TEXT NOT NULL,
            timeframe       TEXT NOT NULL,
            signal_strength TEXT NOT NULL,
            direction       TEXT,
            factors_hit     INTEGER,
            final_probability REAL,
            base_probability  REAL,
            ml_adjustment     REAL,
            entry_price     REAL,
            stop_loss       REAL,
            tp1             REAL,
            tp2             REAL,
            atr             REAL,
            fvg_count       INTEGER,
            prior_trend     TEXT,
            factors_json    TEXT,
            notes_json      TEXT,
            key_levels_json TEXT,
            outcome         TEXT DEFAULT 'OPEN',
            exit_price      REAL,
            exit_reason     TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id       INTEGER REFERENCES signals(id),
            symbol          TEXT NOT NULL,
            timeframe       TEXT NOT NULL,
            direction       TEXT NOT NULL,
            entry_price     REAL NOT NULL,
            stop_loss       REAL NOT NULL,
            tp1             REAL NOT NULL,
            tp2             REAL NOT NULL,
            risk_usd        REAL,
            contracts       REAL,
            outcome         TEXT DEFAULT 'OPEN',
            exit_price      REAL,
            exit_reason     TEXT,
            r_multiple      REAL,
            pnl_usd         REAL,
            notes           TEXT,
            opened_at       TEXT NOT NULL,
            closed_at       TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_signals_ts  ON signals(timestamp);
        CREATE INDEX IF NOT EXISTS idx_trades_sym  ON trades(symbol);
        """)

        # Run migrations for older DBs (ADD COLUMN is idempotent with try/except)
        for migration in [
            "ALTER TABLE signals ADD COLUMN outcome TEXT DEFAULT 'OPEN'",
            "ALTER TABLE signals ADD COLUMN exit_price REAL",
            "ALTER TABLE signals ADD COLUMN base_probability REAL",
            "ALTER TABLE signals ADD COLUMN ml_adjustment REAL",
            "ALTER TABLE signals ADD COLUMN exit_reason TEXT",
        ]:
            try:
                conn.execute(migration)
            except Exception:
                pass   # Column already exists


# ------------------------------------------------------------------ #
#  Signal Log
# ------------------------------------------------------------------ #
def log_signal(signal: dict) -> int:
    """Persist a signal result. Returns the inserted row id."""
    with _get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO signals
              (timestamp, symbol, timeframe, signal_strength, direction,
               factors_hit, final_probability, base_probability, ml_adjustment,
               entry_price, stop_loss, tp1, tp2, atr, fvg_count, prior_trend,
               factors_json, notes_json, key_levels_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            signal.get("timestamp"),
            signal.get("symbol"),
            signal.get("timeframe"),
            signal.get("signal_strength"),
            signal.get("direction"),
            signal.get("factors_hit"),
            signal.get("final_probability"),
            signal.get("base_probability"),
            signal.get("ml_adjustment"),
            signal.get("entry_price"),
            signal.get("stop_loss"),
            signal.get("tp1"),
            signal.get("tp2"),
            signal.get("atr"),
            signal.get("fvg_count"),
            signal.get("prior_trend"),
            json.dumps(signal.get("factors", [])),
            json.dumps(signal.get("notes", [])),
            json.dumps(signal.get("key_levels", [])),
        ))
        
        # Enforce 100-record cap per symbol/timeframe
        sym = signal.get("symbol")
        tf  = signal.get("timeframe")
        conn.execute("""
            DELETE FROM signals 
            WHERE symbol = ? AND timeframe = ? AND id NOT IN (
                SELECT id FROM signals 
                WHERE symbol = ? AND timeframe = ? 
                ORDER BY timestamp DESC LIMIT 100
            )
        """, (sym, tf, sym, tf))

        return cur.lastrowid


# ------------------------------------------------------------------ #
#  Trade Log
# ------------------------------------------------------------------ #
def log_trade(trade: Dict[str, Any]) -> int:
    """Log a new trade entry. Returns trade id."""
    with _get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO trades
              (signal_id, symbol, timeframe, direction, entry_price,
               stop_loss, tp1, tp2, risk_usd, contracts, notes, opened_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            trade.get("signal_id"),
            trade.get("symbol"),
            trade.get("timeframe"),
            trade.get("direction"),
            trade.get("entry_price"),
            trade.get("stop_loss"),
            trade.get("tp1"),
            trade.get("tp2"),
            trade.get("risk_usd", 500),
            trade.get("contracts"),
            trade.get("notes", ""),
            datetime.now(timezone.utc).isoformat(),
        ))
        return cur.lastrowid


def close_trade(trade_id: int, exit_price: float,
                exit_reason: str, entry_price: float,
                stop_loss: float, direction: str,
                risk_usd: float = 500) -> Dict[str, Any]:
    """Mark a trade as closed, calculate R-multiple and P&L."""
    risk_pts = abs(entry_price - stop_loss)
    if risk_pts == 0:
        r_multiple = 0.0
    elif direction == "long":
        r_multiple = round((exit_price - entry_price) / risk_pts, 2)
    else:
        r_multiple = round((entry_price - exit_price) / risk_pts, 2)

    pnl_usd = round(r_multiple * risk_usd, 2)
    outcome = "WIN" if r_multiple > 0 else ("LOSS" if r_multiple < 0 else "BREAKEVEN")

    with _get_conn() as conn:
        conn.execute("""
            UPDATE trades
            SET outcome=?, exit_price=?, exit_reason=?,
                r_multiple=?, pnl_usd=?, closed_at=?
            WHERE id=?
        """, (outcome, exit_price, exit_reason, r_multiple, pnl_usd,
              datetime.now(timezone.utc).isoformat(), trade_id))

    return {"outcome": outcome, "r_multiple": r_multiple, "pnl_usd": pnl_usd}


# ------------------------------------------------------------------ #
#  Queries
# ------------------------------------------------------------------ #
def get_trades(limit: int = 100) -> List[Dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY opened_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_signals(limit: int = 50) -> List[Dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["factors"]    = json.loads(d.get("factors_json") or "[]")
            d["notes"]      = json.loads(d.get("notes_json") or "[]")
            d["key_levels"] = json.loads(d.get("key_levels_json") or "[]")
            result.append(d)
        return result


def get_stats() -> dict:
    """Aggregate performance statistics from both signals and trades tables."""
    with _get_conn() as conn:
        # From trades table (manually logged / auto-closed)
        total     = conn.execute("SELECT COUNT(*) FROM trades WHERE outcome != 'OPEN'").fetchone()[0]
        wins      = conn.execute("SELECT COUNT(*) FROM trades WHERE outcome='WIN'").fetchone()[0]
        losses    = conn.execute("SELECT COUNT(*) FROM trades WHERE outcome='LOSS'").fetchone()[0]
        avg_r     = conn.execute("SELECT AVG(r_multiple) FROM trades WHERE outcome != 'OPEN'").fetchone()[0]
        total_pnl = conn.execute("SELECT SUM(pnl_usd) FROM trades").fetchone()[0]
        tp1_hits  = conn.execute("SELECT COUNT(*) FROM trades WHERE exit_reason='TP1'").fetchone()[0]
        tp2_hits  = conn.execute("SELECT COUNT(*) FROM trades WHERE exit_reason='TP2'").fetchone()[0]
        sl_hits   = conn.execute("SELECT COUNT(*) FROM trades WHERE exit_reason='SL'").fetchone()[0]

        # From signals table (auto-monitored outcomes)
        sig_total = conn.execute("SELECT COUNT(*) FROM signals WHERE outcome != 'OPEN'").fetchone()[0]
        sig_tp1   = conn.execute("SELECT COUNT(*) FROM signals WHERE outcome='TP1'").fetchone()[0]
        sig_tp2   = conn.execute("SELECT COUNT(*) FROM signals WHERE outcome='TP2'").fetchone()[0]
        sig_sl    = conn.execute("SELECT COUNT(*) FROM signals WHERE outcome='SL'").fetchone()[0]
        sig_open  = conn.execute("SELECT COUNT(*) FROM signals WHERE outcome='OPEN'").fetchone()[0]
        sig_wins  = sig_tp1 + sig_tp2
        sig_losses= sig_sl
        sig_wr    = round(sig_wins / sig_total * 100, 1) if sig_total > 0 else 0

        return {
            # Trades table (manual / auto-close trades)
            "total_trades":    total,
            "wins":            wins,
            "losses":          losses,
            "win_rate":        round(wins / total * 100, 1) if total > 0 else 0,
            "avg_r_multiple":  round(avg_r or 0, 2),
            "total_pnl_usd":   round(total_pnl or 0, 2),
            "tp1_hits":        tp1_hits,
            "tp2_hits":        tp2_hits,
            "sl_hits":         sl_hits,
            # Signals table (auto-monitored live signals)
            "signals_total":   conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0],
            "signals_resolved":sig_total,
            "signals_open":    sig_open,
            "signals_tp1":     sig_tp1,
            "signals_tp2":     sig_tp2,
            "signals_sl":      sig_sl,
            "signals_win_rate":sig_wr,
        }
