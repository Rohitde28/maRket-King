"""
main.py  —  ORR Signal Platform Backend
FastAPI + WebSocket server for ES, MES, NQ futures.

Analysis: 3 symbols × [1m, 5m, 15m], every 30 s for 1m, 60 s for others.
No AI/ML by default — pure rule-based, fast.

Start:
    cd backend
    uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

import os
import asyncio
import logging
import json
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pytz
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv

from data_fetcher import (
    fetch_ohlcv, get_current_price, get_symbol_display,
    SUPPORTED_SYMBOLS, resolve_symbol_yf,
)
from strategy.orr_analyzer import analyze, is_in_timing_window, TIMING_WINDOWS_ET
from trade_log import init_db, log_signal, log_trade, close_trade, get_trades, get_signals, get_stats

load_dotenv()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s")
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────
SYMBOLS            = list(SUPPORTED_SYMBOLS.keys())   # [ES, MES, NQ]
ACTIVE_TIMEFRAMES  = ["1m", "5m"]
INTERVAL_1M        = 15    # seconds — aggressive polling for 1m live signals
INTERVAL_OTHER     = 60    # seconds — for 5m
ML_BACKEND         = os.getenv("ML_BACKEND", "RULE_BASED").upper()

app = FastAPI(title="ORR Trading Signal API — ES/MES/NQ", version="2.0.0")

app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.exists(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


# ── State ─────────────────────────────────────────────────────────
# latest_signals[symbol][tf] = signal dict
latest_signals: dict   = {s: {} for s in SYMBOLS}
open_signals:   list   = []
connected_clients: set = set()


class NumpyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (np.integer,)): return int(obj)
        if isinstance(obj, (np.floating,)): return float(obj)
        if isinstance(obj, np.ndarray):    return obj.tolist()
        return super().default(obj)


async def _broadcast(msg_dict: dict):
    msg  = json.dumps(msg_dict, cls=NumpyEncoder)
    dead = set()
    for ws in list(connected_clients):
        try:    await ws.send_text(msg)
        except: dead.add(ws)
    connected_clients.difference_update(dead)


# ── Analysis loop ─────────────────────────────────────────────────
async def run_analysis_loop():
    """
    Per-symbol per-timeframe analysis.
    1m runs every 30 s, 5m/15m every 60 s.
    """
    await asyncio.sleep(2)
    last_ts    = {s: {tf: None for tf in ACTIVE_TIMEFRAMES} for s in SYMBOLS}
    last_sig_w = {}   # throttle duplicate signals per window

    tick = 0
    while True:
        et_tz  = pytz.timezone("America/New_York")
        now_et = datetime.now(timezone.utc).astimezone(et_tz)
        if now_et.weekday() >= 5:
            logger.info("[Engine] Weekend — market closed.")
            await asyncio.sleep(300)
            continue

        loop = asyncio.get_event_loop()

        for symbol in SYMBOLS:
            for tf in ACTIVE_TIMEFRAMES:
                # Throttle: 5m/15m only every other cycle (~60s)
                if tf != "1m" and tick % 2 != 0:
                    continue
                try:
                    df = await loop.run_in_executor(
                        None, fetch_ohlcv, tf, 300, None, None, symbol
                    )
                    if df is None or df.empty:
                        continue

                    latest_ts = df.index[-1]
                    if last_ts[symbol][tf] == latest_ts:
                        continue  # same candle, skip

                    sig  = analyze(df, timeframe=tf, symbol=symbol)
                    sd   = sig.to_dict()
                    sd["timestamp"] = latest_ts.isoformat()
                    sd["symbol"]    = symbol  # ensure set

                    latest_signals[symbol][tf] = sd
                    last_ts[symbol][tf] = latest_ts

                    # Only log/broadcast actionable signals
                    if sig.signal_strength == "ENTER":
                        # ── Throttle: 1 signal per symbol per window per direction
                        window_id = None
                        t_et = now_et.hour * 60 + now_et.minute
                        for idx, (sh, sm, eh, em) in enumerate(TIMING_WINDOWS_ET):
                            if (sh*60+sm) <= t_et <= (eh*60+em):
                                window_id = idx; break

                        tkey = f"{symbol}_{tf}_{window_id}_{sig.direction}"
                        if window_id is not None and last_sig_w.get(tkey):
                            logger.info(f"[Throttle] Dup signal suppressed: {tkey}")
                            continue
                        if window_id is not None:
                            last_sig_w[tkey] = True

                        logger.info(f"[SIGNAL] {symbol} {tf} {sig.direction.upper()} "
                                    f"entry={sig.entry_price} sl={sig.stop_loss}")

                        # Persist to DB
                        try:
                            sd["_db_id"] = log_signal(sd)
                        except Exception as e:
                            logger.error(f"[DB] log_signal: {e}")

                        open_signal_entry = dict(sd)
                        open_signal_entry["_df_cache"] = df  # for failure diagnosis
                        open_signals.append(open_signal_entry)

                        if connected_clients:
                            await _broadcast({"type": "signal", "data": sd})

                except Exception as e:
                    logger.error(f"[Engine] {symbol} {tf}: {e}", exc_info=False)

        tick += 1
        await asyncio.sleep(INTERVAL_1M)


# ── Outcome monitor ───────────────────────────────────────────────
async def run_outcome_monitor():
    """Check every 15s if open signals hit SL/TP1/TP2."""
    from strategy.orr_analyzer import diagnose_trade_failure
    await asyncio.sleep(10)
    while True:
        if open_signals:
            loop = asyncio.get_event_loop()
            prices = {}
            for sym in SYMBOLS:
                p = await loop.run_in_executor(None, get_current_price, sym)
                if p: prices[sym] = p

            still_open = []
            for sig in open_signals:
                sym   = sig.get("symbol", SYMBOLS[0])
                price = prices.get(sym)
                if price is None:
                    still_open.append(sig); continue

                direction = sig.get("direction")
                entry = sig.get("entry_price")
                sl    = sig.get("stop_loss")
                tp1   = sig.get("tp1")
                tp2   = sig.get("tp2")

                outcome = None
                if direction == "long":
                    if price <= sl:    outcome = "SL"
                    elif price >= tp2: outcome = "TP2"
                    elif price >= tp1: outcome = "TP1"
                elif direction == "short":
                    if price >= sl:    outcome = "SL"
                    elif price <= tp2: outcome = "TP2"
                    elif price <= tp1: outcome = "TP1"

                if outcome:
                    sig["outcome"]      = outcome
                    sig["exit_price"]   = price
                    sig["outcome_time"] = datetime.now(timezone.utc).isoformat()

                    # Failure diagnosis on SL
                    exit_reason = None
                    if outcome == "SL":
                        try:
                            tf = sig.get("timeframe", "1m")
                            df_cache = sig.get("_df_cache")
                            if df_cache is not None:
                                exit_reason = diagnose_trade_failure(df_cache, entry, sl, direction, tf)
                            sig["exit_reason"] = exit_reason
                        except Exception:
                            pass

                    logger.info(f"[Monitor] {sym} {outcome} @ {price:.2f}"
                                + (f" | {exit_reason}" if exit_reason else ""))

                    if sig.get("_db_id"):
                        try:
                            from trade_log import _get_conn
                            with _get_conn() as conn:
                                conn.execute(
                                    "UPDATE signals SET outcome=?, exit_price=?, exit_reason=? WHERE id=?",
                                    (outcome, price, exit_reason, sig["_db_id"])
                                )
                        except Exception as e:
                            logger.error(f"[Monitor] DB: {e}")

                    await _broadcast({"type": "outcome", "data": sig})
                else:
                    still_open.append(sig)

            open_signals.clear()
            open_signals.extend(still_open)

        await asyncio.sleep(15)  # aligned with 1m polling interval


# ── Matrix heartbeat ──────────────────────────────────────────────
async def run_matrix_heartbeat():
    """Broadcast full 6-combo status to matrix grid every 15 s."""
    loop = asyncio.get_event_loop()
    await asyncio.sleep(5)
    while True:
        prices = {}
        for sym in SYMBOLS:
            p = await loop.run_in_executor(None, get_current_price, sym)
            prices[sym] = p

        matrix = {}
        for sym in SYMBOLS:
            matrix[sym] = {}
            for tf in ACTIVE_TIMEFRAMES:
                sig = latest_signals.get(sym, {}).get(tf, {})
                matrix[sym][tf] = {
                    "symbol":          sym,
                    "timeframe":       tf,
                    "price":           prices.get(sym),
                    "signal_strength": sig.get("signal_strength", "WAITING"),
                    "direction":       sig.get("direction", "none"),
                    "factors_hit":     sig.get("factors_hit", 0),
                    "entry_price":     sig.get("entry_price"),
                    "stop_loss":       sig.get("stop_loss"),
                }

        await _broadcast({"type": "matrix_update", "data": matrix})
        await asyncio.sleep(15)


# ── Startup ───────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    init_db()
    logger.info(f"✅ DB init | Symbols: {SYMBOLS} | TFs: {ACTIVE_TIMEFRAMES}")
    logger.info(f"✅ ML backend: {ML_BACKEND} (AI only if OLLAMA)")
    logger.info(f"✅ Polling: 1m every {INTERVAL_1M}s, 5m every {INTERVAL_OTHER}s")
    asyncio.create_task(run_analysis_loop())
    asyncio.create_task(run_outcome_monitor())
    asyncio.create_task(run_matrix_heartbeat())


# ── WebSocket ─────────────────────────────────────────────────────
@app.websocket("/ws/signals")
async def websocket_signals(ws: WebSocket):
    await ws.accept()
    connected_clients.add(ws)
    logger.info(f"[WS] Client connected. Total: {len(connected_clients)}")
    try:
        # Push all cached signals on connect
        for sym, tfs in latest_signals.items():
            for tf, sd in tfs.items():
                await ws.send_text(json.dumps({"type": "signal", "data": sd}, cls=NumpyEncoder))
        while True:
            text = await ws.receive_text()
            if text == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        connected_clients.discard(ws)


# ── REST Endpoints ────────────────────────────────────────────────
@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "symbols": SYMBOLS,
        "active_timeframes": ACTIVE_TIMEFRAMES,
        "ml_backend": ML_BACKEND,
        "analysis_interval_1m_sec": INTERVAL_1M,
        "analysis_interval_other_sec": INTERVAL_OTHER,
        "connected_ws_clients": len(connected_clients),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/price")
async def get_price(symbol: str = "ES"):
    loop  = asyncio.get_event_loop()
    price = await loop.run_in_executor(None, get_current_price, symbol)
    return {"symbol": symbol, "price": price,
            "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/prices")
async def get_all_prices_endpoint():
    """Latest price for all 3 symbols."""
    loop   = asyncio.get_event_loop()
    result = {}
    for sym in SYMBOLS:
        p = await loop.run_in_executor(None, get_current_price, sym)
        result[sym] = p
    return result


@app.get("/api/signal")
def get_signal(symbol: str = "ES", timeframe: str = "1m"):
    tf  = timeframe if timeframe in ACTIVE_TIMEFRAMES else "1m"
    sym = symbol.upper() if symbol.upper() in SYMBOLS else SYMBOLS[0]
    sig = latest_signals.get(sym, {}).get(tf)
    if sig:
        return sig
    return {"signal_strength": "NO_TRADE", "symbol": sym, "timeframe": tf,
            "notes": ["Analysis pending — try again shortly"]}


@app.get("/api/signals/all")
def get_all_signals():
    """All latest signals: {symbol: {tf: signal}}"""
    return latest_signals


@app.get("/api/chart/{timeframe}")
async def get_chart_data(timeframe: str, bars: int = 200, symbol: str = "ES"):
    if timeframe not in ["1m","5m","15m","30m","1h","4h","1d"]:
        raise HTTPException(400, "Invalid timeframe")
    loop = asyncio.get_event_loop()
    df   = await loop.run_in_executor(None, fetch_ohlcv, timeframe, bars, None, None, symbol)
    if df is None or df.empty:
        raise HTTPException(503, "No data available")
    records = [{"time": int(ts.timestamp()),
                "open": round(float(r["open"]),4), "high": round(float(r["high"]),4),
                "low":  round(float(r["low"]),4),  "close":round(float(r["close"]),4),
                "volume": int(r.get("volume",0))}
               for ts, r in df.iterrows()]
    return {"symbol": symbol, "timeframe": timeframe, "bars": records}


# ── Trade log ─────────────────────────────────────────────────────
class TradeIn(BaseModel):
    signal_id:   Optional[int] = None
    symbol:      str = "ES"
    timeframe:   str = "1m"
    direction:   str
    entry_price: float
    stop_loss:   float
    tp1:         float
    tp2:         float
    risk_usd:    float = 500.0
    contracts:   Optional[float] = None
    notes:       Optional[str] = ""

class CloseTradeIn(BaseModel):
    trade_id:    int
    exit_price:  float
    exit_reason: str
    entry_price: float
    stop_loss:   float
    direction:   str
    risk_usd:    float = 500.0

@app.post("/api/trades")
def create_trade(t: TradeIn):
    tid = log_trade(t.dict())
    return {"trade_id": tid, "message": "Trade logged"}

@app.put("/api/trades/close")
def close_trade_endpoint(c: CloseTradeIn):
    return close_trade(c.trade_id, c.exit_price, c.exit_reason,
                       c.entry_price, c.stop_loss, c.direction, c.risk_usd)

@app.get("/api/trades")
def list_trades(limit: int = 100):
    return get_trades(limit)

@app.get("/api/signals/history")
def signal_history(limit: int = 50, symbol: str = None, timeframe: str = None):
    sigs = get_signals(limit)
    if symbol:
        sigs = [s for s in sigs if s.get("symbol","").upper() == symbol.upper()]
    if timeframe:
        sigs = [s for s in sigs if s.get("timeframe") == timeframe]
    return sigs

@app.get("/api/stats")
def stats():
    return get_stats()


# ── Backtest ──────────────────────────────────────────────────────
@app.post("/api/backtest")
async def run_backtest_endpoint(
    timeframe: str = "5m",
    start: Optional[str] = None,
    end: Optional[str] = None,
    bars: int = 300,
    min_signal: str = "ENTER",
    skip_timing: bool = False,
    symbol: str = "ES",
):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))

    def _run():
        import importlib, backtest as bt
        importlib.reload(bt)

        results = {"config": {
            "symbol": symbol, "timeframe": timeframe,
            "start": start, "end": end, "bars": "auto" if start else bars,
            "min_signal": min_signal, "skip_timing": skip_timing,
        }, "summary": {}, "trades": []}

        try:
            df = fetch_ohlcv(timeframe, bars=bars, start=start, end=end, symbol=symbol)
            if df is None or df.empty or len(df) < 60:
                return {"error": "Not enough data", "config": results["config"]}

            from strategy import orr_analyzer as _orr
            if skip_timing:
                _orr._is_in_timing_window = lambda: True

            from strategy.orr_analyzer import diagnose_trade_failure
            import pytz
            BROKERAGE = 10.0
            trades = []
            active_until = 0

            for i in range(50, len(df) - 5):
                if i < active_until:
                    continue
                bar_ts = df.index[i]
                if not skip_timing and not bt.is_bar_in_timing_window(bar_ts):
                    continue
                window = df.iloc[max(0, i-300):i+1]
                try:
                    sig = bt.analyze(window, timeframe=timeframe, symbol=symbol)
                except Exception:
                    continue
                valid = {"ENTER"} if min_signal == "ENTER" else {"ENTER", "CAUTION"}
                if sig.signal_strength not in valid or not sig.entry_price or not sig.stop_loss:
                    continue

                try:
                    ist_t = bar_ts.tz_convert(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M IST")
                except Exception:
                    ist_t = str(bar_ts)

                result = bt.simulate_trade_outcome(
                    df, i, sig.entry_price, sig.stop_loss,
                    sig.tp1, sig.tp2, sig.direction)

                diagnosis = None
                if result["outcome"] == "SL":
                    diagnosis = diagnose_trade_failure(window, sig.entry_price, sig.stop_loss, sig.direction, timeframe)

                raw_pnl = result["r_multiple"] * 50.0
                active_until = i + result["bars_held"] + 3

                trades.append({
                    "bar_time":   ist_t,
                    "signal":     sig.signal_strength,
                    "symbol":     symbol,
                    "dir":        sig.direction,
                    "factors":    sig.factors_hit,
                    "prob":       round(sig.final_probability, 3),
                    "entry":      round(sig.entry_price, 2),
                    "sl":         round(sig.stop_loss, 2),
                    "tp1":        round(sig.tp1, 2),
                    "tp2":        round(sig.tp2, 2),
                    "outcome":    result["outcome"],
                    "r_multiple": result["r_multiple"],
                    "pnl_usd":    round(raw_pnl - BROKERAGE, 2),
                    "diagnosis":  diagnosis,
                    "bars_held":  result["bars_held"],
                })

            import pandas as pd
            if trades:
                tdf = pd.DataFrame(trades)
                wins  = len(tdf[tdf["r_multiple"] > 0])
                total = len(tdf)
                results["summary"] = {
                    "total_trades":    total,
                    "win_rate":        round(wins/total, 4) if total else 0,
                    "avg_r":           round(float(tdf["r_multiple"].mean()), 3),
                    "total_pnl_usd":   round(float(tdf["pnl_usd"].sum()), 2),
                    "tp2_hits":        len(tdf[tdf["outcome"]=="TP2"]),
                    "tp1_hits":        len(tdf[tdf["outcome"]=="TP1"]),
                    "sl_hits":         len(tdf[tdf["outcome"]=="SL"]),
                }
            else:
                results["summary"] = {"total_trades":0,"win_rate":0,"avg_r":0,
                                       "total_pnl_usd":0,"tp2_hits":0,"tp1_hits":0,"sl_hits":0}
            results["trades"] = trades
        except Exception as e:
            results["error"] = str(e)
        return results

    loop   = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _run)
    return result


# ── Root ──────────────────────────────────────────────────────────
@app.get("/")
def root():
    idx = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(idx):
        return FileResponse(idx)
    return {"message": "ORR API v2. Frontend not found."}
