"""
prompt_builder.py
=================
Builds structured text prompts from market feature data for the LLM.
Also parses the model's JSON response back into a numeric adjustment.

WHY A SEPARATE MODULE?
-----------------------
- Prompts are easy to iterate without touching analysis logic
- Same prompt format works for Ollama, LM Studio, OpenAI-compat, etc.
- Response parsing is centralised and robust (handles malformed JSON)

EXPECTED MODEL RESPONSE FORMAT (JSON)
--------------------------------------
{
  "signal_quality": "strong" | "moderate" | "weak" | "invalid",
  "confidence":     0.0 to 1.0,
  "adjustment":     -0.10 to 0.10,
  "reasoning":      "brief explanation",
  "risk_flags":     ["optional", "list", "of", "risk", "notes"]
}
"""

import json
import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────── #
#  SYSTEM CONTEXT  (injected once — describes the strategy to the LLM)
# ─────────────────────────────────────────────────────────────────── #
STRATEGY_CONTEXT = """
You are a quantitative trading signal evaluator for the Opening Range Reversal (ORR) strategy.

The ORR strategy trades XAUUSD (Gold) using these rules:
1. FACTOR 1 - Strong Flush: Price makes a fast, aggressive directional move into a pre-marked key support/resistance zone. Must be > 1.5x ATR in momentum.
2. FACTOR 2 - Timing Window: The flush must occur within 15-30 minutes of the US market open (09:45-10:00 ET).
3. FACTOR 3 - Trend Structure Break: The prior trend must show signs of failing (Higher Low after downtrend, or Lower High after uptrend).
4. FACTOR 4 - Momentum Confirmation Candle: A large candle (> 1.2x ATR body) breaks above/below the prior swing high/low in the reversal direction.

Signal Scoring:
- 4/4 factors = 85% base probability → ENTER
- 3/4 factors = 65% base probability → CAUTION
- < 3 factors = NO TRADE

Your job: Given the market data below, assess the QUALITY of the setup and provide a probability adjustment between -0.10 and +0.10.

Be conservative — only boost probability for genuinely strong setups. When in doubt, prefer 0.0 (neutral).
"""

# ─────────────────────────────────────────────────────────────────── #
#  PROMPT BUILDER
# ─────────────────────────────────────────────────────────────────── #
def build_analysis_prompt(features: dict) -> str:
    """
    Convert feature dictionary to a structured LLM prompt.
    The prompt is minimal and data-dense — no fluff.

    Args:
        features: dict from ml_classifier.extract_features()

    Returns:
        str: Complete prompt ready to send to the model
    """

    signal_description = _describe_signal(features)

    prompt = f"""
{STRATEGY_CONTEXT}

--- CURRENT MARKET DATA (XAUUSD) ---
Current Price:        {features.get('current_price', 'N/A')}
ATR (14-period):      {features.get('atr', 'N/A')}
Flush Strength:       {features.get('flush_strength_pts', 0):.4f} pts ({features.get('flush_atr_ratio', 0):.2f}x ATR)
Flush Direction:      {features.get('flush_direction', 'none')}
FVG Count (30 bars):  {features.get('fvg_count', 0)} (>=2 is meaningful, >=3 is strong)
Prior Trend:          {features.get('prior_trend', 'ranging')}
Trend Broke:          {features.get('trend_broke', False)}
Volume Spike Ratio:   {features.get('volume_spike_ratio', 1.0):.2f}x (1.0=normal, >2.0=strong)
Bar Body / ATR:       {features.get('bar_body_atr_ratio', 0):.2f}x
10-Bar Price Change:  {features.get('price_change_pct_10bars', 0):.4f}%
Close Position:       {features.get('close_position_in_bar', 0.5):.2f} (0=bottom, 1=top of bar)

--- SETUP DESCRIPTION ---
{signal_description}

--- YOUR TASK ---
Evaluate this setup and respond with ONLY valid JSON in this exact format:
{{
  "signal_quality": "<strong|moderate|weak|invalid>",
  "confidence": <0.0 to 1.0>,
  "adjustment": <-0.10 to 0.10>,
  "reasoning": "<one sentence max>",
  "risk_flags": ["<flag1>", "<flag2>"]
}}

Rules:
- adjustment > 0   → setup is better than rule-engine suggests
- adjustment < 0   → setup has hidden weaknesses
- adjustment = 0   → neutral, trust the rule-engine score
- risk_flags can be empty []
- Do NOT include anything outside the JSON object.
"""
    return prompt.strip()


def _describe_signal(features: dict) -> str:
    """Generate a human-readable signal description for the model context."""
    direction  = features.get("flush_direction", "none")
    trend      = features.get("prior_trend", "ranging")
    broke      = features.get("trend_broke", False)
    fvg        = features.get("fvg_count", 0)
    flush_rat  = features.get("flush_atr_ratio", 0)
    vol_spike  = features.get("volume_spike_ratio", 1.0)

    parts = []

    if direction != "none":
        parts.append(f"Price made a {'downward' if direction == 'long' else 'upward'} flush ({flush_rat:.1f}x ATR) suggesting a potential {direction.upper()} reversal.")
    else:
        parts.append("No clear flush direction detected.")

    parts.append(f"Prior trend was {trend}. Trend structure {'HAS broken' if broke else 'has NOT broken yet'}.")

    if fvg >= 3:
        parts.append(f"Strong FVG stack ({fvg} gaps) — market is significantly imbalanced.")
    elif fvg >= 2:
        parts.append(f"FVG confirmation ({fvg} gaps) supports reversal thesis.")
    else:
        parts.append("Weak FVG signal (<2 gaps).")

    if vol_spike > 2.0:
        parts.append(f"Significant volume spike ({vol_spike:.1f}x avg) confirms institutional activity.")
    elif vol_spike > 1.5:
        parts.append(f"Moderate volume increase ({vol_spike:.1f}x avg).")

    return " ".join(parts)


# ─────────────────────────────────────────────────────────────────── #
#  RESPONSE PARSER
# ─────────────────────────────────────────────────────────────────── #
def parse_model_response(raw_text: str) -> tuple[float, dict]:
    """
    Parse the model's response into (adjustment, parsed_dict).
    Handles:
      - Clean JSON
      - JSON embedded in markdown code blocks
      - Partially valid JSON
      - Completely malformed output (returns 0.0)

    Returns:
        (adjustment: float, parsed: dict)
    """
    if not raw_text:
        return 0.0, {"error": "empty response"}

    # Strip markdown code fences if present
    text = raw_text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE)

    # Try to extract JSON object
    json_match = re.search(r"\{.*\}", text, re.DOTALL)
    if not json_match:
        logger.warning(f"[Prompt] No JSON found in model response: {raw_text[:200]}")
        return 0.0, {"error": "no_json", "raw": raw_text[:200]}

    try:
        parsed = json.loads(json_match.group())
    except json.JSONDecodeError as e:
        logger.warning(f"[Prompt] JSON parse error: {e} | text: {json_match.group()[:200]}")
        return 0.0, {"error": f"json_decode: {e}", "raw": json_match.group()[:200]}

    # Extract and clamp adjustment
    adj = parsed.get("adjustment", 0.0)
    try:
        adj = float(adj)
        adj = max(-0.10, min(0.10, adj))           # clamp to allowed range
    except (TypeError, ValueError):
        adj = 0.0

    # Map quality → sanity check on adjustment direction
    quality = parsed.get("signal_quality", "").lower()
    if quality == "invalid" and adj > 0:
        adj = 0.0    # don't boost an invalid signal
    if quality == "strong" and adj < -0.05:
        adj = 0.0    # model contradiction — go neutral

    parsed["adjustment"] = adj   # normalise back into dict
    return adj, parsed
