"""Scrape official Moonshot/Kimi pricing from platform.moonshot.ai.

The official pricing page is a Next.js SPA that does not serve pricing
data in the initial HTML.  This module attempts to fetch it anyway —
if Moonshot adds server-side rendering or an API endpoint, it will
start working automatically.

Until then, config/official_prices.yaml provides the fallback.
"""

from __future__ import annotations

import httpx

from ._html_table import extract_table_rows, parse_price

MOONSHOT_PRICING_URL = "https://platform.moonshot.ai/docs/pricing/chat"

_MODEL_MAP = {
    "kimi k2.5": "kimi-k2.5",
    "kimi-k2.5": "kimi-k2.5",
    "kimi k2 thinking": "kimi-k2-thinking",
    "kimi-k2-thinking": "kimi-k2-thinking",
    "kimi k2 0905": "kimi-k2-0905",
    "kimi-k2-0905": "kimi-k2-0905",
    "kimi k2 0711": "kimi-k2",
    "kimi k2": "kimi-k2",
    "kimi-k2": "kimi-k2",
}


def fetch_moonshot_official(timeout: int = 30) -> dict[str, dict]:
    """Attempt to fetch Moonshot pricing. Returns empty dict if SPA shell."""
    try:
        resp = httpx.get(MOONSHOT_PRICING_URL, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        print(f"  WARNING: Could not fetch Moonshot pricing: {e}")
        return {}

    html = resp.text

    # Detect SPA shell: real pricing page would have dollar signs in table rows.
    if "$" not in html:
        return {}

    return _parse_pricing(html)


def _parse_pricing(html: str) -> dict[str, dict]:
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
            "provider": "moonshotai",
            "currency": "USD",
            "input": prices[0],
            "source": MOONSHOT_PRICING_URL,
        }
        if len(prices) >= 2 and prices[1] is not None:
            entry["output"] = prices[1]

        results[canonical] = entry

    return results
