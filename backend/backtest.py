"""
backtest.py
===========
ORR Strategy Historical Backtester
====================================
Runs the ORR analysis engine on historical OHLCV data using a
rolling window to simulate real-time signal detection.

HOW IT WORKS
------------
1. Fetches historical data from Yahoo Finance (or any configured source)
2. For each bar in the data, runs orr_analyzer.analyze() on all preceding bars
   (exactly how the live engine sees data — no look-ahead bias)
3. When a signal fires (ENTER / CAUTION), simulates a trade:
   - Entry at close of signal bar
   - SL and TP calculated by the analyzer
   - Outcome determined by subsequent bars
4. Prints a full performance report

USAGE
-----
From the backend directory:
    python backtest.py

Options (set as env vars or edit the CONFIG section below):
    BACKTEST_TIMEFRAME=5m        # Timeframe to test
    BACKTEST_BARS=500            # Total bars to fetch
    BACKTEST_WARMUP=50           # Bars needed before first signal check
    BACKTEST_MIN_SIGNAL=ENTER    # ENTER | CAUTION (min signal level to trade)
    BACKTEST_RISK_USD=500        # Simulated risk per trade in USD

NOTE: Factor 2 (timing window) is NOT applied in backtesting because
historical bars all have fixed timestamps — the engine tests all 4 factors
but shows Factor 2 status as informational only. Set BACKTEST_SKIP_TIMING=true
to fully ignore it (default), or false to require it.
"""

import os
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

# Make sure we can import from the backend package
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import numpy as np
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────────────────────────── #
#  CONFIG — edit here or set as env vars
# ─────────────────────────────────────────────────────────────────── #
BACKTEST_TIMEFRAME  = os.getenv("BACKTEST_TIMEFRAME",  "5m")
BACKTEST_BARS       = int(os.getenv("BACKTEST_BARS",   "500"))
BACKTEST_WARMUP     = int(os.getenv("BACKTEST_WARMUP", "50"))
MIN_SIGNAL          = os.getenv("BACKTEST_MIN_SIGNAL", "ENTER")   # ENTER | CAUTION
RISK_USD            = float(os.getenv("BACKTEST_RISK_USD", "500"))
SKIP_TIMING         = os.getenv("BACKTEST_SKIP_TIMING", "true").lower() == "true"

# ─────────────────────────────────────────────────────────────────── #
from data_fetcher import fetch_ohlcv, get_symbol_display
from strategy.orr_analyzer import analyze, TIMING_WINDOWS_ET
from strategy.indicators import compute_atr


# ─────────────────────────────────────────────────────────────────── #
#  Patch: override timing window during backtest
# ─────────────────────────────────────────────────────────────────── #
import strategy.orr_analyzer as _orr_mod

_original_is_in_window = _orr_mod._is_in_timing_window

def _backtest_timing_override():
    """
    During backtesting we check if the historical bar's timestamp
    falls in a NY timing window — this is more accurate than using
    the clock at the time of running the backtest.
    """
    return _current_bar_in_window

_current_bar_in_window = False   # set per-bar in the backtest loop


# ─────────────────────────────────────────────────────────────────── #
#  Helpers
# ─────────────────────────────────────────────────────────────────── #
def is_bar_in_timing_window(bar_ts: pd.Timestamp) -> bool:
    """Check if a historical bar's timestamp falls in an NY timing window."""
    try:
        import pytz
        # Ensure bar_ts is timezone-aware before converting
        if bar_ts.tzinfo is None:
            bar_ts = bar_ts.tz_localize("UTC")
        et = bar_ts.tz_convert(pytz.timezone("America/New_York"))
        et_total = et.hour * 60 + et.minute
    except Exception:
        # Fallback to rough UTC calculation if pytz fails
        month      = bar_ts.month
        utc_offset = -4 if 3 <= month <= 10 else -5
        et_h = (bar_ts.hour + utc_offset) % 24
        et_m = bar_ts.minute
        et_total = et_h * 60 + et_m

    for (h0, m0, h1, m1) in TIMING_WINDOWS_ET:
        if (h0 * 60 + m0) <= et_total <= (h1 * 60 + m1):
            return True
    return False


def simulate_trade_outcome(df: pd.DataFrame, signal_i: int,
                            entry: float, sl: float, tp1: float, tp2: float,
                            direction: str) -> dict:
    """
    Look forward from signal bar to see if SL or TP is hit first.

    Returns dict with:
        outcome     : TP2 | TP1 | SL | TIMEOUT (held till end of data)
        exit_price  : float
        bars_held   : int
        r_multiple  : float
    """
    future = df.iloc[signal_i + 1:]
    risk   = abs(entry - sl)
    if risk == 0:
        return {"outcome": "SKIP", "r_multiple": 0.0, "bars_held": 0, "exit_price": entry}

    for i, (ts, row) in enumerate(future.iterrows()):
        hi, lo = row["high"], row["low"]

        if direction == "long":
            if lo <= sl:
                return {"outcome": "SL",  "exit_price": sl,  "bars_held": i + 1,
                        "r_multiple": round(-1.0, 2)}
            if hi >= tp2:
                return {"outcome": "TP2", "exit_price": tp2, "bars_held": i + 1,
                        "r_multiple": round((tp2 - entry) / risk, 2)}
            if hi >= tp1:
                return {"outcome": "TP1", "exit_price": tp1, "bars_held": i + 1,
                        "r_multiple": round((tp1 - entry) / risk, 2)}
        else:  # short
            if hi >= sl:
                return {"outcome": "SL",  "exit_price": sl,  "bars_held": i + 1,
                        "r_multiple": round(-1.0, 2)}
            if lo <= tp2:
                return {"outcome": "TP2", "exit_price": tp2, "bars_held": i + 1,
                        "r_multiple": round((entry - tp2) / risk, 2)}
            if lo <= tp1:
                return {"outcome": "TP1", "exit_price": tp1, "bars_held": i + 1,
                        "r_multiple": round((entry - tp1) / risk, 2)}

    last_close = float(future["close"].iloc[-1]) if not future.empty else entry
    r = (last_close - entry) / risk if direction == "long" else (entry - last_close) / risk
    return {"outcome": "TIMEOUT", "exit_price": last_close,
            "bars_held": len(future), "r_multiple": round(r, 2)}


# ─────────────────────────────────────────────────────────────────── #
#  Main Backtest Loop
# ─────────────────────────────────────────────────────────────────── #
def run_backtest():
    symbol = get_symbol_display()
    print(f"\n{'='*62}")
    print(f"  ORR Strategy Backtester")
    print(f"  Symbol    : {symbol}")
    print(f"  Timeframe : {BACKTEST_TIMEFRAME}  |  Bars: {BACKTEST_BARS}")
    print(f"  Min Signal: {MIN_SIGNAL}  |  Skip Timing: {SKIP_TIMING}")
    print(f"  Simulated Risk per trade: ${RISK_USD}")
    print(f"{'='*62}\n")

    print("Fetching historical data...")
    df = fetch_ohlcv(BACKTEST_TIMEFRAME, BACKTEST_BARS)
    if df.empty or len(df) < BACKTEST_WARMUP + 10:
        print("ERROR: Not enough data returned. Try a longer period or different timeframe.")
        return

    print(f"Got {len(df)} bars: {df.index[0]} → {df.index[-1]}\n")

    # Patch timing function for backtesting
    if SKIP_TIMING:
        _orr_mod._is_in_timing_window = lambda: True   # always pass Factor 2
    else:
        _orr_mod._is_in_timing_window = _backtest_timing_override

    trades    = []
    signals   = []
    active    = False   # don't stack trades

    VALID_SIGNALS = {"ENTER"} if MIN_SIGNAL == "ENTER" else {"ENTER", "CAUTION"}

    for i in range(BACKTEST_WARMUP, len(df) - 5):
        if active:
            continue   # one trade at a time

        window = df.iloc[max(0, i - 200):i + 1]
        bar_ts = df.index[i]

        # Update timing override for this bar
        global _current_bar_in_window
        _current_bar_in_window = is_bar_in_timing_window(bar_ts)

        try:
            sig = analyze(window, timeframe=BACKTEST_TIMEFRAME, symbol=symbol)
        except Exception as e:
            continue

        if sig.signal_strength not in VALID_SIGNALS:
            continue
        if not sig.entry_price or not sig.stop_loss or not sig.tp1 or not sig.tp2:
            continue

        # Log signal
        bar_time_ist = ""
        try:
            import pytz
            bar_time_ist = bar_ts.tz_convert(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M IST")
        except Exception:
            bar_time_ist = str(bar_ts)

        signals.append({
            "bar_index":  i,
            "bar_time":   bar_time_ist,
            "signal":     sig.signal_strength,
            "direction":  sig.direction,
            "factors":    sig.factors_hit,
            "prob":       sig.final_probability,
            "entry":      sig.entry_price,
            "sl":         sig.stop_loss,
            "tp1":        sig.tp1,
            "tp2":        sig.tp2,
        })

        # Simulate trade
        result = simulate_trade_outcome(
            df, i, sig.entry_price, sig.stop_loss,
            sig.tp1, sig.tp2, sig.direction
        )

        pnl = round(result["r_multiple"] * RISK_USD, 2)
        trade = {**signals[-1], **result, "pnl_usd": pnl}
        trades.append(trade)

        print(
            f"  [{bar_time_ist}] {sig.signal_strength:7s} {sig.direction:5s} "
            f"| F:{sig.factors_hit}/4  P:{sig.final_probability:.0%} "
            f"| Entry:{sig.entry_price:.2f} SL:{sig.stop_loss:.2f} "
            f"TP1:{sig.tp1:.2f} TP2:{sig.tp2:.2f} "
            f"→ {result['outcome']:7s} {result['r_multiple']:+.2f}R  ${pnl:+.2f}"
        )

        active = False   # allow next signal (set to True for "one at a time" mode)

    # Restore original timing function
    _orr_mod._is_in_timing_window = _original_is_in_window

    # ── Performance Report ─────────────────────────────────────────
    print(f"\n{'='*62}")
    print("  PERFORMANCE REPORT")
    print(f"{'='*62}")

    if not trades:
        print("  No trades triggered. Try CAUTION level or loosen thresholds in .env")
        return

    tdf = pd.DataFrame(trades)
    total   = len(tdf)
    wins    = len(tdf[tdf["r_multiple"] > 0])
    losses  = len(tdf[tdf["r_multiple"] < 0])
    be      = len(tdf[tdf["r_multiple"] == 0])
    tp2h    = len(tdf[tdf["outcome"] == "TP2"])
    tp1h    = len(tdf[tdf["outcome"] == "TP1"])
    slh     = len(tdf[tdf["outcome"] == "SL"])
    avg_r   = tdf["r_multiple"].mean()
    total_pnl = tdf["pnl_usd"].sum()
    max_win = tdf["pnl_usd"].max()
    max_loss= tdf["pnl_usd"].min()
    avg_bars= tdf["bars_held"].mean()

    # Expectancy
    wr = wins / total if total > 0 else 0
    lr = losses / total if total > 0 else 0
    avg_win  = tdf[tdf["r_multiple"] > 0]["r_multiple"].mean() if wins > 0 else 0
    avg_loss = abs(tdf[tdf["r_multiple"] < 0]["r_multiple"].mean()) if losses > 0 else 0
    expectancy = (wr * avg_win) - (lr * avg_loss)

    print(f"  Signals fired  : {total}")
    print(f"  Win rate       : {wr:.1%}  ({wins}W / {losses}L / {be}BE)")
    print(f"  TP2 hits       : {tp2h}  ({tp2h/total:.1%})")
    print(f"  TP1 hits       : {tp1h}  ({tp1h/total:.1%})")
    print(f"  SL hits        : {slh}  ({slh/total:.1%})")
    print(f"  Avg R/trade    : {avg_r:+.2f}R")
    print(f"  Expectancy     : {expectancy:+.3f}R per trade")
    print(f"  Avg bars held  : {avg_bars:.1f}")
    print(f"  Total P&L      : ${total_pnl:+,.2f}  (@ ${RISK_USD} risk/trade)")
    print(f"  Best trade     : ${max_win:+,.2f}")
    print(f"  Worst trade    : ${max_loss:+,.2f}")

    # By signal strength
    print(f"\n  Breakdown by signal strength:")
    for strength in ["ENTER", "CAUTION"]:
        sub = tdf[tdf["signal"] == strength]
        if len(sub) > 0:
            sw = len(sub[sub["r_multiple"] > 0])
            print(f"    {strength:7s}: {len(sub)} trades  |  WR {sw/len(sub):.1%}  |  AvgR {sub['r_multiple'].mean():+.2f}")

    # Save results
    out_path = Path(__file__).parent / "backtest_results.json"
    with open(out_path, "w") as f:
        json.dump({
            "config": {
                "symbol":    symbol,
                "timeframe": BACKTEST_TIMEFRAME,
                "bars":      BACKTEST_BARS,
                "warmup":    BACKTEST_WARMUP,
                "min_signal":MIN_SIGNAL,
                "risk_usd":  RISK_USD,
                "skip_timing": SKIP_TIMING,
            },
            "summary": {
                "total_trades":  total,
                "win_rate":      round(wr, 4),
                "avg_r":         round(avg_r, 4),
                "expectancy":    round(expectancy, 4),
                "total_pnl_usd": round(total_pnl, 2),
                "tp2_hits":      tp2h,
                "tp1_hits":      tp1h,
                "sl_hits":       slh,
            },
            "trades": trades,
        }, f, indent=2, default=str)

    print(f"\n  Full results saved to: {out_path}")
    print(f"{'='*62}\n")


if __name__ == "__main__":
    run_backtest()
