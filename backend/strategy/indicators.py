"""
indicators.py
=============
Pure technical indicator calculations used by the ORR strategy engine.
All functions operate on pandas DataFrames with columns: open, high, low, close, volume
"""

import numpy as np
import pandas as pd
from typing import Optional, List, Tuple


# ------------------------------------------------------------------ #
#  ATR  (Average True Range)
# ------------------------------------------------------------------ #
def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Average True Range = rolling mean of True Range.
    True Range = max(H-L, |H-Cprev|, |L-Cprev|)
    """
    high  = df["high"]
    low   = df["low"]
    close = df["close"]

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    atr = tr.rolling(period).mean()
    return atr


# ------------------------------------------------------------------ #
#  Swing Highs / Lows
# ------------------------------------------------------------------ #
def find_swing_highs(df: pd.DataFrame, window: int = 3) -> pd.Series:
    """
    Local swing highs: candle whose high is highest in a rolling window on each side.
    Returns boolean Series — True at swing high bars.
    """
    high = df["high"]
    is_high = (high == high.rolling(window * 2 + 1, center=True).max())
    return is_high


def find_swing_lows(df: pd.DataFrame, window: int = 3) -> pd.Series:
    """
    Local swing lows: candle whose low is lowest in a rolling window on each side.
    Returns boolean Series — True at swing low bars.
    """
    low = df["low"]
    is_low = (low == low.rolling(window * 2 + 1, center=True).min())
    return is_low


def last_swing_high(df: pd.DataFrame, window: int = 3) -> Optional[float]:
    """Price of the most recent swing high."""
    sh = find_swing_highs(df, window)
    highs = df.loc[sh, "high"]
    return float(highs.iloc[-1]) if not highs.empty else None


def last_swing_low(df: pd.DataFrame, window: int = 3) -> Optional[float]:
    """Price of the most recent swing low."""
    sl = find_swing_lows(df, window)
    lows = df.loc[sl, "low"]
    return float(lows.iloc[-1]) if not lows.empty else None


# ------------------------------------------------------------------ #
#  Fair Value Gaps  (FVG)
# ------------------------------------------------------------------ #
def detect_fvg(df: pd.DataFrame) -> pd.DataFrame:
    """
    Fair Value Gap: a 3-candle pattern where the body of candle N-2 
    and body of candle N do NOT overlap, leaving a gap (imbalance zone).

    Returns a DataFrame with columns:
      fvg_type    : 'bullish' | 'bearish' | None
      fvg_high    : top of the gap
      fvg_low     : bottom of the gap
    """
    results = []
    for i in range(2, len(df)):
        c0 = df.iloc[i - 2]   # candle N-2
        c1 = df.iloc[i - 1]   # candle N-1 (the big move)
        c2 = df.iloc[i]       # candle N

        # Bullish FVG: c0 high < c2 low  (gap above c0)
        if c0["high"] < c2["low"]:
            results.append({
                "time":     df.index[i],
                "fvg_type": "bullish",
                "fvg_high": c2["low"],
                "fvg_low":  c0["high"],
            })
        # Bearish FVG: c0 low > c2 high  (gap below c0)
        elif c0["low"] > c2["high"]:
            results.append({
                "time":     df.index[i],
                "fvg_type": "bearish",
                "fvg_high": c0["low"],
                "fvg_low":  c2["high"],
            })
        else:
            results.append({"time": df.index[i], "fvg_type": None,
                             "fvg_high": None, "fvg_low": None})

    return pd.DataFrame(results).set_index("time") if results else pd.DataFrame()


def count_recent_fvgs(df: pd.DataFrame, lookback: int = 20) -> int:
    """Count FVGs in the last `lookback` bars."""
    fvg = detect_fvg(df.tail(lookback + 2))
    return int((fvg["fvg_type"].notna()).sum())


# ------------------------------------------------------------------ #
#  Trend Structure
# ------------------------------------------------------------------ #
def detect_trend(df: pd.DataFrame, window: int = 5) -> str:
    """
    Classify trend as 'uptrend' | 'downtrend' | 'ranging' based on
    sequence of swing highs/lows.
    """
    sh_mask = find_swing_highs(df, window)
    sl_mask = find_swing_lows(df, window)

    sh_prices = df.loc[sh_mask, "high"].values
    sl_prices = df.loc[sl_mask, "low"].values

    if len(sh_prices) < 2 or len(sl_prices) < 2:
        return "ranging"

# Uptrend: higher highs AND higher lows
    hh = sh_prices[-1] > sh_prices[-2]
    hl = sl_prices[-1] > sl_prices[-2]

    # Downtrend: lower highs AND lower lows
    lh = sh_prices[-1] < sh_prices[-2]
    ll = sl_prices[-1] < sl_prices[-2]

    if hh and hl:
        return "uptrend"
    elif lh and ll:
        return "downtrend"
    else:
        return "ranging"


def detect_mss(df: pd.DataFrame, direction: str = "long") -> bool:
    """
    Market Structure Shift (MSS):
    - Long Reversal: Price breaks and closes ABOVE the most recent swing high.
    - Short Reversal: Price breaks and closes BELOW the most recent swing low.
    """
    if len(df) < 10: return False

    if direction == "long":
        sh = last_swing_high(df.iloc[:-1]) # exclude current bar
        if sh and df["close"].iloc[-1] > sh:
            return True
    else:
        sl = last_swing_low(df.iloc[:-1])
        if sl and df["close"].iloc[-1] < sl:
            return True
    return False


def detect_london_range(df: pd.DataFrame) -> Tuple[Optional[float], Optional[float]]:
    """
    Find the high/low between 03:00 and 09:00 AM US Eastern time.
    Requires the DataFrame index to be timezone-aware (aware of Eastern or UTC).
    """
    try:
        # Convert index to ET to find session bars
        import pytz
        et_tz = pytz.timezone("America/New_York")

        if df.index.tz is None:
            # Fallback for naive index (assume UTC)
            temp_df = df.copy()
            temp_df.index = temp_df.index.tz_localize("UTC").tz_convert(et_tz)
        else:
            temp_df = df.copy()
            temp_df.index = temp_df.index.tz_convert(et_tz)

        # Filter for London hours (03:00 - 09:00 ET)
        london_bars = temp_df.between_time("03:00", "09:00")
        if london_bars.empty:
            return None, None

        return float(london_bars["high"].max()), float(london_bars["low"].min())
    except Exception:
        return None, None


def detect_trend_break(df: pd.DataFrame, window: int = 5) -> Tuple[bool, str]:
    """
    Detect if the prior trend has shown signs of breaking.
    Returns (broke: bool, prior_trend: str)

    Break rules (per ORR strategy):
      - In a downtrend: market prints a Higher Low (HL) → potential reversal
      - In an uptrend:  market prints a Lower High (LH) → potential reversal
      - Price crosses a dynamic trend line
    """
    sh_mask = find_swing_highs(df, window)
    sl_mask = find_swing_lows(df, window)

    sh_prices = df.loc[sh_mask, "high"].values
    sl_prices = df.loc[sl_mask, "low"].values

    prior_trend = detect_trend(df.iloc[:-5], window)   # trend before last 5 bars

    if len(sh_prices) < 2 or len(sl_prices) < 2:
        return False, prior_trend

    broke = False

    if prior_trend == "downtrend":
        # Higher Low = potential reversal from downtrend
        if sl_prices[-1] > sl_prices[-2]:
            broke = True
        # Head & shoulders top: last SH < previous SH (failed push)
        if len(sh_prices) >= 3 and sh_prices[-1] < sh_prices[-2]:
            broke = True

    elif prior_trend == "uptrend":
        # Lower High = potential reversal from uptrend
        if sh_prices[-1] < sh_prices[-2]:
            broke = True
        # Inverse H&S: last SL > previous SL (failed push lower)
        if len(sl_prices) >= 3 and sl_prices[-1] > sl_prices[-2]:
            broke = True

    return broke, prior_trend


# ------------------------------------------------------------------ #
#  Flush Detector  (Factor 1)
# ------------------------------------------------------------------ #
def detect_flush(df: pd.DataFrame, atr_multiplier: float = 1.5,
                 lookback: int = 10) -> Tuple[bool, str]:
    """
    A 'flush' is a fast, aggressive directional price move greater than
    `atr_multiplier` × ATR within the last `lookback` bars.

    Returns (is_flush: bool, direction: 'long' | 'short' | 'none')
    """
    if len(df) < 20:
        return False, "none"

    atr = compute_atr(df, 14).iloc[-1]
    if atr == 0 or np.isnan(atr):
        return False, "none"

    recent = df.tail(lookback)
    price_range = recent["close"].iloc[-1] - recent["close"].iloc[0]
    abs_range   = abs(price_range)

    if abs_range >= atr_multiplier * atr:
        direction = "long" if price_range < 0 else "short"  # flush DOWN = potential long
        return True, direction

    return False, "none"


# ------------------------------------------------------------------ #
#  Momentum Confirmation Candle  (Factor 4)
# ------------------------------------------------------------------ #
def detect_momentum_candle(df: pd.DataFrame, atr_multiplier: float = 1.2,
                            direction: str = "long") -> bool:
    """
    The most recent candle must be large (> atr_multiplier * ATR)
    and moving in the reversal direction:
      - Long  reversal: big bullish candle (close > open, large body)
      - Short reversal: big bearish candle (close < open, large body)
    """
    if len(df) < 15:
        return False

    atr     = compute_atr(df, 14).iloc[-1]
    last    = df.iloc[-1]
    body    = abs(last["close"] - last["open"])

    if body < atr_multiplier * atr:
        return False

    if direction == "long"  and last["close"] > last["open"]:
        return True
    if direction == "short" and last["close"] < last["open"]:
        return True

    return False


# ------------------------------------------------------------------ #
#  Key Level Detection  (S/R zones for Factor 1 confirmation)
# ------------------------------------------------------------------ #
def detect_key_levels(df: pd.DataFrame, sensitivity: int = 5) -> List[float]:
    """
    Find key support/resistance price levels.
    Prioritizes:
    1. London Session High/Low (Strong Liquidity)
    2. Previous Day High/Low
    3. Swing points from the recent chart
    """
    levels = []

    # 1. London Range
    lon_h, lon_l = detect_london_range(df)
    if lon_h: levels.append(lon_h)
    if lon_l: levels.append(lon_l)

    # 2. Daily High/Low (from the last 1-2 days)
    try:
        daily_h = float(df["high"].resample('D').max().iloc[-1])
        daily_l = float(df["low"].resample('D').min().iloc[-1])
        levels.extend([daily_h, daily_l])
    except Exception:
        pass

    # 3. Swing points
    sh_mask = find_swing_highs(df, sensitivity)
    sl_mask = find_swing_lows(df, sensitivity)
    levels += list(df.loc[sh_mask, "high"].values)
    levels += list(df.loc[sl_mask, "low"].values)

    if not levels:
        return []

    # Cluster close levels (0.1% threshold for tighter levels)
    levels.sort()
    clustered = [levels[0]]
    for lvl in levels[1:]:
        if abs(lvl - clustered[-1]) / clustered[-1] > 0.001:  # 0.1%
            clustered.append(lvl)
        else:
            clustered[-1] = (clustered[-1] + lvl) / 2
    return clustered


def price_near_key_level(price: float, levels: List[float],
                         tolerance_pct: float = 0.003) -> bool:
    """Return True if price is within tolerance_pct of any key level."""
    for lvl in levels:
        if abs(price - lvl) / lvl <= tolerance_pct:
            return True
    return False
