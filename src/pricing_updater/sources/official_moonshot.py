"""Read Kimi's official pricing document, including literal MDX tables."""
from __future__ import annotations

import re

import httpx

from ._pricing_tables import html_tables, mdx_tables, plain, price_columns, usd_price

MOONSHOT_PRICING_URL = "https://platform.kimi.ai/docs/pricing/chat"
MOONSHOT_PRICING_DOCUMENT_URL = MOONSHOT_PRICING_URL + ".md"


def fetch_moonshot_official(timeout: int = 30) -> dict[str, dict]:
    for url in (MOONSHOT_PRICING_DOCUMENT_URL, MOONSHOT_PRICING_URL):
        try:
            response = httpx.get(url, timeout=timeout, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"  WARNING: Could not fetch Kimi pricing at {url}: {exc}")
            continue
        result = _parse_pricing(response.text)
        if result:
            return result
    return {}


def _parse_pricing(document: str) -> dict[str, dict]:
    results = {}
    for table in [*mdx_tables(document), *html_tables(document)]:
        if not table:
            continue
        headers = [plain(cell).lower() for cell in table[0]]
        model_column = next((i for i, label in enumerate(headers)
                             if label in ("model", "model name")), None)
        fields = price_columns(headers)
        unit_column = next((i for i, label in enumerate(headers) if label == "unit"), None)
        if model_column is None or not {"input", "output"} <= fields.keys():
            continue
        if unit_column is None:
            continue
        for row in table[1:]:
            if len(row) != len(headers):
                continue
            if not re.fullmatch(r"(?:1\s*M|1,000,000)\s*tokens", plain(row[unit_column]), re.I):
                continue
            name = plain(row[model_column]).lower()
            if not re.fullmatch(r"kimi-[a-z0-9][a-z0-9.-]*", name):
                continue
            prices = {field: usd_price(row[index]) for field, index in fields.items()}
            if prices["input"] is None or prices["output"] is None:
                continue
            results[name] = {
                "provider": "moonshotai", "currency": "USD",
                "source": MOONSHOT_PRICING_URL,
                **{field: float(value) for field, value in prices.items() if value is not None},
            }
    return results
