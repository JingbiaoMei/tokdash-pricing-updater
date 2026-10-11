"""Canonicalize scraped model IDs into tokdash pricing_db keys.

The pricing_db.json uses short model IDs as keys (e.g. "gpt-5.4", not
"openai/gpt-5.4").  This module maps OpenRouter source IDs to those keys,
applying any overrides from config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .sources.base import PRICE_FIELDS, ScrapedModel


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

    # Data-quality flags carried from the scraper. See ScrapedModel.
    missing_fields: set = field(default_factory=set)
    explicit_zero_fields: set = field(default_factory=set)
    invalid_fields: set = field(default_factory=set)

    # Provenance, kept out of pricing_db.json so the consumer schema is stable.
    # field_sources: field -> label of the source that supplied the final value.
    # evidence:      field -> {label: value} for every value seen for that field.
    field_sources: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)


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
        List of CanonicalModel (cache fields may still be zero-ish; policy fills those).
    """
    overrides = id_overrides or {}
    results: list[CanonicalModel] = []

    for m in scraped:
        canonical_id = overrides.get(m.short_id, m.short_id)
        provider = provider_map.get(m.provider_slug, m.provider_slug)

        values = {
            "input": m.input_per_mtok,
            "output": m.output_per_mtok,
            "cache_read": m.cache_read_per_mtok,
            "cache_write": m.cache_write_per_mtok,
        }
        label = m.source_name or "source"
        evidence = {}
        sources = {}
        for name in PRICE_FIELDS:
            value = values.get(name)
            if name in m.missing_fields or value is None:
                continue
            evidence[name] = {label: float(value)}
            sources[name] = label

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
                missing_fields=set(m.missing_fields),
                explicit_zero_fields=set(m.explicit_zero_fields),
                invalid_fields=set(m.invalid_fields),
                field_sources=sources,
                evidence=evidence,
            )
        )

    return results
