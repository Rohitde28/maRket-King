import requests
import json
import time

BASE_URL = "http://127.0.0.1:8000/api/backtest" # Using Port 8000 as per main.py update

def run_test(start_date, timeframe, symbol):
    params = {
        "timeframe": timeframe,
        "start": start_date,
        "min_signal": "ENTER", # High Accuracy Mode
        "skip_timing": False,
        "symbol": symbol
    }
    try:
        response = requests.post(BASE_URL, params=params, timeout=60)
        data = response.json()
        if "summary" in data:
            s = data["summary"]
            print(f"[{symbol} | {start_date} | {timeframe}] WR: {s['win_rate']*100:.1f}% | Trades: {s['total_trades']} | PnL: ${s['total_pnl_usd']}")
            return s["win_rate"], s["total_trades"]
        else:
            print(f"Error in {symbol} {start_date}: {data.get('error')}")
            return 0, 0
    except Exception as e:
        print(f"Connection error: {e}")
        return 0, 0

scenarios = [
    # symbol, start_date, timeframe
    ("GC=F", "2026-03-01", "15m"),
    ("GC=F", "2026-04-01", "15m"),
    ("NQ=F", "2026-04-01", "15m"),
    ("EURUSD=X", "2026-04-01", "15m")
]

print("Starting FINAL QUALITY ASSURANCE LOOP...")
results = []
for sym, date, tf in scenarios:
    wr, trades = run_test(date, tf, sym)
    results.append(wr)

avg_wr = sum(results) / len(results)
print(f"\n=========================================")
print(f"FINAL AVERAGE WIN RATE: {avg_wr*100:.1f}%")
print(f"=========================================")

if avg_wr >= 0.60:
    print("!!! ACCURACY TARGET ACHIEVED (>60%) !!!")
else:
    print("!!! ACCURACY TARGET FAILED - RETUNING REQUIRED !!!")
