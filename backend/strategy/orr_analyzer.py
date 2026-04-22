"""
orr_analyzer.py
===============
Opening Range Reversal — rule-based signal engine.
Strictly aligned to Opening_Range_Reversal_Strategy.txt

4-Factor Checklist (spec §5):
  F1 — Strong Flush into a Key Zone  : fast, large, directional move to pre-marked level
  F2 — Reversal Timing Alignment      : must coincide with 9:45 or 10:00 ET window
  F3 — Trend Structure Break          : HL / LH sequence or H&S pattern visible
  F4 — Momentum Confirmation Candle   : decisive break of prior swing H (long) / L (short)

Signal output:
  ENTER   — ALL 4 factors satisfied (spec: "no exceptions")
  CAUTION — F1 MUST pass + exactly 3/4 total (one of F2/F3/F4 is marginal)
  NO_TRADE — F1 failed OR fewer than 3 factors

Entry   : stop-market at prior swing high (long) / prior swing low (short)  [spec §6]
SL      : below flush extreme low (long) / above flush extreme high (short) [spec §7]
TP1     : entry + 2×risk  [spec §8]
TP2     : entry + 3×risk  [spec §8]
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass, asdict
from typing import List, Optional
from datetime import datetime, timedelta
import pytz

# ── R-multiples (spec §8) ─────────────────────────────────────────
TP1_R = 2.0
TP2_R = 3.0

# ── Timing Windows (ET) — spec §3 ────────────────────────────────
# "The strategy does NOT enter randomly. It waits for specific time windows."
# Window 1: 9:45 ET  (±2 min buffer for bar-level precision)
# Window 2: 10:00 ET (±2 min buffer) — "most reliable"
# Extended: 10:30–12:00 ET — later setups (Gold, etc.)
TIMING_WINDOWS_ET = [
    (9, 43,  9, 47),    # W1 — 9:45 ET  (15 min after open)
    (9, 58, 10,  2),    # W2 — 10:00 ET (30 min after open, STRONGEST)
    (10, 28, 10, 32),   # Extended — 10:30 ET
]

# Exported for main.py throttle-key generation
TIMING_WINDOWS_ET_RAW = TIMING_WINDOWS_ET

ET_TZ = pytz.timezone("America/New_York")


# ── Data classes ──────────────────────────────────────────────────
@dataclass
class FactorResult:
    name:      str
    satisfied: bool
    detail:    str


@dataclass
class ORRSignal:
    timestamp:         str
    timeframe:         str
    symbol:            str
    direction:         str
    factors_hit:       int
    factors:           List[dict]
    base_probability:  float
    ml_adjustment:     float
    final_probability: float
    signal_strength:   str
    entry_price:       Optional[float]
    entry_range:       Optional[dict]
    stop_loss:         Optional[float]
    tp1:               Optional[float]
    tp2:               Optional[float]
    atr:               Optional[float]
    fvg_count:         int
    key_levels:        List[float]
    prior_trend:       str
    notes:             List[str]

    def to_dict(self):
        return asdict(self)


# ── ATR (14-period) ───────────────────────────────────────────────
def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"]  - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(period).mean()


# ── Timing window check (spec §3) ────────────────────────────────
def is_in_timing_window(dt_et=None) -> bool:
    """True if current ET bar timestamp falls inside a defined timing window."""
    if dt_et is None:
        dt_et = datetime.now(pytz.UTC).astimezone(ET_TZ)
    h, m = dt_et.hour, dt_et.minute
    t = h * 60 + m
    for sh, sm, eh, em in TIMING_WINDOWS_ET:
        if (sh * 60 + sm) <= t <= (eh * 60 + em):
            return True
    return False

# alias used by main.py
_is_in_timing_window = is_in_timing_window


def next_window_et() -> Optional[tuple]:
    """Return (start_h, start_m) of next upcoming window, or None if past all."""
    dt_et = datetime.now(pytz.UTC).astimezone(ET_TZ)
    t     = dt_et.hour * 60 + dt_et.minute
    for sh, sm, eh, em in TIMING_WINDOWS_ET:
        if t < sh * 60 + sm:
            return (sh, sm)
    return None


# ── Session-range key levels (spec §4 pre-market prep) ───────────
def _get_session_range(df: pd.DataFrame, df_et, start_h, start_m, end_h, end_m):
    """High/low of a named session slice within the last 24 h."""
    try:
        cutoff = df.index[-1] - timedelta(hours=24)
        bars   = df[df.index >= cutoff]
        b_et   = bars.index.tz_convert(ET_TZ)
        mins   = b_et.hour * 60 + b_et.minute
        s, e   = start_h * 60 + start_m, end_h * 60 + end_m
        mask   = (mins >= s) | (mins <= e) if s > e else (mins >= s) & (mins <= e)
        slc    = bars[mask.values]
        if not slc.empty:
            return float(slc["high"].max()), float(slc["low"].min())
    except Exception:
        pass
    return None, None


# ── FACTOR 1: Strong Flush into Key Zone (spec §5 F1) ────────────
def _check_flush(df: pd.DataFrame, atr: float, last_et):
    """
    Spec: "market must make an aggressive, directional move into a pre-marked
    support or resistance level. The move must be fast and large — not slow or choppy."

    Implementation:
    • Flush = last 3 bars (fast move, not a 5-bar grind)
    • Size  > 1.8× ATR  (aggressive threshold — choppy markets will not fire)
    • Must close back INSIDE the key level (reversal from it, not a continuation)

    Returns: (satisfied, direction, anchor_name, flush_high, flush_low, detail)
    """
    # ── Pre-market key levels (spec §4) ──────────────────────────
    or_h,   or_l   = _get_session_range(df, last_et, 9, 30, 9, 45)   # Opening Range
    lon_h,  lon_l  = _get_session_range(df, last_et, 3,  0, 8, 30)   # London session
    asia_h, asia_l = _get_session_range(df, last_et, 19, 0, 1,  0)   # Asia session

    # Previous day high/low — "mark previous day's high, low, and close" (spec §4)
    pdh = float(df["high"].iloc[:-1].rolling(50).max().iloc[-1]) if len(df) > 50 else None
    pdl = float(df["low"].iloc[:-1].rolling(50).min().iloc[-1])  if len(df) > 50 else None

    levels = [
        ("OR High",      or_h,   "short"),
        ("OR Low",       or_l,   "long"),
        ("London High",  lon_h,  "short"),
        ("London Low",   lon_l,  "long"),
        ("Asia High",    asia_h, "short"),
        ("Asia Low",     asia_l, "long"),
        ("Prev Day High",pdh,    "short"),
        ("Prev Day Low", pdl,    "long"),
    ]

    # ── Measure the flush: LAST 3 BARS only (must be fast) ───────
    lookback   = min(3, len(df))
    recent     = df.iloc[-lookback:]
    flush_high = float(recent["high"].max())
    flush_low  = float(recent["low"].min())
    move_size  = flush_high - flush_low

    lc = float(df["close"].iloc[-1])

    # Spec: "Fast and large — not slow or choppy"
    # 1.8× ATR over 3 bars = genuinely fast and large
    min_flush_atr = 1.8
    if move_size < atr * min_flush_atr:
        return (False, "none", None, flush_high, flush_low,
                f"Weak flush {move_size/atr:.1f}×ATR over 3 bars (need {min_flush_atr}×)")

    # ── Check reversal FROM a key level ──────────────────────────
    # Tolerance = 0.5× ATR — price must be close to the level, not far away
    tolerance = atr * 0.5
    for name, lvl, direction in levels:
        if lvl is None:
            continue
        if direction == "short":
            # Flushed UP to resistance, closed back below it → short reversal
            if flush_high >= (lvl - tolerance) and lc < lvl:
                return (True, "short", name, flush_high, flush_low,
                        f"↓ Flush to {name} ({lvl:.2f}), {move_size/atr:.1f}×ATR in 3 bars")
        else:
            # Flushed DOWN to support, closed back above it → long reversal
            if flush_low <= (lvl + tolerance) and lc > lvl:
                return (True, "long", name, flush_high, flush_low,
                        f"↑ Flush to {name} ({lvl:.2f}), {move_size/atr:.1f}×ATR in 3 bars")

    return (False, "none", None, flush_high, flush_low,
            f"Flush {move_size/atr:.1f}×ATR but missed all key zones")


# ── FACTOR 3: Trend Structure Break (spec §5 F3) ─────────────────
def _check_trend_break(df: pd.DataFrame, direction: str):
    """
    Spec: "prior trend must show signs of failing."
    Evidence accepted:
      • Price breaking a trendline (Lower High for short, Higher Low for long)
      • Head & Shoulders pattern: push to extreme, pullback, failed re-test
    "The trader does NOT enter on first sign — waits for structural confirmation."

    Requires at least 3 identified swing points to confirm structure.
    """
    if len(df) < 30:
        return False, "Insufficient bars for structure analysis"

    highs = df["high"].iloc[-30:].values
    lows  = df["low"].iloc[-30:].values

    if direction == "long":
        # Market was in downtrend (LL-LH). Look for HL = downtrend failing.
        swing_lows = [lows[i] for i in range(1, len(lows) - 1)
                      if lows[i] < lows[i-1] and lows[i] < lows[i+1]]

        # Require at least 3 swing lows for structural confirmation (spec: wait for confirmation)
        if len(swing_lows) >= 3:
            if swing_lows[-1] > swing_lows[-2]:
                return True, f"Higher Low confirmed ({swing_lows[-2]:.1f}→{swing_lows[-1]:.1f})"
            # Inv H&S: push lower, pullback, failed lower-low re-test
            if swing_lows[-2] < swing_lows[-3] and swing_lows[-1] > swing_lows[-2]:
                return True, "Inv H&S bottom: failed re-test of extreme low"
        elif len(swing_lows) == 2 and swing_lows[-1] > swing_lows[-2]:
            # 2 swing lows is minimal — only accept if the HL is clear (>0.5 ATR gap)
            atr_est = float(df["high"].iloc[-14:].max() - df["low"].iloc[-14:].min()) / 14
            if (swing_lows[-1] - swing_lows[-2]) > atr_est * 0.5:
                return True, f"Clear Higher Low ({swing_lows[-2]:.1f}→{swing_lows[-1]:.1f})"

        return False, f"No confirmed HL structure ({len(swing_lows)} swing lows found)"

    else:  # short
        swing_highs = [highs[i] for i in range(1, len(highs) - 1)
                       if highs[i] > highs[i-1] and highs[i] > highs[i+1]]

        if len(swing_highs) >= 3:
            if swing_highs[-1] < swing_highs[-2]:
                return True, f"Lower High confirmed ({swing_highs[-2]:.1f}→{swing_highs[-1]:.1f})"
            if swing_highs[-2] > swing_highs[-3] and swing_highs[-1] < swing_highs[-2]:
                return True, "H&S top: failed re-test of swing high"
        elif len(swing_highs) == 2 and swing_highs[-1] < swing_highs[-2]:
            atr_est = float(df["high"].iloc[-14:].max() - df["low"].iloc[-14:].min()) / 14
            if (swing_highs[-2] - swing_highs[-1]) > atr_est * 0.5:
                return True, f"Clear Lower High ({swing_highs[-2]:.1f}→{swing_highs[-1]:.1f})"

        return False, f"No confirmed LH structure ({len(swing_highs)} swing highs found)"


# ── FACTOR 4: Momentum Confirmation Candle (spec §5 F4) ──────────
def _check_momentum_candle(df: pd.DataFrame, direction: str):
    """
    Spec: "A large, decisive candle must break above the prior swing high (for longs)
    or below the prior swing low (for shorts). Entry is a stop-market order triggered
    on the break of that level."

    Prior swing = highest/lowest of bars [-6:-1] (5 bars before current).
    """
    if len(df) < 6:
        return False, None, "Insufficient bars"

    prior       = df.iloc[-6:-1]
    swing_high  = float(prior["high"].max())
    swing_low   = float(prior["low"].min())
    last_close  = float(df["close"].iloc[-1])
    last_high   = float(df["high"].iloc[-1])
    last_low    = float(df["low"].iloc[-1])

    if direction == "long":
        if last_high > swing_high and last_close > swing_high:
            return True, swing_high, f"Broke prior swing high {swing_high:.2f} (entry level)"
        return False, None, f"Waiting: close {last_close:.2f} < swing high {swing_high:.2f}"
    else:
        if last_low < swing_low and last_close < swing_low:
            return True, swing_low, f"Broke prior swing low {swing_low:.2f} (entry level)"
        return False, None, f"Waiting: close {last_close:.2f} > swing low {swing_low:.2f}"


# ── No-trade helper ───────────────────────────────────────────────
def _no_trade(symbol, tf, ts, notes, factors, atr=0.0, hit=0, trend="none"):
    return ORRSignal(
        timestamp=ts, timeframe=tf, symbol=symbol, direction="none",
        factors_hit=hit,
        factors=[asdict(f) for f in factors] if factors and isinstance(factors[0], FactorResult) else factors,
        base_probability=0.0, ml_adjustment=0.0, final_probability=0.0,
        signal_strength="NO_TRADE", entry_price=None, entry_range=None,
        stop_loss=None, tp1=None, tp2=None, atr=atr,
        fvg_count=0, key_levels=[], prior_trend=trend, notes=notes,
    )


# ── Main analyzer ─────────────────────────────────────────────────
def analyze(df: pd.DataFrame, timeframe: str = "1m", symbol: str = "ES") -> ORRSignal:
    """
    Run the 4-factor ORR checklist.
    ENTER only when ALL 4 pass.
    CAUTION when exactly 3/4 pass AND F1 (flush into key zone) is one of them.
    NO_TRADE otherwise — spec §11: "no exceptions."
    """
    notes   = []
    factors = []
    now_str = df.index[-1].strftime("%Y-%m-%d %H:%M:%S")

    if len(df) < 60:
        return _no_trade(symbol, timeframe, now_str, ["Insufficient bars (<60)"], factors)

    # ── Timezone ──────────────────────────────────────────────────
    try:
        df_et  = df.index.tz_convert(ET_TZ)
    except Exception:
        df_et  = df.index
    last_et = df_et[-1]

    # ── ATR ───────────────────────────────────────────────────────
    atr_series = compute_atr(df, 14)
    atr = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 1.0

    # ── Prior trend context (EMA50 bias) ─────────────────────────
    ema50       = df["close"].ewm(span=50, adjust=False).mean().iloc[-1]
    lc          = float(df["close"].iloc[-1])
    prior_trend = "long" if lc > ema50 else "short"

    # ══ F1: Strong Flush into Key Zone ════════════════════════════
    f1_ok, direction, anchor, flush_high, flush_low, f1_detail = _check_flush(df, atr, last_et)
    factors.append(FactorResult("F1: Strong Flush → Key Zone", f1_ok, f1_detail))
    if not f1_ok:
        notes.append("F1 fail: " + f1_detail)

    # ══ CRITICAL GATE: F1 MUST pass for any signal ════════════════
    # Spec §11: "No flush into key zone — skip." A trade without a flush has no edge.
    if not f1_ok:
        notes.append("F1 gate: no flush → NO TRADE (per spec §11)")
        return _no_trade(symbol, timeframe, now_str, notes,
                         [asdict(f) for f in factors], atr, 0, prior_trend)

    # ══ F2: Timing Window ═════════════════════════════════════════
    f2_ok    = is_in_timing_window(last_et)
    f2_detail = "✓ Timing window active" if f2_ok else f"Off-window ({last_et.strftime('%H:%M')} ET)"
    factors.append(FactorResult("F2: Timing Window (9:45/10:00 ET)", f2_ok, f2_detail))
    if not f2_ok:
        notes.append("F2 fail: " + f2_detail)

    # ══ F3: Trend Structure Break ══════════════════════════════════
    f3_ok, f3_detail = _check_trend_break(df, direction)
    factors.append(FactorResult("F3: Trend Structure Break", f3_ok, f3_detail))
    if not f3_ok:
        notes.append("F3 fail: " + f3_detail)

    # ══ F4: Momentum Confirmation Candle ══════════════════════════
    f4_ok, momentum_entry, f4_detail = _check_momentum_candle(df, direction)
    factors.append(FactorResult("F4: Momentum Confirmation Candle", f4_ok, f4_detail))
    if not f4_ok:
        notes.append("F4 fail: " + f4_detail)

    # ── Scoring ───────────────────────────────────────────────────
    # F1 is already confirmed above. Count total.
    factors_hit = sum(1 for f in factors if f.satisfied)  # includes F1

    # Spec: "ALL four factors must be present — no exceptions" for ENTER.
    # We extend with CAUTION for 3/4 ONLY when F1 is satisfied (edge still exists).
    if factors_hit < 3:
        notes.append(f"Only {factors_hit}/4 factors — NO TRADE")
        return _no_trade(symbol, timeframe, now_str, notes,
                         [asdict(f) for f in factors], atr, factors_hit, prior_trend)

    # ── Entry (spec §6) ───────────────────────────────────────────
    # Stop-market at the break of prior swing high/low.
    # If F4 failed (no confirmation yet), use current close as provisional entry.
    entry_px = momentum_entry if (f4_ok and momentum_entry) else lc

    # ── SL (spec §7) ──────────────────────────────────────────────
    # "Below the extreme of the flush that formed the reversal pattern."
    # Small buffer (0.2× ATR) to avoid exact-tick stops.
    if direction == "long":
        stop_loss = round(flush_low  - atr * 0.2, 2)
        risk      = max(entry_px - stop_loss, atr * 0.5)
    else:
        stop_loss = round(flush_high + atr * 0.2, 2)
        risk      = max(stop_loss - entry_px, atr * 0.5)

    # ── TP (spec §8) ──────────────────────────────────────────────
    if direction == "long":
        tp1 = round(entry_px + risk * TP1_R, 2)
        tp2 = round(entry_px + risk * TP2_R, 2)
    else:
        tp1 = round(entry_px - risk * TP1_R, 2)
        tp2 = round(entry_px - risk * TP2_R, 2)

    # Entry range (±10% ATR around stop-market trigger)
    atr_buf     = round(atr * 0.10, 2)
    entry_range = {"low": round(entry_px - atr_buf, 2), "high": round(entry_px + atr_buf, 2)}

    # ── Signal strength & probability ─────────────────────────────
    if factors_hit == 4:
        prob         = 0.90
        sig_strength = "ENTER"
        notes.append(f"4/4 ✅ {direction.upper()} | Entry ~{entry_px:.2f} | SL {stop_loss} | TP1 {tp1} | TP2 {tp2}")
    else:
        prob         = 0.65
        sig_strength = "CAUTION"
        failed = [f.name for f in factors if not f.satisfied]
        notes.append(f"3/4 🟡 CAUTION {direction.upper()} | Missing: {', '.join(failed)}")
        notes.append(f"Entry ~{entry_px:.2f} | SL {stop_loss} | TP1 {tp1} | TP2 {tp2}")

    if anchor:
        notes.append(f"Key zone hit: {anchor}")

    return ORRSignal(
        timestamp=now_str,   timeframe=timeframe, symbol=symbol,
        direction=direction, factors_hit=factors_hit,
        factors=[asdict(f) for f in factors],
        base_probability=prob,  ml_adjustment=0.0, final_probability=prob,
        signal_strength=sig_strength,
        entry_price=round(entry_px, 2),
        entry_range=entry_range,
        stop_loss=stop_loss,
        tp1=tp1, tp2=tp2,  atr=round(atr, 4),
        fvg_count=0, key_levels=[], prior_trend=prior_trend, notes=notes,
    )


# ── Backtest failure diagnosis ────────────────────────────────────
def diagnose_trade_failure(df: pd.DataFrame, entry: float, sl: float,
                           direction: str, tf: str) -> str:
    """Explain why a trade that fired still hit SL."""
    if len(df) < 5:
        return "Insufficient data for diagnosis."
    recent = df.iloc[-10:]
    if direction == "long":
        if recent["high"].max() < entry + (entry - sl) * 0.2:
            return "V-Shape Failure: price never reached 20% of risk — immediate reversal against trade."
        if (recent["low"] < sl + (entry - sl) * 0.1).sum() > 5:
            return "Structural Decay: price bled slowly into stop — no momentum behind move."
        return "Liquidity Sweep: spike below stop then recovered — thin market or news spike."
    else:
        if recent["low"].min() > entry - (sl - entry) * 0.2:
            return "V-Shape Failure: price never reached 20% of risk — immediate reversal against trade."
        if (recent["high"] > sl - (sl - entry) * 0.1).sum() > 5:
            return "Structural Decay: price bled slowly into stop — no momentum behind move."
        return "Liquidity Sweep: spike above stop then recovered — thin market or news spike."
