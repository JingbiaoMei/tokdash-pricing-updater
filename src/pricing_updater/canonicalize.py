"""Canonicalize scraped model IDs into tokdash pricing_db keys.

The pricing_db.json uses short model IDs as keys (e.g. "gpt-5.4", not
"openai/gpt-5.4").  This module maps OpenRouter source IDs to those keys,
applying any overrides from config.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .sources.base import ScrapedModel


@dataclass
class CanonicalModel:
    """A model ready for the pricing_db after canonicalization + policy."""

    canonical_id: str  # the key in pricing_db.json["models"]
    provider: str  # the "provider" field value
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_read_per_mtok: Decimal
    cache_write_per_mtok: Decimal
    source_url: str
    source_name: str
    note: str = ""
    unit: str = "per_million_tokens"


def canonicalize(
    scraped: list[ScrapedModel],
    provider_map: dict[str, str],
    id_overrides: dict[str, str] | None = None,
) -> list[CanonicalModel]:
    """Map scraped models to canonical tokdash pricing_db entries.

    Args:
        scraped: raw scraped models from a source.
        provider_map: {source_slug: tokdash_provider}, e.g. {"openai": "openai"}.
        id_overrides: optional {source_short_id: canonical_id} for edge cases.

    Returns:
        List of CanonicalModel (cache fields may still be None-ish; policy fills those).
    """
    overrides = id_overrides or {}
    results: list[CanonicalModel] = []

    for m in scraped:
        canonical_id = overrides.get(m.short_id, m.short_id)
        provider = provider_map.get(m.provider_slug, m.provider_slug)

        results.append(
            CanonicalModel(
                canonical_id=canonical_id,
                provider=provider,
                input_per_mtok=m.input_per_mtok,
                output_per_mtok=m.output_per_mtok,
                cache_read_per_mtok=m.cache_read_per_mtok or Decimal("0"),
                cache_write_per_mtok=m.cache_write_per_mtok or Decimal("0"),
                source_url=m.source_url,
                source_name=m.source_name,
            )
        )

    return results
