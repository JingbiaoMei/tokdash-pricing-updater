from decimal import Decimal

from pricing_updater.canonicalize import CanonicalModel
from pricing_updater.policy import apply_policy


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
