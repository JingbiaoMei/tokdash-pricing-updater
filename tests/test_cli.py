from pricing_updater.cli import _apply_approved, _step_diff
from pricing_updater.diff import PricingChange


def test_apply_approved_preserves_new_provider_sections():
    current_db = {
        "version": "2.0.2",
        "models": {
            "_comment_openai": "=== OpenAI Models ===",
            "gpt-5.4": {"provider": "openai", "input": 2.5, "output": 15.0},
        },
    }
    proposed_db = {
        "version": "2.0.3",
        "models": {
            "_comment_openai": "=== OpenAI Models ===",
            "gpt-5.4": {"provider": "openai", "input": 2.5, "output": 15.0},
            "_comment_qwen": "=== Qwen Models ===",
            "qwen3.5-397b-a17b": {"provider": "qwen", "input": 0.39, "output": 2.34},
        },
    }
    approved = [
        PricingChange(
            model_id="qwen3.5-397b-a17b",
            change_type="new",
            classification="safe_add",
            new_entry=proposed_db["models"]["qwen3.5-397b-a17b"],
        )
    ]

    final_db = _apply_approved(current_db, proposed_db, approved)

    assert "_comment_qwen" in final_db["models"]
    assert final_db["models"]["_comment_qwen"] == "=== Qwen Models ==="
    assert "qwen3.5-397b-a17b" in final_db["models"]


def test_apply_approved_restores_unapproved_price_changes():
    current_db = {
        "version": "2.0.2",
        "models": {
            "_comment_openai": "=== OpenAI Models ===",
            "gpt-5.4": {"provider": "openai", "input": 2.5, "output": 15.0},
        },
    }
    proposed_db = {
        "version": "2.0.3",
        "models": {
            "_comment_openai": "=== OpenAI Models ===",
            "gpt-5.4": {"provider": "openai", "input": 3.0, "output": 15.0},
        },
    }

    final_db = _apply_approved(current_db, proposed_db, approved=[])

    assert final_db["models"]["gpt-5.4"]["input"] == 2.5


def test_step_diff_detects_alias_only_add(tmp_path):
    current_db = {
        "version": "2.0.2",
        "aliases": {},
        "models": {
            "seed-2.0-lite": {"provider": "bytedance-seed", "input": 0.11, "output": 0.22},
        },
    }
    proposed_db = {
        "version": "2.0.3",
        "aliases": {"doubao-seed-2.0-lite": "seed-2.0-lite"},
        "models": current_db["models"],
    }

    changes, report = _step_diff(
        current_db,
        proposed_db,
        manual_cfg={},
        derived_cfg={},
        state_dir=tmp_path,
        policy_cfg={"removal": {"enabled": False}},
    )

    assert [c.change_type for c in changes] == ["alias_add"]
    assert changes[0].classification == "safe_add"
    assert "doubao-seed-2.0-lite" in report


def test_apply_approved_does_not_apply_unapproved_alias_remap():
    current_db = {
        "version": "2.0.2",
        "aliases": {"doubao-seed-1.6": "seed-1.6"},
        "models": {
            "seed-1.6": {"provider": "bytedance-seed", "input": 0.8, "output": 0.8},
            "seed-1.6-flash": {"provider": "bytedance-seed", "input": 0.4, "output": 0.4},
        },
    }
    proposed_db = {
        "version": "2.0.3",
        "aliases": {"doubao-seed-1.6": "seed-1.6-flash"},
        "models": current_db["models"],
    }

    final_db = _apply_approved(current_db, proposed_db, approved=[])

    assert final_db["aliases"]["doubao-seed-1.6"] == "seed-1.6"


def test_apply_approved_preserves_order_and_inserts_in_place():
    current_db = {
        "version": "2.0.2",
        "models": {
            "_comment_openai": "=== OpenAI Models ===",
            "gpt-5.4": {"provider": "openai", "input": 2.5, "output": 15.0},
            "_comment_google": "=== Google Models ===",
            "gemini-3-pro": {"provider": "google", "input": 2.0, "output": 12.0},
            "glm-4.6": {"provider": "z-ai", "input": 0.6, "output": 2.2},  # legacy
        },
    }
    proposed_db = {
        "version": "2.0.3",
        "models": {
            "_comment_openai": "=== OpenAI Models ===",
            "gpt-5.4": {"provider": "openai", "input": 2.5, "output": 15.0},
            "gpt-5.5": {"provider": "openai", "input": 3.0, "output": 18.0},
            "_comment_google": "=== Google Models ===",
            "gemini-3-pro": {"provider": "google", "input": 2.0, "output": 12.0},
            "gemini-3.5-flash": {"provider": "google", "input": 1.5, "output": 9.0},
            "_comment_reka": "=== Reka Models ===",
            "reka-flash-3": {"provider": "reka", "input": 0.1, "output": 0.2},
        },
    }
    approved = [
        PricingChange(model_id="gpt-5.5", change_type="new", classification="safe_add",
                      new_entry=proposed_db["models"]["gpt-5.5"]),
        PricingChange(model_id="gemini-3.5-flash", change_type="new", classification="safe_add",
                      new_entry=proposed_db["models"]["gemini-3.5-flash"]),
        PricingChange(model_id="reka-flash-3", change_type="new", classification="safe_add",
                      new_entry=proposed_db["models"]["reka-flash-3"]),
    ]

    final = _apply_approved(current_db, proposed_db, approved)

    # New models inserted next to their provider; brand-new provider appended;
    # legacy entry (glm-4.6) never moves.
    assert list(final["models"].keys()) == [
        "_comment_openai",
        "gpt-5.4",
        "gpt-5.5",            # inserted after last openai entry
        "_comment_google",
        "gemini-3-pro",
        "gemini-3.5-flash",   # inserted after last google entry
        "glm-4.6",            # legacy, unmoved
        "_comment_reka",      # brand-new provider section, appended at end
        "reka-flash-3",
    ]
