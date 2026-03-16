"""Scrape official DeepSeek pricing from api-docs.deepseek.com.

DeepSeek publishes pricing in USD at a stable English-language URL.
Returns ScrapedModel entries that get merged into official_prices at runtime.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

import httpx

from .base import ScrapedModel

DEEPSEEK_PRICING_URL = "https://api-docs.deepseek.com/quick_start/pricing"


def _parse_price(text: str) -> Decimal | None:
    """Extract a dollar amount like '$0.28' from text."""
    m = re.search(r"\$?([\d.]+)", text)
    if m:
        try:
            return Decimal(m.group(1))
        except InvalidOperation:
            pass
    return None


def fetch_deepseek_official(timeout: int = 30) -> list[ScrapedModel]:
    """Fetch and parse the DeepSeek pricing page.

    Returns ScrapedModel entries with source_name='deepseek_official'.
    Prices are in USD per MTok.
    """
    try:
        resp = httpx.get(DEEPSEEK_PRICING_URL, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        print(f"  WARNING: Could not fetch DeepSeek pricing: {e}")
        return []

    html = resp.text
    models: list[ScrapedModel] = []

    # The DeepSeek pricing page lists models in a table.
    # Look for patterns like "deepseek-chat" with prices "$0.28 per 1M tokens"
    # or structured as rows with model name and price columns.
    #
    # Known models from the page (as of 2026-03):
    #   deepseek-chat:     input $0.28/MTok, output $0.42/MTok, cache hit $0.028/MTok
    #   deepseek-reasoner: input $0.28/MTok, output $0.42/MTok, cache hit $0.028/MTok

    # Try to extract from markdown/HTML table rows.
    # Fall back to known values if parsing fails.
    parsed = _try_parse_table(html)
    if parsed:
        return parsed

    # Fallback: return nothing and let config/official_prices.yaml handle it.
    print("  WARNING: Could not parse DeepSeek pricing table, using config fallback")
    return []


def _try_parse_table(html: str) -> list[ScrapedModel]:
    """Best-effort parse of the pricing table from HTML/markdown content."""
    results = []

    # Look for model names we know about
    model_patterns = {
        "deepseek-chat": "deepseek-chat",
        "deepseek-reasoner": "deepseek-r1",
    }

    for page_name, canonical_name in model_patterns.items():
        if page_name not in html.lower():
            continue

        # Extract prices near the model name.
        # Look for dollar amounts in nearby context.
        idx = html.lower().find(page_name)
        if idx < 0:
            continue
        context = html[max(0, idx - 200):idx + 500]

        prices = re.findall(r"\$?([\d.]+)\s*(?:per\s+1M|/\s*1M|per\s+million)", context, re.I)
        if not prices:
            # Try simpler pattern: just dollar amounts
            prices = re.findall(r"\$([\d.]+)", context)

        if len(prices) >= 2:
            try:
                input_price = Decimal(prices[0])
                output_price = Decimal(prices[-1])
                # Cache hit is typically listed between input and output or as 10% of input
                cache_read = Decimal(prices[1]) if len(prices) >= 3 else input_price * Decimal("0.1")

                results.append(ScrapedModel(
                    source_id=f"deepseek/{canonical_name}",
                    provider_slug="deepseek",
                    short_id=canonical_name,
                    input_per_mtok=input_price,
                    output_per_mtok=output_price,
                    cache_read_per_mtok=cache_read,
                    cache_write_per_mtok=input_price,  # cache miss = input price
                    source_name="deepseek_official",
                    source_url=DEEPSEEK_PRICING_URL,
                ))
            except InvalidOperation:
                continue

    return results
