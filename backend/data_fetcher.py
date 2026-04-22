"""
data_fetcher.py
===============
Multi-symbol OHLCV data layer.

Supported symbols:
  ES  → ES=F   (E-mini S&P 500)
  MES → MES=F  (Micro E-mini S&P 500)
  NQ  → NQ=F   (E-mini NASDAQ-100)

Provider: Yahoo Finance (free, ~15-min delayed for futures).
Switch to MT5/Alpaca via DATA_SOURCE env var for real-time.
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────
DATA_SOURCE = os.getenv("DATA_SOURCE", "YAHOO").upper()

# Supported symbols: key = clean name, value = Yahoo ticker
SUPPORTED_SYMBOLS = {
    "ES":  "ES=F",
    "MES": "MES=F",
    "NQ":  "NQ=F",
}

# Default symbol (can be overridden per call)
DEFAULT_SYMBOL_KEY = os.getenv("DEFAULT_SYMBOL", "ES")
DEFAULT_SYMBOL_YF  = SUPPORTED_SYMBOLS.get(DEFAULT_SYMBOL_KEY, "ES=F")

# Yahoo Finance period map — give enough history for 1m (7d max)
YF_PERIOD_MAP = {
    "1m":  "7d",
    "5m":  "60d",
    "15m": "60d",
    "30m": "60d",
    "1h":  "730d",
    "4h":  "730d",
    "1d":  "5y",
}

YF_INTERVAL_MAP = {
    "1m":  "1m",
    "5m":  "5m",
    "15m": "15m",
    "30m": "30m",
    "1h":  "1h",
    "4h":  "1h",   # resample to 4h manually
    "1d":  "1d",
}

# ── Internal Yahoo helpers ─────────────────────────────────────────
def _fetch_yahoo(timeframe: str = "5m", bars: int = 300,
                 start: str = None, end: str = None,
                 symbol_yf: str = None) -> pd.DataFrame:
    """
    Fetch OHLCV from Yahoo Finance.
    symbol_yf: Yahoo ticker string, e.g. 'ES=F'
    """
    if symbol_yf is None:
        symbol_yf = DEFAULT_SYMBOL_YF

    interval = YF_INTERVAL_MAP.get(timeframe, "5m")
    ticker   = yf.Ticker(symbol_yf)

    try:
        if start:
            df = ticker.history(
                start       = start,
                end         = end,
                interval    = interval,
                auto_adjust = True,
            )
        else:
            period = YF_PERIOD_MAP.get(timeframe, "60d")
            df = ticker.history(period=period, interval=interval, auto_adjust=True)
    except Exception as e:
        logger.error(f"[Yahoo] Fetch error for {symbol_yf} {timeframe}: {e}")
        return pd.DataFrame()

    if df is None or df.empty:
        logger.warning(f"[Yahoo] No data for {symbol_yf} ({timeframe})")
        return pd.DataFrame()

    df.index = df.index.tz_convert("UTC")
    df.columns = [c.lower() for c in df.columns]
    df = df[[c for c in ["open","high","low","close","volume"] if c in df.columns]].dropna()

    # Fill missing volume with 0
    if "volume" not in df.columns:
        df["volume"] = 0

    if timeframe == "4h":
        df = df.resample("4h").agg({
            "open": "first", "high": "max",
            "low":  "min",   "close": "last",
            "volume": "sum",
        }).dropna()

    return df.tail(bars) if not start else df


def _get_price_yahoo(symbol_yf: str = None) -> Optional[float]:
    """Latest close price from Yahoo (1m bar)."""
    if symbol_yf is None:
        symbol_yf = DEFAULT_SYMBOL_YF
    try:
        ticker = yf.Ticker(symbol_yf)
        hist = ticker.history(period="1d", interval="1m")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception as e:
        logger.error(f"[Yahoo] Price fetch error for {symbol_yf}: {e}")
    return None


# ── Public API ────────────────────────────────────────────────────
def resolve_symbol_yf(symbol: str) -> str:
    """
    Accept either clean key ('ES', 'NQ', 'MES') or direct Yahoo ticker.
    Returns Yahoo ticker string.
    """
    upper = symbol.upper() if symbol else DEFAULT_SYMBOL_KEY
    if upper in SUPPORTED_SYMBOLS:
        return SUPPORTED_SYMBOLS[upper]
    # Already a Yahoo ticker?
    if "=" in symbol or symbol.endswith("=F"):
        return symbol
    return DEFAULT_SYMBOL_YF


def get_symbol_display(symbol: str = None) -> str:
    """Human-readable label for UI."""
    LABELS = {
        "ES=F":  "ES (E-mini S&P 500)",
        "MES=F": "MES (Micro E-mini S&P 500)",
        "NQ=F":  "NQ (NASDAQ Futures)",
    }
    yf_sym = resolve_symbol_yf(symbol or DEFAULT_SYMBOL_KEY)
    return LABELS.get(yf_sym, yf_sym)


def fetch_ohlcv(timeframe: str = "1m", bars: int = 300,
                start: Optional[str] = None, end: Optional[str] = None,
                symbol: str = None) -> pd.DataFrame:
    """
    Unified OHLCV fetcher.
    symbol: 'ES', 'MES', 'NQ'  OR direct Yahoo ticker.
    """
    yf_sym = resolve_symbol_yf(symbol or DEFAULT_SYMBOL_KEY)
    if DATA_SOURCE == "YAHOO":
        return _fetch_yahoo(timeframe, bars, start=start, end=end, symbol_yf=yf_sym)
    # Fallback
    logger.warning(f"Unknown DATA_SOURCE '{DATA_SOURCE}', using Yahoo")
    return _fetch_yahoo(timeframe, bars, start=start, end=end, symbol_yf=yf_sym)


def get_current_price(symbol: str = None) -> Optional[float]:
    """Get latest market price for symbol."""
    yf_sym = resolve_symbol_yf(symbol or DEFAULT_SYMBOL_KEY)
    if DATA_SOURCE == "YAHOO":
        return _get_price_yahoo(yf_sym)
    return _get_price_yahoo(yf_sym)


def get_all_prices() -> dict:
    """Fetch latest price for all 3 symbols. Returns {sym: price}."""
    result = {}
    for key, yf_sym in SUPPORTED_SYMBOLS.items():
        price = _get_price_yahoo(yf_sym)
        result[key] = price
    return result
