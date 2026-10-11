"""Pricing policy: FX-normalized official overlay, cache fallback, rounding.

Pipeline order:
1. Resolve family mapping (versioned IDs → family official price).
2. Convert official prices to USD via FX rates.
3. Apply max(openrouter, official) per field.
4. Fill heuristic cache rates when no source provided them.
5. Round all prices to configured precision.
"""

from __future__ import annotations

import fnmatch
from decimal import ROUND_HALF_UP, Decimal

from .canonicalize import CanonicalModel

_PRICE_FIELDS = [
    ("input", "input_per_mtok"),
    ("output", "output_per_mtok"),
    ("cache_read", "cache_read_per_mtok"),
    ("cache_write", "cache_write_per_mtok"),
]


# ---------------------------------------------------------------------------
# Family mapping
# ---------------------------------------------------------------------------

def resolve_family(
    canonical_id: str,
    official_prices: dict[str, dict],
    family_map: list[dict],
) -> dict | None:
    """Look up official pricing for a model, checking exact key then family map.

    Returns the official entry dict, or None if no match.
    """
    # Exact match first.
    entry = official_prices.get(canonical_id)
    if entry is not None:
        return entry

    # Family map: try each pattern in order.
    for rule in family_map:
        pattern = rule.get("pattern", "")
        family_key = rule.get("family", "")
        if fnmatch.fnmatch(canonical_id, pattern):
            entry = official_prices.get(family_key)
            if entry is not None:
                return entry

    return None


# ---------------------------------------------------------------------------
# FX normalization
# ---------------------------------------------------------------------------

def _to_usd(value: float | int, currency: str, fx_rates: dict[str, float]) -> Decimal:
    """Convert a price from the given currency to USD."""
    rate = fx_rates.get(currency)
    if rate is None:
        raise ValueError(
            f"Unknown currency {currency!r} — add it to config/fx_rates.yaml"
        )
    return Decimal(str(value)) * Decimal(str(rate))


# ---------------------------------------------------------------------------
# Official overlay with FX + family
# ---------------------------------------------------------------------------

def apply_official_overlay(
    models: list[CanonicalModel],
    official_prices: dict[str, dict],
    family_map: list[dict] | None = None,
    fx_rates: dict[str, float] | None = None,
) -> list[CanonicalModel]:
    """Apply official prices using max(openrouter, official_usd) per field.

    Steps per model:
    1. Resolve official entry via exact key or family map.
    2. Convert official prices from their native currency to USD.
    3. For each price field, keep max(openrouter, official_usd).
    """
    if not official_prices:
        return models

    fam = family_map or []
    rates = fx_rates or {"USD": 1.0}

    for m in models:
        official = resolve_family(m.canonical_id, official_prices, fam)
        if official is None:
            continue

        currency = official.get("currency", "USD")
        if currency not in rates:
            # Don't abort the whole run for one unconvertible currency: keep the
            # OpenRouter price and warn so it can be fixed in config/fx_rates.yaml.
            print(
                f"  WARNING: {m.canonical_id!r} official price is in unknown "
                f"currency {currency!r} (add it to config/fx_rates.yaml); "
                f"keeping OpenRouter price."
            )
            continue

        source_label = f"official:{official.get('source', 'official')}"
        for cfg_key, attr in _PRICE_FIELDS:
            official_val = official.get(cfg_key)
            if official_val is None:
                continue
            official_usd = _to_usd(official_val, currency, rates)
            m.evidence.setdefault(cfg_key, {})["official_usd"] = float(official_usd)
            current_dec = getattr(m, attr)
            if official_usd > current_dec:
                setattr(m, attr, official_usd)
                m.field_sources[cfg_key] = source_label
                # A published official value settles both a field the scraper
                # never saw and one it saw as free, so neither stays flagged.
                m.missing_fields.discard(cfg_key)
                m.explicit_zero_fields.discard(cfg_key)
                if not m.note:
                    m.note = f"Official price used ({official.get('source', 'official')})"
            else:
                m.field_sources.setdefault(cfg_key, m.source_name or "openrouter")

    return models


# ---------------------------------------------------------------------------
# Cache fallback
# ---------------------------------------------------------------------------

def apply_cache_fallback(
    models: list[CanonicalModel],
    cache_fallback: dict[str, dict[str, float]],
) -> list[CanonicalModel]:
    """Fill missing cache fields with per-provider heuristics."""
    default_fallback = cache_fallback.get("default", {})

    for m in models:
        fb = cache_fallback.get(m.provider, default_fallback)
        read_factor = Decimal(str(fb.get("cache_read_factor", 0.10)))
        write_factor = Decimal(str(fb.get("cache_write_factor", 1.00)))

        # A published cache price of 0 is a fact about the provider, not a gap
        # to fill in, so explicit zeros are left alone.
        if "cache_read" not in m.explicit_zero_fields and m.cache_read_per_mtok == 0 and m.input_per_mtok > 0:
            m.cache_read_per_mtok = m.input_per_mtok * read_factor
            m.field_sources["cache_read"] = "derived_from_input"
            m.missing_fields.discard("cache_read")

        if "cache_write" not in m.explicit_zero_fields and m.cache_write_per_mtok == 0 and m.input_per_mtok > 0:
            m.cache_write_per_mtok = m.input_per_mtok * write_factor
            m.field_sources["cache_write"] = "derived_from_input"
            m.missing_fields.discard("cache_write")

    return models


# ---------------------------------------------------------------------------
# Rounding
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

def apply_policy(
    models: list[CanonicalModel],
    cache_fallback: dict[str, dict[str, float]],
    max_decimal_places: int = 6,
    official_prices: dict[str, dict] | None = None,
    family_map: list[dict] | None = None,
    fx_rates: dict[str, float] | None = None,
) -> list[CanonicalModel]:
    """Full policy pipeline: official overlay → cache fallback → rounding."""
    models = apply_official_overlay(models, official_prices or {}, family_map, fx_rates)
    models = apply_cache_fallback(models, cache_fallback)
    models = apply_rounding(models, max_decimal_places)
    return models
