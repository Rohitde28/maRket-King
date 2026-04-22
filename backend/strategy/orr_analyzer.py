"""
orr_analyzer.py
===============
Opening Range Reversal — pure rule-based signal engine.
Implements strategy spec to the letter (no ML by default).

4-Factor Checklist (ALL 4 required for ENTER):
  F1 — Strong Flush into a Key Zone  (fast, large move to pre-marked level)
  F2 — Reversal Timing Alignment     (9:43-9:47 or 9:58-10:02 ET)
  F3 — Trend Structure Break         (H&S / Higher-Low / trendline break)
  F4 — Momentum Confirmation Candle  (break of prior swing high/low)

Entry   : last close (stop-market trigger on next bar)
SL      : below flush low (long) / above flush high (short)
TP1     : entry + 2×risk
TP2     : entry + 3×risk
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass, asdict, field
from typing import List, Optional
from datetime import datetime, timedelta
import pytz

# ── R-multiples (from spec §8) ────────────────────────────────────
TP1_R = 2.0
TP2_R = 3.0

# ── Timing Windows (ET) — from spec §3 ───────────────────────────
# Window 1: 9:45 ET  (±2 min buffer)
# Window 2: 10:00 ET (±2 min buffer)  ← "strongest"
# Extended:  10:30 ET (±2 min)
TIMING_WINDOWS_ET = [
    (9, 43,  9, 47),   # W1 — 9:45 ET
    (9, 58, 10,  2),   # W2 — 10:00 ET  (STRONGEST)
    (10, 28, 10, 32),  # Extended — 10:30 ET
]

# Used by main.py for throttle key generation (exported)
TIMING_WINDOWS_ET_RAW = TIMING_WINDOWS_ET

ET_TZ = pytz.timezone("America/New_York")


# ── Data classes ──────────────────────────────────────────────────
@dataclass
class FactorResult:
    name: str
    satisfied: bool
    detail: str


@dataclass
class ORRSignal:
    timestamp:        str
    timeframe:        str
    symbol:           str
    direction:        str
    factors_hit:      int
    factors:          List[dict]
    base_probability: float
    ml_adjustment:    float
    final_probability: float
    signal_strength:  str
    entry_price:      Optional[float]
    entry_range:      Optional[dict]
    stop_loss:        Optional[float]
    tp1:              Optional[float]
    tp2:              Optional[float]
    atr:              Optional[float]
    fvg_count:        int
    key_levels:       List[float]
    prior_trend:      str
    notes:            List[str]

    def to_dict(self):
        return asdict(self)


# ── ATR ───────────────────────────────────────────────────────────
def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"]  - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(period).mean()


# ── Timing window check ───────────────────────────────────────────
def is_in_timing_window(dt_et=None) -> bool:
    """True if current ET time falls inside any defined timing window."""
    if dt_et is None:
        dt_et = datetime.now(pytz.UTC).astimezone(ET_TZ)
    h, m = dt_et.hour, dt_et.minute
    t = h * 60 + m
    for sh, sm, eh, em in TIMING_WINDOWS_ET:
        if (sh * 60 + sm) <= t <= (eh * 60 + em):
            return True
    return False

# alias for import compatibility
_is_in_timing_window = is_in_timing_window


def next_window_et() -> Optional[tuple]:
    """Return (start_h, start_m) of next upcoming window, or None."""
    dt_et = datetime.now(pytz.UTC).astimezone(ET_TZ)
    t = dt_et.hour * 60 + dt_et.minute
    for sh, sm, eh, em in TIMING_WINDOWS_ET:
        if t < sh * 60 + sm:
            return (sh, sm)
    return None


# ── Key level detection (spec §4) ─────────────────────────────────
def _get_session_range(df: pd.DataFrame, df_et, start_h, start_m, end_h, end_m):
    """High/low of a session slice within last 24 h."""
    try:
        t = df_et.hour * 60 + df_et.minute
        s = start_h * 60 + start_m
        e = end_h   * 60 + end_m
        cutoff = df.index[-1] - timedelta(hours=24)
        bars = df[df.index >= cutoff]
        b_et = bars.index.tz_convert(ET_TZ)
        mins = b_et.hour * 60 + b_et.minute
        if s > e:  # overnight
            mask = (mins >= s) | (mins <= e)
        else:
            mask = (mins >= s) & (mins <= e)
        slc = bars[mask.values]
        if not slc.empty:
            return slc["high"].max(), slc["low"].min()
    except Exception:
        pass
    return None, None


# ── Factor 1: Strong Flush into Key Zone (spec §5 F1) ────────────
def _check_flush(df, atr, df_et):
    """
    Detect a fast, aggressive directional flush into a pre-marked
    support or resistance level.
    Returns: (satisfied, direction, anchor_name, flush_extreme_high, flush_extreme_low, detail)
    """
    # Session ranges as key levels
    or_h,  or_l  = _get_session_range(df, df_et, 9, 30, 9, 45)  # Opening Range
    lon_h, lon_l = _get_session_range(df, df_et, 3,  0, 8, 30)  # London
    asia_h,asia_l= _get_session_range(df, df_et, 19, 0, 1,  0)  # Asia

    # Prev-day high/low (rolling 50-bar proxy)
    pdh = float(df["high"].iloc[:-1].rolling(50).max().iloc[-1]) if len(df) > 50 else None
    pdl = float(df["low"].iloc[:-1].rolling(50).min().iloc[-1])  if len(df) > 50 else None

    levels = [
        ("OR High",    or_h,   "short"),
        ("OR Low",     or_l,   "long"),
        ("London High",lon_h,  "short"),
        ("London Low", lon_l,  "long"),
        ("Asia High",  asia_h, "short"),
        ("Asia Low",   asia_l, "long"),
        ("Prev Day High", pdh, "short"),
        ("Prev Day Low",  pdl, "long"),
    ]

    # Measure flush: max range over last 5 bars
    lookback = min(5, len(df))
    recent = df.iloc[-lookback:]
    flush_high = recent["high"].max()
    flush_low  = recent["low"].min()
    move_size  = flush_high - flush_low

    lc = float(df["close"].iloc[-1])
    lh = float(df["high"].iloc[-1])
    ll = float(df["low"].iloc[-1])

    # Fast = move > 1.5× ATR (aggressive, not slow/choppy)
    min_flush_atr = 1.5
    if move_size < atr * min_flush_atr:
        return False, "none", None, flush_high, flush_low, f"Weak flush {move_size/atr:.1f}×ATR (need {min_flush_atr}×)"

    # Check if close reversed FROM a key level
    for name, lvl, direction in levels:
        if lvl is None:
            continue
        tolerance = atr * 0.5
        if direction == "short":
            # Flushed UP through resistance then closed back below it
            if flush_high >= (lvl - tolerance) and lc < lvl:
                return True, "short", name, flush_high, flush_low, f"↓ Flush to {name} ({lvl:.1f}), {move_size/atr:.1f}×ATR"
        else:
            # Flushed DOWN through support then closed back above it
            if flush_low <= (lvl + tolerance) and lc > lvl:
                return True, "long", name, flush_high, flush_low, f"↑ Flush to {name} ({lvl:.1f}), {move_size/atr:.1f}×ATR"

    return False, "none", None, flush_high, flush_low, f"No key zone hit ({move_size/atr:.1f}×ATR)"


# ── Factor 3: Trend Structure Break (spec §5 F3) ──────────────────
def _check_trend_break(df, direction):
    """
    Spec: prior trend must show signs of failing.
    Evidence: HL sequence ending, H&S pattern, or trendline break.
    Uses last 20 bars.
    """
    if len(df) < 20:
        return False, "Insufficient bars"

    closes = df["close"].iloc[-20:].values
    highs  = df["high"].iloc[-20:].values
    lows   = df["low"].iloc[-20:].values

    if direction == "long":
        # Was in a downtrend (LH-LL), now printing a Higher Low
        # Find last 3 swing lows
        swing_lows = []
        for i in range(1, len(lows) - 1):
            if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
                swing_lows.append(lows[i])
        if len(swing_lows) >= 2:
            # Last low is HIGHER than the one before → downtrend ending
            if swing_lows[-1] > swing_lows[-2]:
                return True, f"Higher Low formed ({swing_lows[-2]:.1f}→{swing_lows[-1]:.1f})"
        # H&S bottom: push to new low, pullback, failed re-test
        if len(swing_lows) >= 3:
            if swing_lows[-2] < swing_lows[-3] and swing_lows[-1] > swing_lows[-2]:
                return True, "Inv H&S / double-bottom forming"
        return False, "No HL or structure break"

    else:  # short
        # Was in uptrend (HH-HL), now printing Lower High
        swing_highs = []
        for i in range(1, len(highs) - 1):
            if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
                swing_highs.append(highs[i])
        if len(swing_highs) >= 2:
            if swing_highs[-1] < swing_highs[-2]:
                return True, f"Lower High formed ({swing_highs[-2]:.1f}→{swing_highs[-1]:.1f})"
        if len(swing_highs) >= 3:
            if swing_highs[-2] > swing_highs[-3] and swing_highs[-1] < swing_highs[-2]:
                return True, "H&S top forming"
        return False, "No LH or structure break"


# ── Factor 4: Momentum Confirmation Candle (spec §5 F4) ──────────
def _check_momentum_candle(df, direction):
    """
    A large decisive candle must break above prior swing high (long)
    or below prior swing low (short).
    Entry placed as stop-market AT that break level.
    """
    if len(df) < 5:
        return False, None, "Insufficient bars"

    # Prior swing high/low = highest/lowest of bars 2-6 from end (excluding last)
    prior_bars = df.iloc[-6:-1]
    prior_swing_high = float(prior_bars["high"].max())
    prior_swing_low  = float(prior_bars["low"].min())

    last_close = float(df["close"].iloc[-1])
    last_high  = float(df["high"].iloc[-1])
    last_low   = float(df["low"].iloc[-1])

    # Candle body size
    atr_proxy = float(df["high"].iloc[-10:].max() - df["low"].iloc[-10:].min()) / 10

    if direction == "long":
        if last_high > prior_swing_high and last_close > prior_swing_high:
            entry = prior_swing_high  # stop-market entry above prior swing
            return True, entry, f"Broke prior swing high {prior_swing_high:.1f}"
        return False, None, f"Waiting: close ({last_close:.1f}) < swing high ({prior_swing_high:.1f})"

    else:  # short
        if last_low < prior_swing_low and last_close < prior_swing_low:
            entry = prior_swing_low  # stop-market entry below prior swing
            return True, entry, f"Broke prior swing low {prior_swing_low:.1f}"
        return False, None, f"Waiting: close ({last_close:.1f}) > swing low ({prior_swing_low:.1f})"


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


# ── Main analyzer (spec §5, §6, §7, §8) ──────────────────────────
def analyze(df: pd.DataFrame, timeframe: str = "1m", symbol: str = "ES") -> ORRSignal:
    """
    Run the 4-factor ORR checklist.
    Returns ORRSignal.  signal_strength = 'ENTER' only if ALL 4 factors pass.
    """
    notes   = []
    factors = []
    now_str = df.index[-1].strftime("%Y-%m-%d %H:%M:%S")

    if len(df) < 50:
        return _no_trade(symbol, timeframe, now_str, ["Insufficient bars (<50)"], factors)

    # ── Timezone conversion ───────────────────────────────────────
    try:
        df_et = df.index.tz_convert(ET_TZ)
    except Exception:
        df_et = df.index
    last_et = df_et[-1]

    # ── ATR (14-period) ───────────────────────────────────────────
    atr_series = compute_atr(df, 14)
    atr = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 1.0

    # ── Prior trend (EMA50 context) ───────────────────────────────
    ema50 = df["close"].ewm(span=50, adjust=False).mean().iloc[-1]
    lc    = float(df["close"].iloc[-1])
    prior_trend = "long" if lc > ema50 else "short"

    # ══ FACTOR 1: Strong Flush into Key Zone ══════════════════════
    f1_ok, direction, anchor, flush_high, flush_low, f1_detail = _check_flush(df, atr, last_et)
    factors.append(FactorResult("Strong Flush into Key Zone", f1_ok, f1_detail))

    if not f1_ok:
        notes.append("F1 fail: " + f1_detail)

    # ══ FACTOR 2: Timing Window Alignment ═════════════════════════
    f2_ok = is_in_timing_window(last_et)
    f2_detail = "✓ Timing window active" if f2_ok else f"Off-window ({last_et.strftime('%H:%M')} ET)"
    factors.append(FactorResult("Reversal Timing Alignment", f2_ok, f2_detail))
    if not f2_ok:
        notes.append("F2 fail: " + f2_detail)

    # ══ FACTOR 3: Trend Structure Break ═══════════════════════════
    if f1_ok and direction != "none":
        f3_ok, f3_detail = _check_trend_break(df, direction)
    else:
        f3_ok, f3_detail = False, "No valid flush direction"
    factors.append(FactorResult("Trend Structure Break", f3_ok, f3_detail))
    if not f3_ok:
        notes.append("F3 fail: " + f3_detail)

    # ══ FACTOR 4: Momentum Confirmation Candle ════════════════════
    if f1_ok and direction != "none":
        f4_ok, momentum_entry, f4_detail = _check_momentum_candle(df, direction)
    else:
        f4_ok, momentum_entry, f4_detail = False, None, "No valid flush direction"
    factors.append(FactorResult("Momentum Confirmation Candle", f4_ok, f4_detail))
    if not f4_ok:
        notes.append("F4 fail: " + f4_detail)

    # ── Scoring ───────────────────────────────────────────────────
    factors_hit = sum(1 for f in factors if f.satisfied)

    # Spec §5: "ALL four factors must be present" → strict 4/4 only
    if factors_hit < 4:
        notes.append(f"Only {factors_hit}/4 factors — NO TRADE")
        return _no_trade(symbol, timeframe, now_str, notes,
                         [asdict(f) for f in factors], atr, factors_hit, prior_trend)

    # ── Entry / SL / TP (spec §6, §7, §8) ────────────────────────
    entry_px = momentum_entry if momentum_entry else lc

    if direction == "long":
        # SL below the flush extreme low (spec §7)
        stop_loss = round(flush_low - atr * 0.3, 2)
        risk      = entry_px - stop_loss
    else:
        # SL above flush extreme high (spec §7)
        stop_loss = round(flush_high + atr * 0.3, 2)
        risk      = stop_loss - entry_px

    if risk <= 0:
        risk = atr  # safeguard

    tp1 = round(entry_px + risk * TP1_R if direction == "long" else entry_px - risk * TP1_R, 2)
    tp2 = round(entry_px + risk * TP2_R if direction == "long" else entry_px - risk * TP2_R, 2)

    atr_buf = round(atr * 0.15, 2)
    entry_range = {
        "low":  round(entry_px - atr_buf, 2),
        "high": round(entry_px + atr_buf, 2),
    }

    notes.append(f"4/4 ✅ {direction.upper()} @ {entry_px:.2f} | SL {stop_loss} | TP1 {tp1} | TP2 {tp2}")
    if anchor:
        notes.append(f"Key zone: {anchor}")

    return ORRSignal(
        timestamp=now_str, timeframe=timeframe, symbol=symbol,
        direction=direction,
        factors_hit=factors_hit,
        factors=[asdict(f) for f in factors],
        base_probability=0.90, ml_adjustment=0.0, final_probability=0.90,
        signal_strength="ENTER",
        entry_price=round(entry_px, 2),
        entry_range=entry_range,
        stop_loss=stop_loss,
        tp1=tp1, tp2=tp2, atr=round(atr, 4),
        fvg_count=0, key_levels=[],
        prior_trend=prior_trend, notes=notes,
    )


# ── Backtest helper ───────────────────────────────────────────────
def diagnose_trade_failure(df, entry, sl, direction, tf):
    """
    Analyzes WHY a trade failed (hit SL).
    """
    if len(df) < 5:
        return "Insufficient data for diagnosis."
    
    recent = df.iloc[-10:]
    max_price = recent["high"].max()
    min_price = recent["low"].min()
    
    if direction == "long":
        # If it hit SL without ever going above entry
        if recent["high"].max() < entry + (entry - sl) * 0.2:
            return "V-Shape Failure: Immediate continuation against trade."
        # If it spent a long time near SL
        if (recent["low"] < sl + (entry - sl) * 0.1).sum() > 5:
            return "Structural Decay: Price bled into stop loss."
        return "Volatile Stop: Liquidity sweep below key level."
    else:
        if recent["low"].min() > entry - (sl - entry) * 0.2:
            return "V-Shape Failure: Immediate continuation against trade."
        if (recent["high"] > sl - (sl - entry) * 0.1).sum() > 5:
            return "Structural Decay: Price bled into stop loss."
        return "Volatile Stop: Liquidity sweep above key level."
