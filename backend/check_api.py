import requests

BASE_URL = "http://127.0.0.1:8005/api/backtest"
payload = {
    "timeframe": "15m",
    "start": "2026-03-01",
    "min_signal": "CAUTION",
    "skip_timing": False
}

r = requests.post(BASE_URL, json=payload)
data = r.json()
print(f"Status: {r.status_code}")
if "summary" in data:
    print(f"Summary: {data['summary']}")
if "trades" in data:
    print(f"Trades found: {len(data['trades'])}")
if "error" in data:
    print(f"Error: {data['error']}")
if "config" in data:
    print(f"Config used: {data['config']}")
