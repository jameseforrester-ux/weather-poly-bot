"""
polymarket_service.py
─────────────────────
Interfaces with Polymarket's Gamma REST API to:
  • Search for temperature / weather markets
  • Return current YES/NO prices
  • Compute trading edge against our model's probability estimate
"""

import json
import re
from typing import Optional

import httpx

from config import (
    EDGE_THRESHOLD, HTTP_TIMEOUT,
    PM_GAMMA_URL, PM_MARKET_URL, PRED_UNCERTAINTY_C,
)
from weather_service import f_to_c, prob_exceeds

# ── Keywords used to find weather markets ─────────────────
WEATHER_KEYWORDS = ["temperature", "high temp", "weather", "degrees",
                    "fahrenheit", "celsius", "heat", "cold"]

# ─────────────────────────────────────────────────────────
#  Raw API helpers
# ─────────────────────────────────────────────────────────

async def _get_markets(keyword: str, limit: int = 50) -> list[dict]:
    """Search Gamma API for active markets matching keyword."""
    url = f"{PM_GAMMA_URL}/markets"
    params = {
        "active":   "true",
        "closed":   "false",
        "limit":    limit,
        "keyword":  keyword,
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            return r.json() if isinstance(r.json(), list) else []
    except Exception:
        return []


async def _get_all_weather_markets(limit: int = 100) -> list[dict]:
    """Combine results from multiple keyword searches, deduplicated."""
    seen: set[str] = set()
    combined: list[dict] = []
    for kw in WEATHER_KEYWORDS[:4]:          # avoid rate-limit
        for m in await _get_markets(kw, limit=30):
            mid = m.get("id") or m.get("conditionId", "")
            if mid and mid not in seen:
                seen.add(mid)
                combined.append(m)
    return combined


# ─────────────────────────────────────────────────────────
#  Parsing helpers
# ─────────────────────────────────────────────────────────

def _parse_prices(market: dict) -> tuple[Optional[float], Optional[float]]:
    """Return (yes_price, no_price) in [0,1]."""
    raw = market.get("outcomePrices") or market.get("bestBid") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None, None
    if isinstance(raw, list) and len(raw) >= 2:
        try:
            return float(raw[0]), float(raw[1])
        except Exception:
            return None, None
    return None, None


def _parse_outcomes(market: dict) -> tuple[str, str]:
    raw = market.get("outcomes", '["Yes","No"]')
    if isinstance(raw, str):
        try:
            outcomes = json.loads(raw)
        except Exception:
            outcomes = ["Yes", "No"]
    else:
        outcomes = raw
    yes = outcomes[0] if len(outcomes) > 0 else "Yes"
    no  = outcomes[1] if len(outcomes) > 1 else "No"
    return yes, no


def _extract_threshold_f(question: str) -> Optional[float]:
    """
    Try to extract a Fahrenheit threshold from a market question.
    Handles patterns like:
      "Will the high temperature in NYC exceed 75°F?"
      "NYC high temp above 68 degrees F"
      "Daily max > 72°F?"
    """
    patterns = [
        r'(\d+(?:\.\d+)?)\s*°?\s*[Ff](?:ahrenheit)?(?:\b|°)',
        r'(\d+(?:\.\d+)?)\s*degrees?\s*[Ff](?:ahrenheit)?',
        r'(?:above|exceed|over|>)\s*(\d+(?:\.\d+)?)\s*°?[Ff]?',
    ]
    for pat in patterns:
        m = re.search(pat, question, re.IGNORECASE)
        if m:
            return float(m.group(1))
    return None


def _extract_threshold_c(question: str) -> Optional[float]:
    """Extract a Celsius threshold."""
    patterns = [
        r'(\d+(?:\.\d+)?)\s*°?\s*[Cc](?:elsius)?(?:\b|°)',
        r'(\d+(?:\.\d+)?)\s*degrees?\s*[Cc](?:elsius)?',
    ]
    for pat in patterns:
        m = re.search(pat, question, re.IGNORECASE)
        if m:
            return float(m.group(1))
    return None


def _market_url(market: dict) -> str:
    slug = market.get("slug") or market.get("groupItemTitle", "")
    if slug:
        return PM_MARKET_URL.format(slug=slug)
    mid = market.get("id", "")
    return f"https://polymarket.com/market/{mid}"


# ─────────────────────────────────────────────────────────
#  Edge computation
# ─────────────────────────────────────────────────────────

def compute_edge(market: dict, predicted_c: float) -> Optional[dict]:
    """
    Given a market and our predicted high temp (°C), compute:
      • threshold extracted from the question
      • our model's probability of YES
      • edge vs. current YES price
    Returns None if we can't parse a threshold.
    """
    question = market.get("question", "")
    yes_price, no_price = _parse_prices(market)
    if yes_price is None:
        return None

    # Try to find threshold in °F first, convert; then try °C
    threshold_c = None
    thr_f = _extract_threshold_f(question)
    if thr_f is not None:
        threshold_c = f_to_c(thr_f)
        thr_display = f"{thr_f}°F / {threshold_c:.1f}°C"
    else:
        thr_c = _extract_threshold_c(question)
        if thr_c is not None:
            threshold_c = thr_c
            thr_display = f"{thr_c:.1f}°C / {thr_c * 9/5 + 32:.1f}°F"
        else:
            return None   # can't parse threshold

    our_prob = prob_exceeds(predicted_c, threshold_c)
    edge_yes = our_prob - yes_price        # +ve means market under-prices YES
    edge_no  = (1 - our_prob) - no_price  # +ve means market under-prices NO

    best_edge = max(edge_yes, edge_no)
    best_side = "YES" if edge_yes >= edge_no else "NO"
    best_price = yes_price if best_side == "YES" else no_price

    return {
        "market_id":   market.get("id", ""),
        "question":    question,
        "slug":        market.get("slug", ""),
        "url":         _market_url(market),
        "yes_price":   round(yes_price, 3),
        "no_price":    round(no_price,  3),
        "threshold_c": threshold_c,
        "thr_display": thr_display,
        "our_prob":    round(our_prob, 3),
        "edge_yes":    round(edge_yes, 3),
        "edge_no":     round(edge_no,  3),
        "best_edge":   round(best_edge, 3),
        "best_side":   best_side,
        "best_price":  round(best_price, 3),
        "has_edge":    best_edge >= EDGE_THRESHOLD,
        "end_date":    market.get("endDateIso") or market.get("endDate", ""),
        "volume":      market.get("volume", 0),
        "outcomes":    _parse_outcomes(market),
    }


# ─────────────────────────────────────────────────────────
#  Public interface
# ─────────────────────────────────────────────────────────

async def search_temperature_markets(location_keyword: str,
                                     predicted_c: float) -> dict:
    """
    Returns {
      "all_markets": [...],
      "edge_markets": [...],   # only those with edge ≥ threshold
      "count": int,
    }
    """
    raw_markets = await _get_all_weather_markets()

    # Filter to markets that mention the location keyword
    kw_lower = location_keyword.lower()
    relevant  = [
        m for m in raw_markets
        if kw_lower in (m.get("question", "") + " " + m.get("slug", "")).lower()
    ]
    if not relevant:
        # Broader: any temperature market (user may want to browse)
        relevant = raw_markets[:20]

    results = []
    for m in relevant:
        edge = compute_edge(m, predicted_c)
        if edge:
            results.append(edge)

    results.sort(key=lambda x: -x["best_edge"])
    edge_markets = [r for r in results if r["has_edge"]]

    return {
        "all_markets":  results,
        "edge_markets": edge_markets,
        "count":        len(results),
    }


async def get_market_by_id(market_id: str) -> Optional[dict]:
    url = f"{PM_GAMMA_URL}/markets/{market_id}"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.json()
    except Exception:
        return None


async def get_current_price(market_id: str) -> Optional[tuple[float, float]]:
    """Return (yes_price, no_price) or None."""
    m = await get_market_by_id(market_id)
    if m:
        return _parse_prices(m)
    return None


# ─────────────────────────────────────────────────────────
#  Formatting helpers
# ─────────────────────────────────────────────────────────

def format_market_card(edge: dict, idx: int = 0) -> str:
    """Single market card for Telegram (HTML)."""
    edge_pct = edge["best_edge"] * 100
    prob_pct  = edge["our_prob"] * 100

    if edge["has_edge"]:
        edge_icon = "🚀 EDGE FOUND"
    elif edge_pct > 20:
        edge_icon = "🟡 Borderline"
    else:
        edge_icon = "🔴 No Edge"

    side_icon = "✅" if edge["best_side"] == "YES" else "❌"

    return (
        f"{'─' * 28}\n"
        f"<b>{idx+1}. {edge['question'][:80]}</b>\n"
        f"   Threshold: <code>{edge['thr_display']}</code>\n"
        f"   💰 YES: <b>${edge['yes_price']:.2f}</b>  |  NO: <b>${edge['no_price']:.2f}</b>\n"
        f"   🧠 Our prob (YES): <b>{prob_pct:.0f}%</b>\n"
        f"   {side_icon} Best side: <b>{edge['best_side']}</b> @ ${edge['best_price']:.2f}\n"
        f"   {edge_icon}: <b>{edge_pct:+.0f}¢/share</b>\n"
        f"   📊 Volume: ${float(edge.get('volume', 0)):,.0f}\n"
        f"   🔗 <a href=\"{edge['url']}\">Open on Polymarket</a>"
    )
