"""Scrape official Z.ai/GLM pricing from docs.z.ai.

Z.ai publishes English USD pricing at a Mintlify-hosted docs site.
The page is server-rendered HTML with <table> elements.
"""

from __future__ import annotations

import httpx

from ._html_table import extract_table_rows, parse_price

ZAI_PRICING_URL = "https://docs.z.ai/guides/overview/pricing"

# page model name (lowercased) → canonical pricing_db key
_MODEL_MAP = {
    "glm-5": "glm-5",
    "glm-5-turbo": "glm-5-turbo",
    "glm-5-code": "glm-5-code",
    "glm-4.7": "glm-4.7",
    "glm-4.7-flash": "glm-4.7-flash",
    "glm-4.7-flashx": "glm-4.7-flashx",
    "glm-4.6": "glm-4.6",
    "glm-4.6v": "glm-4.6v",
    "glm-4.6v-flash": "glm-4.6v-flash",
    "glm-4.6v-flashx": "glm-4.6v-flashx",
    "glm-4.5": "glm-4.5",
    "glm-4.5v": "glm-4.5v",
    "glm-4.5-air": "glm-4.5-air",
    "glm-4.5-airx": "glm-4.5-airx",
    "glm-4.5-x": "glm-4.5-x",
    "glm-4.5-flash": "glm-4.5-flash",
    "glm-4-32b-0414-128k": "glm-4-32b",
    "glm-4-32b": "glm-4-32b",
}


def fetch_zai_official(timeout: int = 30) -> dict[str, dict]:
    """Fetch and parse Z.ai pricing. Returns {model_id: price_dict}."""
    try:
        resp = httpx.get(ZAI_PRICING_URL, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        print(f"  WARNING: Could not fetch Z.ai pricing: {e}")
        return {}

    return _parse_pricing(resp.text)


def _parse_pricing(html: str) -> dict[str, dict]:
    """Parse HTML tables. Column order: Model | Input | Cached Input | (Storage) | Output."""
    results: dict[str, dict] = {}

    for row in extract_table_rows(html):
        if len(row) < 3:
            continue

        model_name = row[0].lower().strip()

        canonical = None
        for page_name, canon in _MODEL_MAP.items():
            if page_name == model_name:
                canonical = canon
                break
        if canonical is None:
            continue

        input_price = parse_price(row[1]) if len(row) > 1 else None
        cache_read = parse_price(row[2]) if len(row) > 2 else None
        # Column 3 is usually "Cached Input Storage" (free), skip it.
        output_price = parse_price(row[-1])

        if input_price is None and output_price is None:
            # Free model
            if any("free" in c.lower() for c in row[1:]):
                results[canonical] = {
                    "provider": "z-ai", "currency": "USD",
                    "input": 0.0, "output": 0.0,
                    "source": ZAI_PRICING_URL,
                    "note": "Free tier model",
                }
            continue

        entry: dict = {
            "provider": "z-ai",
            "currency": "USD",
            "source": ZAI_PRICING_URL,
        }
        if input_price is not None:
            entry["input"] = input_price
        if output_price is not None:
            entry["output"] = output_price
        if cache_read is not None:
            entry["cache_read"] = cache_read

        results[canonical] = entry

    return results
