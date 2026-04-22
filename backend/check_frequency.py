import requests
import json

BASE_URL = "http://127.0.0.1:8005/api/backtest"

def check_month(start_date):
    payload = {
        "timeframe": "15m",
        "start": start_date,
        "min_signal": "CAUTION"
    }
    r = requests.post(BASE_URL, json=payload)
    data = r.json()
    if "summary" in data:
        s = data["summary"]
        print(f"REPORT [{start_date}]: {s['total_trades']} Trades | {s['win_rate']*100:.1f}% WR | PnL: ${s['total_pnl_usd']}")
    else:
        print(f"Error checking {start_date}: {data.get('error')}")

print("FINAL QUALITY CHECK...")
check_month("2026-04-01")
check_month("2026-03-01")
