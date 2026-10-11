"""Base data model for scraped pricing data."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

# The four price fields a pricing_db entry carries.
PRICE_FIELDS = ("input", "output", "cache_read", "cache_write")

# Fields that must exist for an entry to be priceable at all.
CORE_PRICE_FIELDS = ("input", "output")


@dataclass
class ScrapedModel:
    """A single model's pricing as returned by a source.

    All prices are per 1M tokens, stored as Decimal for precision.

    A missing price and a published zero are different facts and must not be
    conflated: ``missing_fields`` records fields the source omitted or returned
    unparseable, ``explicit_zero_fields`` records fields the source genuinely
    published as 0. Zero-filling a missing field silently turns a paid model
    into a free one, so downstream code consults these sets instead.
    """

    source_id: str  # raw ID from the source, e.g. "openai/gpt-5.4"
    provider_slug: str  # source's provider identifier, e.g. "openai"
    short_id: str  # model name without provider, e.g. "gpt-5.4"

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_read_per_mtok: Decimal | None = None  # None = source didn't provide
    cache_write_per_mtok: Decimal | None = None

    source_name: str = ""  # "openrouter", "anthropic_official", etc.
    source_url: str = ""  # provenance URL
    context_length: int | None = None

    raw_extra: dict = field(default_factory=dict)  # extra source-specific fields
    missing_fields: set = field(default_factory=set)
    explicit_zero_fields: set = field(default_factory=set)
