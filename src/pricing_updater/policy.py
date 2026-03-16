"""Pricing policy: official price overlay, cache fallback, rounding.

This layer sits between canonicalization and rendering.  It:
1. Overlays official prices from config/official_prices.yaml using max(openrouter, official).
2. Fills heuristic cache rates when no source provided them.
3. Rounds all prices to the configured precision.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from .canonicalize import CanonicalModel


def apply_official_overlay(
    models: list[CanonicalModel],
    official_prices: dict[str, dict],
) -> list[CanonicalModel]:
    """Apply official prices using max(openrouter, official) per field.

    For each model that has an entry in official_prices, compare each price
    field and keep the higher value.  This is conservative: we never
    underestimate cost, whether the user is routed through OpenRouter or
    hitting the provider API directly.

    Args:
        models: canonical models from OpenRouter.
        official_prices: {model_id: {"input": float, "output": float, ...}}.

    Returns:
        Same list with prices adjusted upward where official is higher.
    """
    if not official_prices:
        return models

    for m in models:
        official = official_prices.get(m.canonical_id)
        if official is None:
            continue

        fields = [
            ("input", "input_per_mtok"),
            ("output", "output_per_mtok"),
            ("cache_read", "cache_read_per_mtok"),
            ("cache_write", "cache_write_per_mtok"),
        ]
        for cfg_key, attr in fields:
            official_val = official.get(cfg_key)
            if official_val is None:
                continue
            official_dec = Decimal(str(official_val))
            current_dec = getattr(m, attr)
            if official_dec > current_dec:
                setattr(m, attr, official_dec)
                if not m.note:
                    m.note = f"Official price used ({official.get('source', 'official')})"

    return models


def apply_cache_fallback(
    models: list[CanonicalModel],
    cache_fallback: dict[str, dict[str, float]],
) -> list[CanonicalModel]:
    """Fill missing cache fields with per-provider heuristics.

    Only applies when the source (and official overlay) didn't provide
    cache pricing (field is 0).
    """
    default_fallback = cache_fallback.get("default", {})

    for m in models:
        fb = cache_fallback.get(m.provider, default_fallback)
        read_factor = Decimal(str(fb.get("cache_read_factor", 0.10)))
        write_factor = Decimal(str(fb.get("cache_write_factor", 1.00)))

        if m.cache_read_per_mtok == 0 and m.input_per_mtok > 0:
            m.cache_read_per_mtok = m.input_per_mtok * read_factor

        if m.cache_write_per_mtok == 0 and m.input_per_mtok > 0:
            m.cache_write_per_mtok = m.input_per_mtok * write_factor

    return models


def apply_rounding(
    models: list[CanonicalModel],
    max_decimal_places: int = 6,
) -> list[CanonicalModel]:
    """Round all price fields to the configured precision."""
    quant = Decimal(10) ** -max_decimal_places
    for m in models:
        m.input_per_mtok = m.input_per_mtok.quantize(quant, rounding=ROUND_HALF_UP)
        m.output_per_mtok = m.output_per_mtok.quantize(quant, rounding=ROUND_HALF_UP)
        m.cache_read_per_mtok = m.cache_read_per_mtok.quantize(quant, rounding=ROUND_HALF_UP)
        m.cache_write_per_mtok = m.cache_write_per_mtok.quantize(quant, rounding=ROUND_HALF_UP)
    return models


def apply_policy(
    models: list[CanonicalModel],
    cache_fallback: dict[str, dict[str, float]],
    max_decimal_places: int = 6,
    official_prices: dict[str, dict] | None = None,
) -> list[CanonicalModel]:
    """Full policy pipeline: official overlay → cache fallback → rounding."""
    models = apply_official_overlay(models, official_prices or {})
    models = apply_cache_fallback(models, cache_fallback)
    models = apply_rounding(models, max_decimal_places)
    return models
