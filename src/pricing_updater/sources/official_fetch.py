"""Orchestrator for official-source fetching.

Attempts to scrape pricing from each provider's official page.
Results are merged into official_prices for the overlay step.
Failures are non-fatal — the YAML config serves as fallback.
"""

from __future__ import annotations

from .base import ScrapedModel


def fetch_all_official(timeout: int = 30) -> dict[str, dict]:
    """Fetch from all official sources, return {model_id: price_dict}.

    Each returned dict has: input, output, cache_read, cache_write,
    currency, source.  Currency is always USD (converted at source).

    This is additive — it only returns models it successfully scraped.
    Models not returned here fall back to config/official_prices.yaml.
    """
    results: dict[str, dict] = {}

    # DeepSeek (English, stable)
    results.update(_fetch_deepseek(timeout))

    # Chinese providers — stubs for future implementation.
    # These require either:
    #   - Browser automation for SPA rendering, or
    #   - Stable API endpoints (not yet identified), or
    #   - Manual config/official_prices.yaml entries (current approach)
    #
    # When implemented, each should:
    #   1. Fetch the pricing page
    #   2. Parse model names and prices in native currency
    #   3. Convert to USD using fx_rates
    #   4. Return the same dict format

    return results


def _fetch_deepseek(timeout: int) -> dict[str, dict]:
    from .official_deepseek import fetch_deepseek_official

    scraped = fetch_deepseek_official(timeout)
    out = {}
    for m in scraped:
        out[m.short_id] = {
            "provider": "deepseek",
            "currency": "USD",
            "input": float(m.input_per_mtok),
            "output": float(m.output_per_mtok),
            "cache_read": float(m.cache_read_per_mtok) if m.cache_read_per_mtok else None,
            "cache_write": float(m.cache_write_per_mtok) if m.cache_write_per_mtok else None,
            "source": m.source_url,
        }
    return out
