"""OpenRouter /api/v1/models scraper."""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from .base import ScrapedModel

OPENROUTER_API = "https://openrouter.ai/api/v1/models"

_MILLION = Decimal("1000000")


def _to_decimal(val: str | float | int | None) -> Decimal | None:
    """Parse a per-token price string into Decimal, or None if missing/zero."""
    if val is None:
        return None
    try:
        d = Decimal(str(val))
    except (InvalidOperation, ValueError):
        return None
    if d == 0:
        return None
    return d


def _per_mtok(per_token: Decimal | None) -> Decimal | None:
    if per_token is None:
        return None
    return per_token * _MILLION


def fetch_openrouter(
    tracked_providers: dict[str, str],
    skip_patterns: list[str],
    timeout: int = 30,
    save_raw: Path | None = None,
) -> list[ScrapedModel]:
    """Fetch all models from OpenRouter, filter to tracked providers.

    Args:
        tracked_providers: {openrouter_slug: tokdash_provider}, e.g. {"openai": "openai"}.
        skip_patterns: regex patterns to skip model IDs.
        timeout: HTTP timeout in seconds.
        save_raw: if set, write the raw API response to this path.

    Returns:
        List of ScrapedModel for tracked providers.
    """
    resp = httpx.get(OPENROUTER_API, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()

    if save_raw is not None:
        save_raw.parent.mkdir(parents=True, exist_ok=True)
        save_raw.write_text(json.dumps(payload, indent=2) + "\n")

    models_raw = payload.get("data", [])
    compiled_skips = [re.compile(p) for p in skip_patterns]

    results: list[ScrapedModel] = []
    for m in models_raw:
        model_id: str = m.get("id", "")
        if "/" not in model_id:
            continue

        provider_slug, _, short_id = model_id.partition("/")
        if provider_slug not in tracked_providers:
            continue

        if any(p.search(model_id) for p in compiled_skips):
            continue

        pricing = m.get("pricing") or {}

        prompt = _to_decimal(pricing.get("prompt"))
        completion = _to_decimal(pricing.get("completion"))
        if prompt is None and completion is None:
            continue  # no pricing data at all

        cache_read = _to_decimal(pricing.get("input_cache_read"))
        cache_write = _to_decimal(pricing.get("input_cache_write"))

        results.append(
            ScrapedModel(
                source_id=model_id,
                provider_slug=provider_slug,
                short_id=short_id,
                input_per_mtok=_per_mtok(prompt) or Decimal("0"),
                output_per_mtok=_per_mtok(completion) or Decimal("0"),
                cache_read_per_mtok=_per_mtok(cache_read),
                cache_write_per_mtok=_per_mtok(cache_write),
                source_name="openrouter",
                source_url=f"openrouter.ai/{model_id}",
                context_length=m.get("context_length"),
                raw_extra={
                    k: v
                    for k, v in pricing.items()
                    if k not in ("prompt", "completion", "input_cache_read", "input_cache_write")
                },
            )
        )

    return results
