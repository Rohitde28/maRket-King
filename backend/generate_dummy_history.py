"""
generate_dummy_history.py
Generates 5 dummy signals from the past week for UI testing when the market is closed.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from trade_log import log_signal, log_trade, _get_conn, init_db

def generate():
    init_db()
    
    # Check if we already have data
    with _get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        if count > 0:
            print("Database already has signals. Clearing them...")
            conn.execute("DELETE FROM signals")
            conn.execute("DELETE FROM trades")
            conn.commit()

    now = datetime.now(timezone.utc)
    
    # 5 dummy signals over the last 5 weekdays
    dummies = [
        {"days_ago": 5, "dir": "long",  "sig": "ENTER",   "prob": 0.88, "outcome": "TP2"},
        {"days_ago": 4, "dir": "short", "sig": "CAUTION", "prob": 0.65, "outcome": "SL"},
        {"days_ago": 3, "dir": "long",  "sig": "ENTER",   "prob": 0.85, "outcome": "TP1"},
        {"days_ago": 2, "dir": "short", "sig": "ENTER",   "prob": 0.89, "outcome": "TP2"},
        {"days_ago": 1, "dir": "long",  "sig": "CAUTION", "prob": 0.70, "outcome": "OPEN"},
    ]

    for d in dummies:
        dt = now - timedelta(days=d["days_ago"])
        # Set to approx NY open time
        dt = dt.replace(hour=13, minute=45, second=0, microsecond=0)
        
        entry = 2350.00
        risk  = 5.0
        sl    = entry - risk if d["dir"] == "long" else entry + risk
        tp1   = entry + risk * 2 if d["dir"] == "long" else entry - risk * 2
        tp2   = entry + risk * 3 if d["dir"] == "long" else entry - risk * 3
        
        sig_id = log_signal({
            "timestamp": dt.isoformat(),
            "symbol": "XAUUSD(dummy)",
            "timeframe": "5m",
            "signal_strength": d["sig"],
            "direction": d["dir"],
            "factors_hit": 4 if d["sig"] == "ENTER" else 3,
            "final_probability": d["prob"],
            "base_probability": d["prob"] - 0.05,
            "ml_adjustment": 0.05,
            "entry_price": entry,
            "stop_loss": sl,
            "tp1": tp1,
            "tp2": tp2,
            "atr": 2.5,
            "fvg_count": 2,
            "prior_trend": "bullish" if d["dir"] == "long" else "bearish",
        })
        
        # update outcome directly manually
        with _get_conn() as conn:
            conn.execute("UPDATE signals SET outcome=? WHERE id=?", (d["outcome"], sig_id))
            
    print(f"Generated {len(dummies)} dummy signals in trades.db for UI testing.")

if __name__ == "__main__":
    generate()
