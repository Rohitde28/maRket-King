# ORR Trading Signal Platform

A fully automated backend for the Opening Range Reversal (ORR) trading strategy. It connects to Yahoo Finance (or MT5/OANDA) to analyze price action, identify high-probability setups at the NY open, and broadcast signals and trade outcomes to a web frontend.

## Features

- **Automated Analysis:** Checks 4 key factors (Flush, Timing Window, Trend Break, Momentum Candle).
- **Timezone Aware:** NY timing windows (09:45 & 10:00 ET) are automatically calculated and adjusted for Daylight Saving Time using `pytz`, no matter where your server is located (e.g., IST in India).
- **Live Outcome Monitoring:** Tracks fired signals in the background and checks every 30 seconds if Stop Loss (SL) or Take Profit (TP1/TP2) has been hit.
- **Historical Backtesting:** Run the exact same analysis engine on past data (by date range) without look-ahead bias.
- **Local DB:** All signals and trade outcomes are stored locally in a SQLite database (`backend/data/trades.db`).
- **AI Integration (Optional):** Connects to a local Ollama model to adjust probabilities based on advanced reasoning.

## Getting Started

### 1. Requirements

- Python 3.10+
- (Optional) Ollama running locally if you want AI reasoning

### 2. Installation

Open a terminal in the `backend` folder and install dependencies:

```bash
cd backend
pip install -r requirements.txt
```

### 3. Configuration

By default, the system uses Yahoo Finance (15m delayed) and a Rule-Based fallback for ML.
To change settings, copy the `.env.example` file to `.env`:

```bash
cp .env.example .env
```

Edit `.env` to connect to real-time data or Ollama:
- `DATA_SOURCE=METATRADER5` (Requires Windows and MT5 terminal open)
- `ML_BACKEND=OLLAMA`
- `OLLAMA_MODEL=llama3.2`

### 4. Running the Server

Start the FastAPI backend server:

```bash
cd backend
python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

> **Note**: The `--reload` flag automatically restarts the server whenever you save changes to your code!

The server will:
1. Initialize the SQLite database.
2. Start the analysis loop (runs every 60 seconds by default on 1m, 5m, 15m timeframes).
3. Start the outcome monitor (checks open trades every 30 seconds).

### 5. Viewing the Frontend

The simple functional frontend is served directly by the backend!

Open your browser and go to:
**http://localhost:8000**

From here you can:
- **LIVE MODE:** View the NY clock, countdown, live signals, the 4-factor checklist, and real-time outcomes (✅ TP1 Hit, ❌ SL Hit).
- **BACKTEST MODE:** Select a timeframe and a `Start Date`, then click run. The system will fetch historical data from that date to today and print a full performance report (Win Rate, Expectancy, P&L).

## Logs and Data

- **Database:** All data is stored in `backend/data/trades.db`. You can view it using any SQLite viewer (like DB Browser for SQLite).
- **Terminal Logs:** The backend terminal will print outcomes (e.g., `[Monitor] Signal outcome: TP1 @ 2354.20`).
- **Backtest Results:** When run via the frontend, results appear on screen. When run via the standalone script (`python backtest.py`), a `backtest_results.json` file is generated in the `backend` folder.
