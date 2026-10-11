"""Orchestrator for official-source fetching.

Attempts to scrape pricing from each provider's official page.
Results are merged into official_prices for the overlay step.
Failures are non-fatal — config/official_prices.yaml serves as fallback — but
they are recorded, because a scraper that quietly died looks exactly like a day
with no official price changes.
"""

from __future__ import annotations


def fetch_all_official(timeout: int = 30, health: list | None = None) -> dict[str, dict]:
    """Fetch from all official sources, return {model_id: price_dict}.

    Each returned dict has: provider, currency, input, output, and
    optionally cache_read, cache_write, source.

    This is additive — it only returns models it successfully scraped.
    Models not returned here fall back to config/official_prices.yaml.

    ``health``, when given, gets one record per adapter: ``{"source", "status",
    "models", "detail"}`` with status ok | failed | empty. A silent fallback to
    hand-maintained YAML is the difference between a stale price nobody notices
    and a reviewer knowing which numbers came from where.
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
            _record(health, name, "failed", 0, str(e) or type(e).__name__)
            continue
        if not fetched:
            # Reachable page, nothing parsed: the markup moved, most likely.
            _record(health, name, "empty", 0, "no models parsed; fell back to YAML")
            continue
        print(f"  {name}: {len(fetched)} models")
        results.update(fetched)
        _record(health, name, "ok", len(fetched), "")

    return results


def _record(health: list | None, source: str, status: str, models: int, detail: str) -> None:
    if health is not None:
        health.append({
            "source": source, "status": status, "models": models, "detail": detail,
        })


def _fetch_deepseek(timeout: int) -> dict[str, dict]:
    from .official_deepseek import fetch_deepseek_official

    scraped = fetch_deepseek_official(timeout)
    return {
        m.short_id: {
            "provider": "deepseek",
            "currency": "USD",
            "input": float(m.input_per_mtok),
            "output": float(m.output_per_mtok),
            # `is not None`, not truthiness. Free cache reads are published as a
            # literal 0, and a truthiness test dropped that 0 on its way into the
            # overlay, where the cache heuristic then invented a positive price to
            # replace a real "cache reads cost nothing".
            "cache_read": (
                float(m.cache_read_per_mtok) if m.cache_read_per_mtok is not None else None
            ),
            "cache_write": (
                float(m.cache_write_per_mtok) if m.cache_write_per_mtok is not None else None
            ),
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
