from decimal import Decimal
from collections import OrderedDict
from pathlib import Path

import yaml
from pricing_updater.canonicalize import CanonicalModel
from pricing_updater.render_tokdash_db import render, resolve_derived_models, dump_json


def _make_model(cid, provider, inp, out, cr, cw, source=""):
    return CanonicalModel(
        canonical_id=cid,
        provider=provider,
        input_per_mtok=Decimal(str(inp)),
        output_per_mtok=Decimal(str(out)),
        cache_read_per_mtok=Decimal(str(cr)),
        cache_write_per_mtok=Decimal(str(cw)),
        source_url=source,
        source_name="openrouter",
    )


def test_render_basic():
    models = [
        _make_model("gpt-5.4", "openai", 2.5, 15.0, 0.25, 2.5,
                     source="openrouter.ai/openai/gpt-5.4"),
    ]
    provider_order = [
        {"key": "_comment_openai", "label": "=== OpenAI ===", "providers": ["openai"]},
    ]
    db = render(
        canonical_models=models,
        manual_models={},
        derived_models={},
        aliases={},
        provider_order=provider_order,
        current_version="2.0.1",
    )
    assert db["version"] == "2.0.2"
    assert "gpt-5.4" in db["models"]
    assert db["models"]["gpt-5.4"]["input"] == 2.5
    assert db["models"]["_comment_openai"] == "=== OpenAI ==="


def test_manual_models_included():
    manual = {
        "custom-model": {
            "provider": "openai",
            "input": 1.0,
            "output": 2.0,
            "unit": "per_million_tokens",
        },
    }
    provider_order = [
        {"key": "_comment_openai", "label": "=== OpenAI ===", "providers": ["openai"]},
    ]
    db = render(
        canonical_models=[],
        manual_models=manual,
        derived_models={},
        aliases={},
        provider_order=provider_order,
        current_version="1.0.0",
    )
    assert "custom-model" in db["models"]


def test_render_raises_for_provider_missing_from_provider_order():
    models = [
        _make_model("qwen3.5-397b-a17b", "qwen", 0.195, 1.56, 0.0195, 0.195),
    ]
    provider_order = [
        {"key": "_comment_openai", "label": "=== OpenAI ===", "providers": ["openai"]},
    ]

    try:
        render(
            canonical_models=models,
            manual_models={},
            derived_models={},
            aliases={},
            provider_order=provider_order,
            current_version="2.0.1",
        )
        assert False, "render() should fail when provider_order omits a provider"
    except ValueError as exc:
        assert "qwen" in str(exc)
        assert "qwen3.5-397b-a17b" in str(exc)


def test_derived_models_resolve():
    base = _make_model("claude-sonnet-4.6", "anthropic", 3.0, 15.0, 0.3, 3.75)
    derived_cfg = {
        "antigravity-claude-sonnet-4-6": {
            "derive_from": "claude-sonnet-4.6",
            "provider": "google",
            "note": "Using Claude Sonnet 4.6 pricing.",
        },
    }
    canonical_by_id = {"claude-sonnet-4.6": base}
    resolved = resolve_derived_models(derived_cfg, canonical_by_id, {})
    assert "antigravity-claude-sonnet-4-6" in resolved
    assert resolved["antigravity-claude-sonnet-4-6"]["input"] == 3.0
    assert resolved["antigravity-claude-sonnet-4-6"]["provider"] == "google"


def test_selected_live_providers_are_configured_for_scrape_and_render():
    cfg_dir = Path(__file__).parent.parent / "config"
    providers_cfg = yaml.safe_load((cfg_dir / "tracked_providers.yaml").read_text()) or {}
    policy_cfg = yaml.safe_load((cfg_dir / "pricing_policy.yaml").read_text()) or {}

    tracked = {
        entry["slug"]: entry["tokdash_provider"]
        for entry in providers_cfg.get("providers", [])
    }
    provider_sections = {
        provider
        for section in policy_cfg.get("provider_order", [])
        for provider in section.get("providers", [])
    }

    assert tracked.get("qwen") == "qwen"
    assert tracked.get("bytedance") == "bytedance"
    assert tracked.get("bytedance-seed") == "bytedance"
    assert tracked.get("tencent") == "tencent"

    assert "qwen" in provider_sections
    assert "bytedance" in provider_sections
    assert "tencent" in provider_sections


def test_dump_json_produces_valid_json():
    import json
    db = {"version": "1.0.0", "models": {"m": {"input": Decimal("1.5")}}}
    text = dump_json(db)
    parsed = json.loads(text)
    assert parsed["models"]["m"]["input"] == 1.5
