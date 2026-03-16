"""Scrape official MiniMax pricing from platform.minimax.io.

MiniMax publishes English USD pricing at their docs platform.
The pay-as-you-go page serves HTML with <table> elements.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

import httpx

from ._html_table import extract_table_rows, parse_price

MINIMAX_PRICING_URL = "https://platform.minimax.io/docs/guides/pricing-paygo"

# Exact match only — "minimax-m2.5" must NOT match "minimax-m2.5-highspeed".
_MODEL_MAP = {
    "minimax-m2.5": "minimax-m2.5",
    "minimax-m2.1": "minimax-m2.1",
    "minimax-m2": "minimax-m2",
    "m2-her": "minimax-m2-her",
    "minimax-m2-her": "minimax-m2-her",
    "minimax-01": "minimax-01",
    "minimax-m1": "minimax-m1",
}


def fetch_minimax_official(timeout: int = 30) -> dict[str, dict]:
    """Fetch and parse MiniMax pricing. Returns {model_id: price_dict}."""
    try:
        resp = httpx.get(MINIMAX_PRICING_URL, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        print(f"  WARNING: Could not fetch MiniMax pricing: {e}")
        return {}

    return _parse_pricing(resp.text)


def _parse_pricing(html: str) -> dict[str, dict]:
    """Parse HTML tables from the MiniMax page."""
    results: dict[str, dict] = {}

    for row in extract_table_rows(html):
        if len(row) < 3:
            continue

        model_cell = row[0].lower().strip()

        canonical = None
        for page_name, canon in _MODEL_MAP.items():
            if model_cell == page_name:
                canonical = canon
                break
        if canonical is None:
            continue

        prices = [parse_price(c) for c in row[1:]]

        if not prices or prices[0] is None:
            continue

        entry: dict = {
            "provider": "minimax",
            "currency": "USD",
            "input": prices[0],
            "source": MINIMAX_PRICING_URL,
        }

        if len(prices) >= 2 and prices[1] is not None:
            entry["output"] = prices[1]

        results[canonical] = entry

    # Look for cache pricing in the page text.
    cache_read = _find_cache_price(html, "read")
    cache_write = _find_cache_price(html, "write")
    if cache_read is not None or cache_write is not None:
        for entry in results.values():
            if cache_read is not None:
                entry.setdefault("cache_read", cache_read)
            if cache_write is not None:
                entry.setdefault("cache_write", cache_write)

    return results


def _find_cache_price(html: str, kind: str) -> float | None:
    """Look for cache pricing in prose: 'Prompt Caching Read: $0.03/M tokens'."""
    pattern = rf"(?:cache|caching)\s+{kind}[^$\n]*\$([\d.]+)"
    m = re.search(pattern, html, re.I)
    if m:
        try:
            return float(Decimal(m.group(1)))
        except InvalidOperation:
            pass
    return None
