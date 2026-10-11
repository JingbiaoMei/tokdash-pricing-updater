"""Pricing policy: FX-normalized official overlay, cache fallback, rounding.

Pipeline order:
1. Resolve family mapping (versioned IDs -> family official price).
2. Convert official prices to USD via FX rates.
3. Settle each price field: official wins where it is the only source or the
   bigger number, otherwise the scraped value stands.
4. Fill heuristic cache rates only where no source published anything.
5. Round all prices to configured precision.

Open decision (issue #202, D1): step 3 compares OpenRouter against the official
page field by field, so a model can end up with one field from each source, and
a higher aggregator price beats the provider's own page. Whether the official
price should simply win whenever both exist is a policy call that has not been
made yet; the existing tests pin today's max() behaviour, so treat them as the
status quo rather than as a ratified rule.
"""

from __future__ import annotations

import fnmatch
from decimal import ROUND_HALF_UP, Decimal

from .canonicalize import CanonicalModel
from .sources.base import clean_price

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

def _to_usd(value: Decimal | float | int, currency: str, fx_rates: dict[str, float]) -> Decimal:
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
    """Overlay official prices onto scraped ones, field by field.

    Steps per model:
    1. Resolve official entry via exact key or family map.
    2. Convert official prices from their native currency to USD.
    3. Per field, take the official value when it is larger or when the scrape
       never settled the field; otherwise keep the scraped value.

    A field the scrape published as 0 is a published fact, so an official 0
    settles it and leaves it flagged, which stops apply_cache_fallback from
    substituting a guessed factor.
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
            raw_usd = clean_price(official_val)
            if raw_usd is None:
                print(
                    f"  WARNING: {m.canonical_id!r} official {cfg_key} price "
                    f"{official_val!r} from {official.get('source', 'official')} is not a "
                    f"finite non-negative number; keeping the scraped price."
                )
                continue
            official_usd = _to_usd(raw_usd, currency, rates)
            m.evidence.setdefault(cfg_key, {})["official_usd"] = float(official_usd)
            current_dec = getattr(m, attr)
            # A published official value settles a field the scraper could not
            # fill, even when it is not the larger number. A missing cache field
            # is internally 0, so a strict > test dropped an official 0 and the
            # cache heuristic guessed over a real "cache reads are free".
            unresolved = cfg_key in m.missing_fields or cfg_key in m.explicit_zero_fields
            if official_usd > current_dec or unresolved:
                setattr(m, attr, official_usd)
                m.field_sources[cfg_key] = source_label
                # The official value answers the question, so the field stops
                # being missing. A zero stays flagged as an explicit zero, which
                # is what keeps apply_cache_fallback from guessing over it.
                m.missing_fields.discard(cfg_key)
                if official_usd == 0:
                    m.explicit_zero_fields.add(cfg_key)
                else:
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
