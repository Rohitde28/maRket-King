"""
ml_classifier.py
================
AI Signal Classifier — Local Model Integration
================================================
This module sends market data to a local LLM via Ollama (or compatible
endpoints) and asks it to assess the quality of a potential ORR signal.

HOW IT WORKS
------------
1. The ORR rule-engine (orr_analyzer.py) detects the 4 factors and builds
   a structured feature set (price action, ATR, FVG count, trend state, etc.)
2. prompt_builder.py converts those features into a structured text prompt.
3. This module sends the prompt to the local model and parses its response
   into a numeric probability adjustment (-0.10 to +0.10).
4. The final probability shown to the user = base_prob + ml_adjustment.

MODEL SELECTION
---------------
Default recommended model:  llama3.2  (fast, small, good reasoning)
Finance-tuned alternative:  qwen2.5:7b or mistral:7b

All configurable via .env  — no code changes needed.

SUPPORTED BACKENDS (set ML_BACKEND= in .env)
--------------------------------------------
  OLLAMA         → Local Ollama (default)  http://localhost:11434
  OPENAI_COMPAT  → Any OpenAI-compatible API (LM Studio, llama.cpp server)
  RULE_BASED     → Pure heuristics, no LLM (fallback, always works)
  DISABLED       → Skip ML step entirely (base probability only)

QUICKSTART
----------
  1. Install Ollama:  https://ollama.com/download
  2. Pull a model:    ollama pull llama3.2
  3. Start Ollama:    ollama serve          (or it auto-starts on Windows)
  4. Set in .env:     ML_BACKEND=OLLAMA
                      OLLAMA_MODEL=llama3.2
  5. Backend auto-connects on first signal analysis.
"""

import os
import json
import logging
import asyncio
from typing import Optional
import numpy as np
import pandas as pd
import httpx
from dotenv import load_dotenv

from strategy.prompt_builder import build_analysis_prompt, parse_model_response

load_dotenv()
logger = logging.getLogger(__name__)

# ── Configuration (all from .env) ──────────────────────────────────────────
ML_BACKEND     = os.getenv("ML_BACKEND",     "RULE_BASED").upper()
OLLAMA_HOST    = os.getenv("OLLAMA_HOST",    "http://localhost:11434")
OLLAMA_MODEL   = os.getenv("OLLAMA_MODEL",   "llama3.2")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "30"))

# OpenAI-compatible endpoint (LM Studio, llama.cpp server, vLLM, etc.)
OPENAI_COMPAT_URL    = os.getenv("OPENAI_COMPAT_URL",    "http://localhost:1234/v1")
OPENAI_COMPAT_MODEL  = os.getenv("OPENAI_COMPAT_MODEL",  "local-model")
OPENAI_COMPAT_APIKEY = os.getenv("OPENAI_COMPAT_APIKEY", "not-needed")

# Inference parameters
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.1"))   # low = deterministic
LLM_MAX_TOKENS  = int(os.getenv("LLM_MAX_TOKENS",    "256"))


# ============================================================================ #
#  OLLAMA Backend
# ============================================================================ #
async def _call_ollama(prompt: str) -> Optional[str]:
    """
    Call local Ollama instance using the /api/generate endpoint.
    Ollama must be running:  ollama serve

    Endpoint reference: https://github.com/ollama/ollama/blob/main/docs/api.md
    """
    url     = f"{OLLAMA_HOST}/api/generate"
    payload = {
        "model":   OLLAMA_MODEL,
        "prompt":  prompt,
        "stream":  False,
        "options": {
            "temperature":  LLM_TEMPERATURE,
            "num_predict":  LLM_MAX_TOKENS,
            "stop":         ["\n\n", "---"],   # stop generation at these tokens
        }
    }

    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            text = data.get("response", "").strip()
            logger.debug(f"[Ollama] Raw response: {text[:200]}")
            return text
    except httpx.ConnectError:
        logger.warning(
            f"[Ollama] Cannot connect to {OLLAMA_HOST}. "
            "Is Ollama running? Run: ollama serve"
        )
    except httpx.TimeoutException:
        logger.warning(f"[Ollama] Timeout after {OLLAMA_TIMEOUT}s — model may be loading")
    except Exception as e:
        logger.error(f"[Ollama] Unexpected error: {e}", exc_info=True)
    return None


async def _call_ollama_chat(prompt: str) -> Optional[str]:
    """
    Alternative: use Ollama's /api/chat endpoint (OpenAI-style messages).
    Some models respond better with system + user message structure.
    Toggle via env:  OLLAMA_USE_CHAT=true
    """
    url     = f"{OLLAMA_HOST}/api/chat"
    payload = {
        "model":    OLLAMA_MODEL,
        "stream":   False,
        "messages": [
            {
                "role":    "system",
                "content": (
                    "You are a professional quantitative trading analyst specialising in "
                    "gold futures (XAUUSD). Your job is to assess whether a given set of "
                    "market conditions represents a high-quality Opening Range Reversal "
                    "setup. Be concise and structured. Always respond in valid JSON."
                )
            },
            {
                "role":    "user",
                "content": prompt
            }
        ],
        "options": {
            "temperature": LLM_TEMPERATURE,
            "num_predict": LLM_MAX_TOKENS,
        }
    }

    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            text = data.get("message", {}).get("content", "").strip()
            logger.debug(f"[Ollama/chat] Response: {text[:200]}")
            return text
    except Exception as e:
        logger.error(f"[Ollama/chat] Error: {e}")
    return None


# ============================================================================ #
#  OpenAI-Compatible Backend  (LM Studio, llama.cpp, vLLM, OpenRouter, etc.)
# ============================================================================ #
async def _call_openai_compat(prompt: str) -> Optional[str]:
    """
    Call any OpenAI-compatible /v1/chat/completions endpoint.

    Works with:
      - LM Studio:      http://localhost:1234/v1
      - llama.cpp:      http://localhost:8080/v1
      - vLLM:           http://localhost:8000/v1
      - OpenRouter:     https://openrouter.ai/api/v1  (set OPENAI_COMPAT_APIKEY)
      - Groq (free):    https://api.groq.com/openai/v1

    Set in .env:
        OPENAI_COMPAT_URL=http://localhost:1234/v1
        OPENAI_COMPAT_MODEL=local-model
        OPENAI_COMPAT_APIKEY=not-needed   # or your real key
    """
    url     = f"{OPENAI_COMPAT_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_COMPAT_APIKEY}",
        "Content-Type":  "application/json",
    }
    payload = {
        "model": OPENAI_COMPAT_MODEL,
        "messages": [
            {
                "role":    "system",
                "content": (
                    "You are a quantitative trading analyst. Assess OpeningRange Reversal "
                    "signals for gold futures. Respond only in JSON."
                )
            },
            {"role": "user", "content": prompt}
        ],
        "temperature": LLM_TEMPERATURE,
        "max_tokens":  LLM_MAX_TOKENS,
    }

    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"].strip()
            logger.debug(f"[OpenAI-compat] Response: {text[:200]}")
            return text
    except Exception as e:
        logger.error(f"[OpenAI-compat] Error: {e}")
    return None


# ============================================================================ #
#  Rule-Based Fallback (no LLM needed)
# ============================================================================ #
def _rule_based_adjustment(features: dict) -> float:
    """
    Pure heuristic adjuster. Used when ML_BACKEND=RULE_BASED or when
    the LLM call fails. Returns adjustment in [-0.10, +0.10].
    """
    score = 0.0

    flush_atr_ratio = features.get("flush_atr_ratio", 0)
    fvg_count       = features.get("fvg_count", 0)
    vol_spike       = features.get("volume_spike_ratio", 1.0)
    close_pos       = features.get("close_position_in_bar", 0.5)
    trend_broke     = features.get("trend_broke", False)

    if flush_atr_ratio > 2.5:  score += 0.05
    elif flush_atr_ratio > 1.8: score += 0.02

    if fvg_count >= 3:  score += 0.04
    elif fvg_count == 2: score += 0.02

    if vol_spike > 2.5:  score += 0.03
    elif vol_spike > 1.8: score += 0.01

    if close_pos > 0.70: score += 0.02
    elif close_pos < 0.30: score -= 0.02

    if trend_broke: score += 0.02

    return round(max(-0.10, min(0.10, score)), 4)


# ============================================================================ #
#  Feature Builder (shared by all backends)
# ============================================================================ #
def extract_features(df: pd.DataFrame, flush_strength: float,
                     fvg_count: int, trend_broke: bool,
                     prior_trend: str, direction: str) -> dict:
    """
    Build a rich feature dictionary from market context.
    This is passed both to the prompt builder and rule-based scorer.
    """
    if len(df) < 15:
        return {}

    last    = df.iloc[-1]
    atr_ser = (df["high"] - df["low"]).rolling(14).mean()
    atr     = float(atr_ser.iloc[-1]) if not atr_ser.empty else 1.0

    vol_avg   = float(df["volume"].iloc[-21:-1].mean()) if "volume" in df.columns else 0
    vol_spike = float(last.get("volume", 0)) / (vol_avg + 1e-9) if vol_avg > 0 else 1.0

    bar_range = float(last["high"] - last["low"])
    close_pos = float(last["close"] - last["low"]) / (bar_range + 1e-9)

    price_change_10 = float(df["close"].iloc[-1] - df["close"].iloc[-10]) if len(df) >= 10 else 0.0
    pct_change_10   = price_change_10 / (float(df["close"].iloc[-10]) + 1e-9) * 100

    return {
        "symbol":               "XAUUSD",
        "current_price":        round(float(last["close"]), 4),
        "atr":                  round(atr, 4),
        "flush_strength_pts":   round(flush_strength, 4),
        "flush_atr_ratio":      round(flush_strength / (atr + 1e-9), 3),
        "flush_direction":      direction,
        "fvg_count":            int(fvg_count),
        "trend_broke":          bool(trend_broke),
        "prior_trend":          prior_trend,
        "volume_spike_ratio":   round(min(vol_spike, 10.0), 2),
        "close_position_in_bar":round(close_pos, 3),
        "price_change_pct_10bars": round(pct_change_10, 4),
        "bar_body_size":        round(abs(float(last["close"]) - float(last["open"])), 4),
        "bar_body_atr_ratio":   round(abs(float(last["close"]) - float(last["open"])) / (atr + 1e-9), 3),
        "last_high":            round(float(last["high"]), 4),
        "last_low":             round(float(last["low"]),  4),
    }


# ============================================================================ #
#  Public Interface
# ============================================================================ #
async def get_ml_adjustment_async(df: pd.DataFrame, flush_strength: float,
                                  fvg_count: int, trend_broke: bool,
                                  prior_trend: str = "ranging",
                                  direction: str = "none") -> tuple[float, dict]:
    """
    Async version. Returns (adjustment_float, debug_info_dict).
    Called by the analysis engine for every signal evaluation.
    """
    features = extract_features(df, flush_strength, fvg_count,
                                trend_broke, prior_trend, direction)
    debug    = {"backend": ML_BACKEND, "features": features}

    if ML_BACKEND == "DISABLED":
        return 0.0, debug

    if ML_BACKEND == "RULE_BASED":
        adj = _rule_based_adjustment(features)
        debug["adjustment"] = adj
        debug["method"] = "rule_based"
        return adj, debug

    # ── LLM path ────────────────────────────────────────────────────
    prompt   = build_analysis_prompt(features)
    raw_resp = None

    if ML_BACKEND == "OLLAMA":
        use_chat = os.getenv("OLLAMA_USE_CHAT", "false").lower() == "true"
        if use_chat:
            raw_resp = await _call_ollama_chat(prompt)
        else:
            raw_resp = await _call_ollama(prompt)

    elif ML_BACKEND == "OPENAI_COMPAT":
        raw_resp = await _call_openai_compat(prompt)

    if raw_resp is None:
        logger.warning("[ML] LLM call failed — falling back to rule-based")
        adj = _rule_based_adjustment(features)
        debug["adjustment"] = adj
        debug["method"]     = "rule_based_fallback"
        debug["llm_error"]  = "No response from model"
        return adj, debug

    adj, parsed = parse_model_response(raw_resp)
    debug["raw_response"] = raw_resp[:500]
    debug["parsed"]       = parsed
    debug["adjustment"]   = adj
    debug["method"]       = f"llm_{ML_BACKEND.lower()}"
    return adj, debug


def get_ml_adjustment(df: pd.DataFrame, flush_strength: float,
                      fvg_count: int, trend_broke: bool,
                      prior_trend: str = "ranging",
                      direction: str = "none") -> float:
    """
    Sync wrapper — runs async call in the event loop.
    Used by the synchronous orr_analyzer path.
    """
    try:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        if loop.is_running():
            # We're inside an active running loop — we cannot `run_until_complete`.
            # Fallback to rule-based. The outer async layer handles Ollama perfectly.
            return _rule_based_adjustment(
                extract_features(df, flush_strength, fvg_count,
                                 trend_broke, prior_trend, direction)
            )
        else:
            adj, _ = loop.run_until_complete(
                get_ml_adjustment_async(df, flush_strength, fvg_count,
                                        trend_broke, prior_trend, direction)
            )
            return adj
    except Exception as e:
        logger.error(f"[ML] Sync wrapper error: {e}")
        return 0.0


async def check_ollama_health() -> dict:
    """
    Health check for the Ollama service.
    Called at startup and surfaced via /api/health endpoint.
    """
    url = f"{OLLAMA_HOST}/api/tags"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data     = resp.json()
            models   = [m["name"] for m in data.get("models", [])]
            has_model = OLLAMA_MODEL in models or any(
                OLLAMA_MODEL in m for m in models
            )
            return {
                "ollama_running":    True,
                "target_model":      OLLAMA_MODEL,
                "model_available":   has_model,
                "available_models":  models,
                "host":              OLLAMA_HOST,
            }
    except Exception as e:
        return {
            "ollama_running":  False,
            "error":           str(e),
            "host":            OLLAMA_HOST,
            "hint":            "Run: ollama serve   |   Pull model: ollama pull llama3.2",
        }
