from decimal import Decimal

from pricing_updater.canonicalize import CanonicalModel, canonicalize
from pricing_updater.sources.base import ScrapedModel


def _make_scraped(source_id, provider, short_id, inp=1.0, out=2.0):
    return ScrapedModel(
        source_id=source_id,
        provider_slug=provider,
        short_id=short_id,
        input_per_mtok=Decimal(str(inp)),
        output_per_mtok=Decimal(str(out)),
        source_name="openrouter",
    )


def test_basic_canonicalization():
    scraped = [_make_scraped("openai/gpt-5.4", "openai", "gpt-5.4")]
    provider_map = {"openai": "openai"}
    result = canonicalize(scraped, provider_map)
    assert len(result) == 1
    assert result[0].canonical_id == "gpt-5.4"
    assert result[0].provider == "openai"


def test_provider_slug_mapping():
    scraped = [_make_scraped("moonshotai/kimi-k2.5", "moonshotai", "kimi-k2.5")]
    provider_map = {"moonshotai": "moonshotai"}
    result = canonicalize(scraped, provider_map)
    assert result[0].canonical_id == "kimi-k2.5"
    assert result[0].provider == "moonshotai"


def test_id_override():
    scraped = [_make_scraped("openai/gpt-5.2", "openai", "gpt-5.2")]
    provider_map = {"openai": "openai"}
    overrides = {"gpt-5.2": "gpt-5.2-renamed"}
    result = canonicalize(scraped, provider_map, id_overrides=overrides)
    assert result[0].canonical_id == "gpt-5.2-renamed"
