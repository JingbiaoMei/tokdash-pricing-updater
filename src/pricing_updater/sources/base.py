"""Base data model for scraped pricing data."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

# The four price fields a pricing_db entry carries.
PRICE_FIELDS = ("input", "output", "cache_read", "cache_write")

# Fields that must exist for an entry to be priceable at all.
CORE_PRICE_FIELDS = ("input", "output")

# canonical field name -> the CanonicalModel attribute holding it. Shared by the
# policy overlay and the guards so a new price field cannot be checked in one
# place and forgotten in the other.
PRICE_ATTRS = (
    ("input", "input_per_mtok"),
    ("output", "output_per_mtok"),
    ("cache_read", "cache_read_per_mtok"),
    ("cache_write", "cache_write_per_mtok"),
)


def clean_price(raw) -> Decimal | None:
    """Return ``raw`` as a publishable price, or None if it cannot be one.

    A price must be a finite, non-negative number. NaN, infinity, a negative
    rate and unparseable strings all return None, which callers record as "we do
    not know this price" rather than passing a broken number downstream: guards
    and renderers only ask whether a field is missing, so a NaN that survives
    parsing looks exactly like a valid price to them.
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value < 0:
        return None
    return value


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
    # Subset of missing_fields the source did answer, with a value that cannot
    # be a price. Same handling, different explanation for a human reader.
    invalid_fields: set = field(default_factory=set)
