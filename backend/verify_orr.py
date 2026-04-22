import requests
import json
import time

BASE_URL = "http://127.0.0.1:8005/api/backtest"

def run_test(start_date, timeframe):
    payload = {
        "timeframe": timeframe,
        "start": start_date,
        "min_signal": "CAUTION", # Loose for frequency as requested
        "skip_timing": False
    }
    try:
        response = requests.post(BASE_URL, json=payload, timeout=60)
        data = response.json()
        if "summary" in data:
            s = data["summary"]
            print(f"[{start_date} | {timeframe}] WR: {s['win_rate']*100:.1f}% | Trades: {s['total_trades']} | PnL: ${s['total_pnl_usd']}")
            return s["win_rate"]
        else:
            print(f"Error in {start_date} {timeframe}: {data.get('error')}")
            return 0
    except Exception as e:
        print(f"Connection error: {e}")
        return 0

scenarios = [
    ("2026-03-01", "15m"),
    ("2026-03-01", "5m"),
    ("2026-04-01", "15m"),
    ("2026-04-01", "5m")
]

print("Starting ORR Victory Loop...")
while True:
    results = []
    for date, tf in scenarios:
        wr = run_test(date, tf)
        results.append(wr)
    
    avg_wr = sum(results) / len(results)
    print(f"---> Average Win Rate: {avg_wr*100:.1f}%")
    
    if avg_wr >= 0.70:
        print("!!! TARGET REACHED: 70% !!!")
        break
    else:
        print("Tuning engine... (Waiting for code tweaks)")
        time.sleep(10)
