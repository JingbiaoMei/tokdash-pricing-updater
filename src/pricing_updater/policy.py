"""Pricing policy: fill missing cache fields, round, annotate.

This layer sits between canonicalization and rendering.  It applies
heuristic cache rates when the source didn't provide them and enforces
rounding/precision rules.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from .canonicalize import CanonicalModel


def apply_policy(
    models: list[CanonicalModel],
    cache_fallback: dict[str, dict[str, float]],
    max_decimal_places: int = 6,
) -> list[CanonicalModel]:
    """Apply pricing policy to a list of canonical models.

    Args:
        models: canonical models (cache fields may be 0 if source omitted them).
        cache_fallback: {provider: {"cache_read_factor": f, "cache_write_factor": f}}.
        max_decimal_places: maximum decimal places for rounding.

    Returns:
        Same list with cache fields filled and prices rounded.
    """
    default_fallback = cache_fallback.get("default", {})

    for m in models:
        fb = cache_fallback.get(m.provider, default_fallback)
        read_factor = Decimal(str(fb.get("cache_read_factor", 0.10)))
        write_factor = Decimal(str(fb.get("cache_write_factor", 1.00)))

        # Fill cache pricing when source didn't provide it.
        if m.cache_read_per_mtok == 0 and m.input_per_mtok > 0:
            m.cache_read_per_mtok = m.input_per_mtok * read_factor

        if m.cache_write_per_mtok == 0 and m.input_per_mtok > 0:
            m.cache_write_per_mtok = m.input_per_mtok * write_factor

        # Round all price fields.
        quant = Decimal(10) ** -max_decimal_places
        m.input_per_mtok = m.input_per_mtok.quantize(quant, rounding=ROUND_HALF_UP)
        m.output_per_mtok = m.output_per_mtok.quantize(quant, rounding=ROUND_HALF_UP)
        m.cache_read_per_mtok = m.cache_read_per_mtok.quantize(quant, rounding=ROUND_HALF_UP)
        m.cache_write_per_mtok = m.cache_write_per_mtok.quantize(quant, rounding=ROUND_HALF_UP)

    return models
