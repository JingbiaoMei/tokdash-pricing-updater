"""Scrape official Xiaomi/MiMo pricing from platform.xiaomimimo.com.

Xiaomi's platform may or may not serve pricing in the initial HTML.
This module attempts to fetch; falls back to config/official_prices.yaml.
"""

from __future__ import annotations

import httpx

from ._html_table import extract_table_rows, parse_price

XIAOMI_PRICING_URL = "https://platform.xiaomimimo.com"

_MODEL_MAP = {
    "mimo-v2-flash": "mimo-v2-flash",
    "mimo v2 flash": "mimo-v2-flash",
}


def fetch_xiaomi_official(timeout: int = 30) -> dict[str, dict]:
    """Attempt to fetch Xiaomi MiMo pricing. Returns empty dict if no data."""
    for url in [XIAOMI_PRICING_URL, f"{XIAOMI_PRICING_URL}/pricing"]:
        try:
            resp = httpx.get(url, timeout=timeout, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError:
            continue

        html = resp.text
        if "$" not in html:
            continue

        results = _parse_pricing(html, url)
        if results:
            return results

    return {}


def _parse_pricing(html: str, source_url: str) -> dict[str, dict]:
    results: dict[str, dict] = {}

    for row in extract_table_rows(html):
        if len(row) < 3:
            continue

        model_cell = row[0].lower().strip()
        canonical = None
        for page_name, canon in _MODEL_MAP.items():
            if page_name in model_cell:
                canonical = canon
                break
        if canonical is None:
            continue

        prices = [parse_price(c) for c in row[1:]]
        if not prices or prices[0] is None:
            continue

        entry: dict = {
            "provider": "xiaomi",
            "currency": "USD",
            "input": prices[0],
            "source": source_url,
        }
        if len(prices) >= 2 and prices[1] is not None:
            entry["output"] = prices[1]

        results[canonical] = entry

    return results
