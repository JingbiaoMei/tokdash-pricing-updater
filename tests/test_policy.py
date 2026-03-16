from decimal import Decimal

from pricing_updater.canonicalize import CanonicalModel
from pricing_updater.policy import apply_policy, apply_official_overlay


def _make_canonical(cid, provider, inp, out, cr=Decimal("0"), cw=Decimal("0")):
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


def test_cache_fallback_applied_when_zero():
    m = _make_canonical("gpt-5.4", "openai", "2.50", "15.00")
    cache_fb = {
        "openai": {"cache_read_factor": 0.50, "cache_write_factor": 1.00},
        "default": {"cache_read_factor": 0.10, "cache_write_factor": 1.00},
    }
    result = apply_policy([m], cache_fb)
    # OpenAI fallback: 50% of input for cache_read.
    assert float(result[0].cache_read_per_mtok) == 1.25
    assert float(result[0].cache_write_per_mtok) == 2.50


def test_cache_preserved_when_already_set():
    m = _make_canonical(
        "claude-opus-4.6", "anthropic", "5.00", "25.00",
        cr=Decimal("0.50"), cw=Decimal("6.25"),
    )
    cache_fb = {
        "anthropic": {"cache_read_factor": 0.10, "cache_write_factor": 1.25},
        "default": {"cache_read_factor": 0.10, "cache_write_factor": 1.00},
    }
    result = apply_policy([m], cache_fb)
    # Source already provided cache pricing, should not be overwritten.
    assert float(result[0].cache_read_per_mtok) == 0.50
    assert float(result[0].cache_write_per_mtok) == 6.25


def test_rounding():
    m = _make_canonical("test-model", "default", "1.123456789", "2.987654321")
    result = apply_policy([m], {"default": {"cache_read_factor": 0.1, "cache_write_factor": 1.0}})
    # Should be rounded to 6 decimal places.
    assert str(result[0].input_per_mtok) == "1.123457"
    assert str(result[0].output_per_mtok) == "2.987654"


# --- Official price overlay tests ---

def test_official_overlay_uses_max():
    """When official price is higher than OpenRouter, use official."""
    m = _make_canonical("deepseek-chat", "deepseek", "0.32", "0.89")
    official = {
        "deepseek-chat": {
            "input": 0.28,  # lower than OR → keep OR's 0.32
            "output": 1.10,  # higher than OR → use 1.10
        },
    }
    result = apply_official_overlay([m], official)
    assert float(result[0].input_per_mtok) == 0.32   # kept OpenRouter (higher)
    assert float(result[0].output_per_mtok) == 1.10   # used official (higher)


def test_official_overlay_keeps_openrouter_when_higher():
    """When OpenRouter price is higher than official, keep OpenRouter."""
    m = _make_canonical("deepseek-chat", "deepseek", "0.32", "0.89")
    official = {
        "deepseek-chat": {
            "input": 0.28,   # official lower
            "output": 0.42,  # official lower
        },
    }
    result = apply_official_overlay([m], official)
    assert float(result[0].input_per_mtok) == 0.32  # kept OR
    assert float(result[0].output_per_mtok) == 0.89  # kept OR


def test_official_overlay_fills_cache_from_official():
    """When OpenRouter has no cache pricing but official does, use official."""
    m = _make_canonical("deepseek-chat", "deepseek", "0.32", "0.89",
                        cr=Decimal("0"), cw=Decimal("0"))
    official = {
        "deepseek-chat": {
            "cache_read": 0.028,
            "cache_write": 0.28,
        },
    }
    result = apply_official_overlay([m], official)
    assert float(result[0].cache_read_per_mtok) == 0.028
    assert float(result[0].cache_write_per_mtok) == 0.28


def test_official_overlay_no_match_is_noop():
    """Models without official entries are unchanged."""
    m = _make_canonical("some-model", "openai", "1.00", "2.00")
    result = apply_official_overlay([m], {"other-model": {"input": 5.0}})
    assert float(result[0].input_per_mtok) == 1.00


def test_full_pipeline_with_official():
    """End-to-end: official overlay + cache fallback + rounding."""
    m = _make_canonical("deepseek-chat", "deepseek", "0.32", "0.89",
                        cr=Decimal("0"), cw=Decimal("0"))
    cache_fb = {
        "deepseek": {"cache_read_factor": 0.10, "cache_write_factor": 1.00},
        "default": {"cache_read_factor": 0.10, "cache_write_factor": 1.00},
    }
    official = {
        "deepseek-chat": {
            "input": 0.28,
            "output": 0.42,
            "cache_read": 0.028,
            "cache_write": 0.28,
        },
    }
    result = apply_policy([m], cache_fb, official_prices=official)
    # input: max(0.32, 0.28) = 0.32 (OR higher)
    assert float(result[0].input_per_mtok) == 0.32
    # output: max(0.89, 0.42) = 0.89 (OR higher)
    assert float(result[0].output_per_mtok) == 0.89
    # cache_read: max(0, 0.028) = 0.028 (official provides it)
    assert float(result[0].cache_read_per_mtok) == 0.028
    # cache_write: max(0, 0.28) = 0.28 (official provides it)
    assert float(result[0].cache_write_per_mtok) == 0.28
