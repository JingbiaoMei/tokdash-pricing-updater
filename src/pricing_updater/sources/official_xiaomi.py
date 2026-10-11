"""Read Xiaomi's overseas real-time API tariff from its public pricing docs."""
from __future__ import annotations

import re

import httpx

from ._pricing_tables import html_tables, plain, price_columns, usd_price

XIAOMI_PRICING_URL = "https://mimo.mi.com/static/docs/price/pay-as-you-go.md"


def fetch_xiaomi_official(timeout: int = 30) -> dict[str, dict]:
    try:
        response = httpx.get(XIAOMI_PRICING_URL, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"  WARNING: Could not fetch Xiaomi pricing: {exc}")
        return {}
    return _parse_pricing(response.text, XIAOMI_PRICING_URL)


def _parse_pricing(document: str, source_url: str = XIAOMI_PRICING_URL) -> dict[str, dict]:
    results = {}
    if not re.search(r"Overseas\s*[:：]\s*(?:dollar|USD|\$)\s*/\s*(?:1\s*)?M\s*tokens", document, re.I):
        return results
    # This is a published cache-write tariff, not a guess from the input price.
    free_write = bool(re.search(r"Cache Write\s*:\s*(?:Limited-time )?Free\b", document, re.I))
    for table in html_tables(document):
        if not table:
            continue
        headers = [plain(cell).lower() for cell in table[0]]
        model_column = next((i for i, label in enumerate(headers)
                             if label in ("model", "model name")), None)
        inference_column = next((i for i, label in enumerate(headers)
                                 if label == "inference type"), None)
        fields = price_columns(headers)
        if model_column is None or inference_column is None or not {"input", "output"} <= fields.keys():
            continue
        for row in table[1:]:
            if len(row) != len(headers) or plain(row[inference_column]).lower() != "real-time api":
                continue
            match = re.search(r"\bmimo-[a-z0-9][a-z0-9.-]*\b", row[model_column], re.I)
            if not match:
                continue
            prices = {field: usd_price(row[index]) for field, index in fields.items()}
            # An explicit dollar sign distinguishes overseas rates from RMB.
            if prices["input"] is None or prices["output"] is None:
                continue
            if free_write and "cache_write" not in fields:
                prices["cache_write"] = 0
            results[match.group().lower()] = {
                "provider": "xiaomi", "currency": "USD", "source": source_url,
                **{field: float(value) for field, value in prices.items() if value is not None},
            }
    return results
