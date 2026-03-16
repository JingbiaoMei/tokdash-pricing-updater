from decimal import Decimal

from pricing_updater.canonicalize import CanonicalModel
from pricing_updater.policy import (
    apply_policy,
    apply_official_overlay,
    resolve_family,
)


def _make(cid, provider, inp, out, cr=Decimal("0"), cw=Decimal("0")):
    return CanonicalModel(
        canonical_id=cid,
        provider=provider,
        input_per_mtok=Decimal(str(inp)),
        output_per_mtok=Decimal(str(out)),
        cache_read_per_mtok=cr,
        cache_write_per_mtok=cw,
        source_url="",
        source_name="openrouter",
    )


# ── Cache fallback ──────────────────────────────────────────

def test_cache_fallback_applied_when_zero():
    m = _make("gpt-5.4", "openai", "2.50", "15.00")
    result = apply_policy([m], {
        "openai": {"cache_read_factor": 0.50, "cache_write_factor": 1.00},
        "default": {"cache_read_factor": 0.10, "cache_write_factor": 1.00},
    })
    assert float(result[0].cache_read_per_mtok) == 1.25
    assert float(result[0].cache_write_per_mtok) == 2.50


def test_cache_preserved_when_already_set():
    m = _make("claude-opus-4.6", "anthropic", "5.00", "25.00",
              cr=Decimal("0.50"), cw=Decimal("6.25"))
    result = apply_policy([m], {
        "anthropic": {"cache_read_factor": 0.10, "cache_write_factor": 1.25},
        "default": {"cache_read_factor": 0.10, "cache_write_factor": 1.00},
    })
    assert float(result[0].cache_read_per_mtok) == 0.50
    assert float(result[0].cache_write_per_mtok) == 6.25


def test_rounding():
    m = _make("test-model", "default", "1.123456789", "2.987654321")
    result = apply_policy([m], {"default": {"cache_read_factor": 0.1, "cache_write_factor": 1.0}})
    assert str(result[0].input_per_mtok) == "1.123457"
    assert str(result[0].output_per_mtok) == "2.987654"


# ── Official overlay (exact key) ────────────────────────────

def test_official_overlay_uses_max():
    m = _make("deepseek-chat", "deepseek", "0.32", "0.89")
    official = {"deepseek-chat": {"input": 0.28, "output": 1.10, "currency": "USD"}}
    result = apply_official_overlay([m], official, fx_rates={"USD": 1.0})
    assert float(result[0].input_per_mtok) == 0.32   # OR higher
    assert float(result[0].output_per_mtok) == 1.10   # official higher


def test_official_overlay_keeps_openrouter_when_higher():
    m = _make("deepseek-chat", "deepseek", "0.32", "0.89")
    official = {"deepseek-chat": {"input": 0.28, "output": 0.42, "currency": "USD"}}
    result = apply_official_overlay([m], official, fx_rates={"USD": 1.0})
    assert float(result[0].input_per_mtok) == 0.32
    assert float(result[0].output_per_mtok) == 0.89


def test_official_overlay_fills_cache_from_official():
    m = _make("deepseek-chat", "deepseek", "0.32", "0.89")
    official = {"deepseek-chat": {"cache_read": 0.028, "cache_write": 0.28, "currency": "USD"}}
    result = apply_official_overlay([m], official, fx_rates={"USD": 1.0})
    assert float(result[0].cache_read_per_mtok) == 0.028
    assert float(result[0].cache_write_per_mtok) == 0.28


def test_official_overlay_no_match_is_noop():
    m = _make("some-model", "openai", "1.00", "2.00")
    result = apply_official_overlay([m], {"other-model": {"input": 5.0}})
    assert float(result[0].input_per_mtok) == 1.00


# ── FX normalization ────────────────────────────────────────

def test_fx_conversion_cny_to_usd():
    """Official CNY price should be converted to USD before max()."""
    m = _make("glm-5", "z-ai", "0.72", "2.30")
    # Official: ¥5.0/MTok input ≈ $0.685 at 0.137 rate → lower than OR's $0.72
    official = {"glm-5": {"input": 5.0, "output": 16.0, "currency": "CNY"}}
    fx = {"USD": 1.0, "CNY": 0.137}
    result = apply_official_overlay([m], official, fx_rates=fx)
    assert float(result[0].input_per_mtok) == 0.72  # OR $0.72 > official $0.685
    # output: official ¥16 * 0.137 = $2.192 < OR $2.30
    assert float(result[0].output_per_mtok) == 2.30


def test_fx_conversion_cny_higher_than_or():
    """When CNY-converted price exceeds OpenRouter, official wins."""
    m = _make("test-model", "z-ai", "0.50", "1.00")
    # Official: ¥5.0 * 0.137 = $0.685 > OR's $0.50
    official = {"test-model": {"input": 5.0, "currency": "CNY"}}
    fx = {"USD": 1.0, "CNY": 0.137}
    result = apply_official_overlay([m], official, fx_rates=fx)
    assert float(result[0].input_per_mtok) == 0.685


# ── Family mapping ──────────────────────────────────────────

def test_resolve_family_exact_match():
    official = {"deepseek-chat": {"input": 0.28}}
    assert resolve_family("deepseek-chat", official, []) == {"input": 0.28}


def test_resolve_family_pattern_match():
    official = {"deepseek-chat": {"input": 0.28}}
    fam = [{"pattern": "deepseek-chat-*", "family": "deepseek-chat"}]
    assert resolve_family("deepseek-chat-v3.1", official, fam) == {"input": 0.28}


def test_resolve_family_no_match():
    official = {"deepseek-chat": {"input": 0.28}}
    fam = [{"pattern": "deepseek-chat-*", "family": "deepseek-chat"}]
    assert resolve_family("minimax-m2.5", official, fam) is None


def test_family_overlay_inherits_price():
    """deepseek-chat-v3.1 should inherit deepseek-chat official pricing."""
    m = _make("deepseek-chat-v3.1", "deepseek", "0.15", "0.75")
    official = {"deepseek-chat": {"input": 0.28, "output": 0.42, "currency": "USD"}}
    fam = [{"pattern": "deepseek-chat-*", "family": "deepseek-chat"}]
    result = apply_official_overlay([m], official, family_map=fam, fx_rates={"USD": 1.0})
    # input: max(0.15, 0.28) = 0.28
    assert float(result[0].input_per_mtok) == 0.28
    # output: max(0.75, 0.42) = 0.75
    assert float(result[0].output_per_mtok) == 0.75


def test_family_deepseek_r1_variants():
    """deepseek-r1-0528 should inherit deepseek-r1 pricing."""
    m = _make("deepseek-r1-0528", "deepseek", "0.45", "2.15")
    official = {"deepseek-r1": {"input": 0.28, "output": 0.42, "currency": "USD",
                                "cache_read": 0.028, "cache_write": 0.28}}
    fam = [{"pattern": "deepseek-r1-*", "family": "deepseek-r1"}]
    result = apply_official_overlay([m], official, family_map=fam, fx_rates={"USD": 1.0})
    # OR prices higher, kept
    assert float(result[0].input_per_mtok) == 0.45
    assert float(result[0].output_per_mtok) == 2.15
    # cache from official (OR had 0)
    assert float(result[0].cache_read_per_mtok) == 0.028


# ── Full pipeline ───────────────────────────────────────────

def test_full_pipeline_with_fx_and_family():
    m = _make("deepseek-v3.2", "deepseek", "0.26", "0.38")
    cache_fb = {"deepseek": {"cache_read_factor": 0.10, "cache_write_factor": 1.00},
                "default": {"cache_read_factor": 0.10, "cache_write_factor": 1.00}}
    official = {"deepseek-chat": {"input": 0.28, "output": 0.42,
                                  "cache_read": 0.028, "cache_write": 0.28,
                                  "currency": "USD"}}
    fam = [{"pattern": "deepseek-v3*", "family": "deepseek-chat"}]
    fx = {"USD": 1.0}
    result = apply_policy([m], cache_fb, official_prices=official,
                          family_map=fam, fx_rates=fx)
    # input: max(0.26, 0.28) = 0.28
    assert float(result[0].input_per_mtok) == 0.28
    # output: max(0.38, 0.42) = 0.42
    assert float(result[0].output_per_mtok) == 0.42
    # cache from official
    assert float(result[0].cache_read_per_mtok) == 0.028
    assert float(result[0].cache_write_per_mtok) == 0.28
