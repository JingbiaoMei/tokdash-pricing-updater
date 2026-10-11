"""Orchestrator for official-source fetching.

Attempts to scrape pricing from each provider's official page.
Results are merged into official_prices for the overlay step.
Failures are non-fatal — config/official_prices.yaml serves as fallback.
"""

from __future__ import annotations


def fetch_all_official(timeout: int = 30) -> dict[str, dict]:
    """Fetch from all official sources, return {model_id: price_dict}.

    Each returned dict has: provider, currency, input, output, and
    optionally cache_read, cache_write, source.

    This is additive — it only returns models it successfully scraped.
    Models not returned here fall back to config/official_prices.yaml.
    """
    results: dict[str, dict] = {}

    adapters = [
        ("DeepSeek", _fetch_deepseek),
        ("Z.ai/GLM", _fetch_zai),
        ("MiniMax", _fetch_minimax),
        ("Moonshot/Kimi", _fetch_moonshot),
        ("Xiaomi/MiMo", _fetch_xiaomi),
        ("StepFun", _fetch_stepfun),
    ]

    for name, fetcher in adapters:
        try:
            fetched = fetcher(timeout)
        except Exception as e:
            print(f"  WARNING: {name} official fetch failed: {e}")
            fetched = {}
        if fetched:
            print(f"  {name}: {len(fetched)} models")
            results.update(fetched)

    return results


def _fetch_deepseek(timeout: int) -> dict[str, dict]:
    from .official_deepseek import fetch_deepseek_official

    scraped = fetch_deepseek_official(timeout)
    return {
        m.short_id: {
            "provider": "deepseek",
            "currency": "USD",
            "input": float(m.input_per_mtok),
            "output": float(m.output_per_mtok),
            "cache_read": float(m.cache_read_per_mtok) if m.cache_read_per_mtok else None,
            "cache_write": float(m.cache_write_per_mtok) if m.cache_write_per_mtok else None,
            "source": m.source_url,
        }
        for m in scraped
    }


def _fetch_zai(timeout: int) -> dict[str, dict]:
    from .official_zai import fetch_zai_official
    return fetch_zai_official(timeout)


def _fetch_minimax(timeout: int) -> dict[str, dict]:
    from .official_minimax import fetch_minimax_official
    return fetch_minimax_official(timeout)


def _fetch_moonshot(timeout: int) -> dict[str, dict]:
    from .official_moonshot import fetch_moonshot_official
    return fetch_moonshot_official(timeout)


def _fetch_xiaomi(timeout: int) -> dict[str, dict]:
    from .official_xiaomi import fetch_xiaomi_official
    return fetch_xiaomi_official(timeout)


def _fetch_stepfun(timeout: int) -> dict[str, dict]:
    from .official_stepfun import fetch_stepfun_official
    return fetch_stepfun_official(timeout)
