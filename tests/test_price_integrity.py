"""A missing price must never become a free model."""

from decimal import Decimal

import pytest

from pricing_updater.canonicalize import canonicalize
from pricing_updater.guards import partition_unpriceable
from pricing_updater.policy import (
    apply_cache_fallback,
    apply_official_overlay,
    apply_policy,
)
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
    assert _parse_per_mtok(None) == (None, "absent")     # the key was not there
    assert _parse_per_mtok("0") == (None, "zero")        # published free
    assert _parse_per_mtok("n/a") == (None, "invalid")   # cannot be a price


def test_real_price_scales_to_per_mtok():
    value, kind = _parse_per_mtok("0.000003")
    assert kind == "price"
    assert value == Decimal("3")


@pytest.mark.parametrize("raw", ["-0.000001", "NaN", "Infinity", "-Inf"])
def test_a_number_that_cannot_be_a_price_is_not_one(raw):
    """Negative, NaN and infinite rates must not reach a guard as valid numbers.

    Every guard downstream asks whether a field is missing, so a NaN that parses
    successfully is indistinguishable from a real price and gets proposed.
    """
    assert _parse_per_mtok(raw) == (None, "invalid")


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


def test_invalid_rate_is_a_gap_the_source_did_not_admit_to(monkeypatch):
    _serve(monkeypatch, [
        {"id": "openai/gpt-broken",
         "pricing": {"prompt": "-0.000001", "completion": "0.000002",
                     "input_cache_read": "NaN", "input_cache_write": "0.0000002"}},
    ])
    stats: dict = {}
    models = fetch_openrouter({"openai": "openai"}, [], stats=stats)

    assert len(models) == 1
    assert models[0].missing_fields == {"input", "cache_read"}
    assert stats["invalid_fields"] == {"openai/gpt-broken": ["cache_read", "input"]}


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


# ── review reproductions, 2026-10-11 ───────────────────────

def test_official_zero_cache_read_beats_the_heuristic():
    """An official "cache reads are free" is a fact, not a gap to guess over.

    The overlay used to settle a field only when the official value was larger,
    and a missing cache field is internally 0, so an official 0 lost the
    comparison and apply_cache_fallback stamped a guessed factor on it.
    """
    model = _canonical("free-on-official", input_v=Decimal("1"), output_v=Decimal("2"))
    model.missing_fields = {"cache_read", "cache_write"}

    apply_policy(
        [model],
        {"openai": {"cache_read_factor": 0.5, "cache_write_factor": 1.0}},
        official_prices={"free-on-official": {
            "cache_read": 0, "currency": "USD", "source": "official.example",
        }},
    )

    assert model.cache_read_per_mtok == Decimal("0")
    assert model.field_sources["cache_read"].startswith("official:")
    # Still flagged, so nothing downstream can mistake it for an unfilled field.
    assert "cache_read" in model.explicit_zero_fields
    # The field official did not publish keeps the heuristic it always had.
    assert model.cache_write_per_mtok == Decimal("1")


def test_a_non_numeric_official_price_is_ignored_not_applied():
    model = _canonical("robust", input_v=Decimal("3"), output_v=Decimal("6"))

    apply_official_overlay(
        [model], {"robust": {"input": "NaN", "currency": "USD", "source": "x.example"}},
    )

    assert model.input_per_mtok == Decimal("3")
    assert model.field_sources.get("input") != "official:x.example"


def test_guard_holds_a_model_whose_price_cannot_be_published():
    model = _canonical("negative", input_v=Decimal("-1"), output_v=Decimal("2"))

    usable, dropped = partition_unpriceable([model, _canonical("fine", Decimal("1"))])

    assert [m.canonical_id for m in usable] == ["fine"]
    assert dropped[0]["invalid_fields"] == ["input"]
    assert "unusable" in dropped[0]["reason"]


def test_a_broken_price_is_reported_as_broken_not_as_absent():
    """"No price" and "a price that cannot be a price" send a reviewer elsewhere."""
    broken = _canonical("broken", input_v=Decimal("Infinity"), output_v=Decimal("2"))
    broken.missing_fields = {"input"}
    broken.invalid_fields = {"input"}
    absent = _canonical("absent", input_v=Decimal("0"), output_v=Decimal("2"))
    absent.missing_fields = {"input"}

    _usable, dropped = partition_unpriceable([broken, absent])

    reasons = {d["model_id"]: d["reason"] for d in dropped}
    assert reasons["broken"] == "source published an unusable value for input"
    assert reasons["absent"] == "source supplied no price for input"


def test_renderer_refuses_to_write_an_unusable_price():
    """Last gate. If a broken number reaches the renderer, the run must stop."""
    from pricing_updater.render_tokdash_db import _model_to_entry

    with pytest.raises(ValueError, match="input"):
        _model_to_entry(_canonical("bad", input_v=Decimal("NaN"), output_v=Decimal("2")))


# ── official adapters: a published zero is a fact ──────────


def _deepseek_page(prices: str) -> str:
    """Keep the zero/missing regressions on DeepSeek's labelled table layout."""
    import re

    amounts = re.findall(r"\$[\d.]+", prices)
    cache = (f"<tr><td>1M INPUT TOKENS (CACHE HIT)</td><td>{amounts[1]}</td></tr>"
             if len(amounts) == 3 else "")
    return ("<table><tr><td>MODEL</td><td>deepseek-chat</td></tr>"
            f"<tr><td>1M INPUT TOKENS (CACHE MISS)</td><td>{amounts[0]}</td></tr>"
            + cache
            + f"<tr><td>1M OUTPUT TOKENS</td><td>{amounts[-1]}</td></tr></table>")


def _deepseek_fetched(monkeypatch, html):
    """What the DeepSeek adapter hands the overlay, with the network out of it."""
    from pricing_updater.sources import official_deepseek, official_fetch

    parsed = official_deepseek._try_parse_table(_deepseek_page(html))
    monkeypatch.setattr(official_deepseek, "fetch_deepseek_official", lambda timeout: parsed)
    return official_fetch._fetch_deepseek(30), parsed


def test_an_official_free_cache_price_reaches_the_overlay(monkeypatch):
    """DeepSeek publishes cache reads at $0, and a truthiness test read that as absent.

    With the zero dropped, apply_cache_fallback guessed a positive price over a
    real published free-cache rate, and the PR body credited DeepSeek for a number
    it never printed.
    """
    fetched, parsed = _deepseek_fetched(
        monkeypatch, "$2 per 1M tokens; cache hit $0 per 1M tokens; output $4 per 1M"
    )
    assert parsed and parsed[0].cache_read_per_mtok == 0
    assert fetched["deepseek-chat"]["cache_read"] == 0

    model = _canonical("deepseek-chat", Decimal("2"), Decimal("4"))
    model.missing_fields = {"cache_read"}
    apply_official_overlay([model], fetched)
    apply_cache_fallback([model], {"openai": {"cache_read_factor": 0.5}})

    assert model.cache_read_per_mtok == 0, "free cache reads survived the pipeline"
    assert "cache_read" in model.explicit_zero_fields
    assert model.field_sources["cache_read"].startswith("official:")


def test_the_adapter_invents_no_cache_price_the_page_never_printed(monkeypatch):
    """Two published prices are two facts. A third guessed one is not official.

    The adapter used to fall back to 10% of input, which put a made-up number in
    the column reserved for what the provider actually published, and for a free
    model that made-up number was a 0 the overlay then defended.
    """
    _, parsed = _deepseek_fetched(monkeypatch, "$2 per 1M, output $4 per 1M")

    assert parsed, "input and output still parsed"
    assert parsed[0].cache_read_per_mtok is None
