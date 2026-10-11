"""A missing price must never become a free model."""

from decimal import Decimal

import pytest

from pricing_updater.canonicalize import canonicalize
from pricing_updater.guards import partition_unpriceable
from pricing_updater.policy import apply_cache_fallback, apply_official_overlay
from pricing_updater.sources import openrouter
from pricing_updater.sources.openrouter import _parse_per_mtok, fetch_openrouter


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _serve(monkeypatch, models):
    monkeypatch.setattr(openrouter.httpx, "get", lambda *a, **k: _Resp({"data": models}))


# ── parsing ────────────────────────────────────────────────

def test_zero_and_missing_are_different_facts():
    assert _parse_per_mtok(None) == (None, False)      # absent
    assert _parse_per_mtok("n/a") == (None, False)     # unparseable
    assert _parse_per_mtok("0") == (None, True)        # published free


def test_real_price_scales_to_per_mtok():
    value, published_zero = _parse_per_mtok("0.000003")
    assert published_zero is False
    assert value == Decimal("3")


# ── scraping ───────────────────────────────────────────────

def test_missing_prompt_is_recorded_not_priced_free(monkeypatch):
    _serve(monkeypatch, [
        {"id": "openai/gpt-x", "pricing": {"prompt": None, "completion": "0.000012"}},
    ])
    models = fetch_openrouter({"openai": "openai"}, [])

    assert len(models) == 1
    assert "input" in models[0].missing_fields
    assert "output" not in models[0].missing_fields


def test_both_prices_absent_is_dropped_and_counted(monkeypatch):
    _serve(monkeypatch, [
        {"id": "openai/gpt-y", "pricing": {"prompt": None, "completion": ""}},
    ])
    stats = {}
    models = fetch_openrouter({"openai": "openai"}, [], stats=stats)

    assert models == []
    assert stats["unpriced_skipped"] == 1


def test_published_free_pair_is_skipped_without_alarm(monkeypatch):
    _serve(monkeypatch, [
        {"id": "openai/gpt-free", "pricing": {"prompt": "0", "completion": "0"}},
    ])
    stats = {}
    models = fetch_openrouter({"openai": "openai"}, [], stats=stats)

    assert models == []
    assert stats["free_skipped"] == 1
    assert stats["unpriced_skipped"] == 0


def test_published_zero_cache_survives_as_a_zero():
    """A provider that really does bill cache reads at 0 must not get a
    heuristic rate stamped over the fact."""
    model = _canonical("free-cache", input_v=Decimal("1"), cache_read=Decimal("0"))
    model.explicit_zero_fields = {"cache_read"}

    apply_cache_fallback([model], {"openai": {"cache_read_factor": 0.5}})

    assert model.cache_read_per_mtok == Decimal("0")


def test_missing_cache_field_still_gets_the_heuristic():
    model = _canonical("no-cache", input_v=Decimal("1"), cache_read=Decimal("0"))
    model.missing_fields = {"cache_read"}

    apply_cache_fallback([model], {"openai": {"cache_read_factor": 0.5}})

    assert model.cache_read_per_mtok == Decimal("0.5")


def test_official_price_rescues_a_missing_field():
    model = _canonical("rescued", input_v=Decimal("0"), output_v=Decimal("2"))
    model.missing_fields = {"input"}

    apply_official_overlay(
        [model], {"rescued": {"input": 1.5, "currency": "USD", "source": "example.com"}},
    )

    assert model.input_per_mtok == Decimal("1.5")
    assert "input" not in model.missing_fields
    assert model.field_sources["input"].startswith("official:")


def test_partition_holds_unpriceable_models_out():
    good = _canonical("priced", input_v=Decimal("1"), output_v=Decimal("2"))
    bad = _canonical("half-known", input_v=Decimal("0"), output_v=Decimal("2"))
    bad.missing_fields = {"input"}

    usable, dropped = partition_unpriceable([good, bad])

    assert [m.canonical_id for m in usable] == ["priced"]
    assert dropped[0]["model_id"] == "half-known"
    assert dropped[0]["missing_fields"] == ["input"]


def test_provenance_records_which_source_settled_the_value():
    scraped = canonicalize(
        [_scraped("gpt-a", input_v=Decimal("2.5"), output_v=Decimal("15"))],
        {"openai": "openai"},
    )
    assert scraped[0].field_sources["input"] == "openrouter"
    assert scraped[0].evidence["input"] == {"openrouter": 2.5}


# ── helpers ────────────────────────────────────────────────

def _canonical(cid, input_v, output_v=Decimal("0"), cache_read=Decimal("0"),
               cache_write=Decimal("0")):
    from pricing_updater.canonicalize import CanonicalModel

    return CanonicalModel(
        canonical_id=cid,
        provider="openai",
        input_per_mtok=input_v,
        output_per_mtok=output_v,
        cache_read_per_mtok=cache_read,
        cache_write_per_mtok=cache_write,
        source_url="openrouter.ai/openai/" + cid,
        source_name="openrouter",
    )


def _scraped(short_id, input_v, output_v):
    from pricing_updater.sources.base import ScrapedModel

    return ScrapedModel(
        source_id=f"openai/{short_id}",
        provider_slug="openai",
        short_id=short_id,
        input_per_mtok=input_v,
        output_per_mtok=output_v,
        source_name="openrouter",
    )
