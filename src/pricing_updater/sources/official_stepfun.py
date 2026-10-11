"""Scrape official StepFun pricing from platform.stepfun.ai.

StepFun publishes English USD pricing on a docs page served as
server-rendered HTML with <table> elements. Only rows billed per
"1M tokens" are kept; per-character TTS, per-hour ASR, and per-image
rows use different units and are skipped via the model map.
"""

from __future__ import annotations

import httpx

from ._html_table import extract_table_rows, parse_price

STEPFUN_PRICING_URL = "https://platform.stepfun.ai/docs/en/guides/pricing/details"

# page model name (lowercased) → canonical pricing_db key
_MODEL_MAP = {
    "step-5-preview": "step-5-preview",
    "step-3.7-flash": "step-3.7-flash",
    "step-3.5-flash": "step-3.5-flash",
    "step-3.5-flash-2603": "step-3.5-flash-2603",
    "step-1o-turbo-vision": "step-1o-turbo-vision",
    "step-1o-audio": "step-1o-audio",
    "step-audio-2": "step-audio-2",
    "step-audio-r1.5": "step-audio-r1.5",
    "stepaudio-2.5-chat": "stepaudio-2.5-chat",
    "stepaudio-2.5-realtime": "stepaudio-2.5-realtime",
}


def fetch_stepfun_official(timeout: int = 30) -> dict[str, dict]:
    """Fetch and parse StepFun pricing. Returns {model_id: price_dict} in USD."""
    try:
        resp = httpx.get(STEPFUN_PRICING_URL, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        print(f"  WARNING: Could not fetch StepFun pricing: {e}")
        return {}

    return _parse_pricing(resp.text)


def _parse_pricing(html: str) -> dict[str, dict]:
    """Parse HTML tables. Columns: Model | Unit | Input(cache miss) | Input(cache hit) | Output | [Status]."""
    results: dict[str, dict] = {}

    for row in extract_table_rows(html):
        if len(row) < 5:
            continue

        model_name = row[0].lower().strip()
        canonical = _MODEL_MAP.get(model_name)
        if canonical is None:
            continue

        # Keep only token-billed rows; audio/TTS/ASR/image rows use other units.
        if "1m tokens" not in row[1].lower():
            continue

        input_price = parse_price(row[2])
        cache_read = parse_price(row[3])
        output_price = parse_price(row[4])

        if input_price is None and output_price is None:
            continue

        entry: dict = {
            "provider": "stepfun",
            "currency": "USD",
            "source": STEPFUN_PRICING_URL,
        }
        if input_price is not None:
            entry["input"] = input_price
        if output_price is not None:
            entry["output"] = output_price
        if cache_read is not None:
            entry["cache_read"] = cache_read

        results[canonical] = entry

    return results
