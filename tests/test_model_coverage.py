"""Fixture tests verifying specific models are scraped and handled correctly.

Tests the models explicitly named in the review: minimax-m2-her, minimax-01,
deepseek-chat, deepseek-r1, mimo-v2-flash, glm-5, kimi-k2.5.
"""

import json
from pathlib import Path

import pytest


# Tracked fixture, not the gitignored output/raw captures. A clean clone used to
# skip these ten tests silently because the snapshot they needed was never
# committed; CI looked green while asserting nothing.
SNAPSHOT_PATH = Path(__file__).parent / "data" / "openrouter_fixture.json"


@pytest.fixture(scope="module")
def openrouter_models():
    """Load the saved OpenRouter snapshot as {short_id: pricing_dict}."""
    if not SNAPSHOT_PATH.exists():
        pytest.skip(f"Snapshot not found: {SNAPSHOT_PATH}")
    raw = json.loads(SNAPSHOT_PATH.read_text())
    models = {}
    for m in raw.get("data", []):
        mid = m.get("id", "")
        if "/" in mid:
            _, _, short = mid.partition("/")
            models[mid] = m
            models[short] = m
    return models


# ── Model presence tests ───────────────────────────────

def test_minimax_m2_her_present(openrouter_models):
    assert "minimax/minimax-m2-her" in openrouter_models


def test_minimax_01_present(openrouter_models):
    assert "minimax/minimax-01" in openrouter_models


def test_deepseek_chat_present(openrouter_models):
    assert "deepseek/deepseek-chat" in openrouter_models


def test_deepseek_r1_present(openrouter_models):
    assert "deepseek/deepseek-r1" in openrouter_models


def test_mimo_v2_flash_present(openrouter_models):
    assert "xiaomi/mimo-v2-flash" in openrouter_models


def test_glm_5_present(openrouter_models):
    assert "z-ai/glm-5" in openrouter_models


def test_kimi_k25_present(openrouter_models):
    assert "moonshotai/kimi-k2.5" in openrouter_models


# ── Pricing data sanity ────────────────────────────────

def _get_pricing(openrouter_models, full_id):
    m = openrouter_models.get(full_id, {})
    return m.get("pricing", {})


def test_deepseek_chat_has_pricing(openrouter_models):
    p = _get_pricing(openrouter_models, "deepseek/deepseek-chat")
    assert float(p.get("prompt", 0)) > 0
    assert float(p.get("completion", 0)) > 0


def test_mimo_v2_flash_has_pricing(openrouter_models):
    p = _get_pricing(openrouter_models, "xiaomi/mimo-v2-flash")
    assert float(p.get("prompt", 0)) > 0


def test_glm_5_has_pricing(openrouter_models):
    p = _get_pricing(openrouter_models, "z-ai/glm-5")
    assert float(p.get("prompt", 0)) > 0


# ── k2p5 derivation test ──────────────────────────────

def test_k2p5_is_derived_not_manual():
    """k2p5 should be in derived_models.yaml, not manual_models.yaml."""
    import yaml
    cfg_dir = Path(__file__).parent.parent / "config"
    manual = yaml.safe_load((cfg_dir / "manual_models.yaml").read_text()) or {}
    derived = yaml.safe_load((cfg_dir / "derived_models.yaml").read_text()) or {}
    assert "k2p5" not in (manual.get("models") or {})
    assert "k2p5" in (derived.get("models") or {})
    assert derived["models"]["k2p5"]["derive_from"] == "kimi-k2.5"


# ── Family map test ───────────────────────────────────

def test_family_map_covers_deepseek_variants():
    """All known DeepSeek variants should match a family pattern."""
    import yaml
    from pricing_updater.policy import resolve_family
    cfg_dir = Path(__file__).parent.parent / "config"
    fam_cfg = yaml.safe_load((cfg_dir / "official_family_map.yaml").read_text()) or {}
    official_cfg = yaml.safe_load((cfg_dir / "official_prices.yaml").read_text()) or {}

    families = fam_cfg.get("families", [])
    official = official_cfg.get("models", {})

    variants = [
        "deepseek-chat-v3.1", "deepseek-chat-v3-0324",
        "deepseek-v3.2", "deepseek-v3.1-terminus", "deepseek-v3.2-speciale",
        "deepseek-r1-0528", "deepseek-r1-distill-qwen-32b", "deepseek-r1-distill-llama-70b",
    ]
    for v in variants:
        entry = resolve_family(v, official, families)
        assert entry is not None, f"{v} should resolve via family map"
