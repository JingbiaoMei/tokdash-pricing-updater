from pricing_updater.diff import compute_diff


def test_new_model_detected():
    current = {"existing-model": {"provider": "openai", "input": 1.0, "output": 2.0}}
    proposed = {
        "existing-model": {"provider": "openai", "input": 1.0, "output": 2.0},
        "new-model": {"provider": "openai", "input": 3.0, "output": 6.0},
    }
    changes, ls = compute_diff(current, proposed, set(), set(), {})
    assert len(changes) == 1
    assert changes[0].change_type == "new"
    assert changes[0].classification == "safe_add"
    assert changes[0].model_id == "new-model"


def test_price_change_detected():
    current = {"m1": {"provider": "openai", "input": 1.0, "output": 2.0}}
    proposed = {"m1": {"provider": "openai", "input": 1.5, "output": 2.0}}
    changes, _ = compute_diff(current, proposed, set(), set(), {})
    assert len(changes) == 1
    assert changes[0].change_type == "price_change"
    assert changes[0].classification == "review_required"
    assert "input" in changes[0].fields_changed


def test_stale_model_needs_repeated_misses():
    current = {
        "old-model": {"provider": "openai", "input": 1.0, "output": 2.0},
    }
    proposed = {}  # model not in proposed

    # First miss: no change reported (below threshold).
    changes, ls = compute_diff(current, proposed, set(), set(), {}, min_misses=3)
    assert len(changes) == 0
    assert ls["old-model"]["consecutive_misses"] == 1

    # Second miss.
    changes, ls = compute_diff(current, proposed, set(), set(), ls, min_misses=3)
    assert len(changes) == 0
    assert ls["old-model"]["consecutive_misses"] == 2

    # Third miss: now flagged.
    changes, ls = compute_diff(current, proposed, set(), set(), ls, min_misses=3)
    assert len(changes) == 1
    assert changes[0].classification == "manual_only"
    assert changes[0].consecutive_misses == 3


def test_manual_models_never_flagged():
    current = {"manual-m": {"provider": "openai", "input": 1.0, "output": 2.0}}
    proposed = {}  # not in proposed
    changes, _ = compute_diff(
        current, proposed, manual_ids={"manual-m"}, derived_ids=set(),
        last_seen={}, min_misses=1,
    )
    assert len(changes) == 0


def test_derived_models_never_flagged():
    current = {"antigravity-x": {"provider": "google", "input": 5.0, "output": 25.0}}
    proposed = {}
    changes, _ = compute_diff(
        current, proposed, manual_ids=set(), derived_ids={"antigravity-x"},
        last_seen={}, min_misses=1,
    )
    assert len(changes) == 0


def test_stale_disabled_skips_flagging():
    """With enable_stale=False, a model missing from the scrape is never flagged,
    no matter how many consecutive misses it has accumulated."""
    current = {"legacy-model": {"provider": "openai", "input": 1.0, "output": 2.0}}
    proposed = {}  # not in proposed
    # Even with a tiny threshold and a pre-loaded miss count, nothing is flagged.
    last_seen = {"legacy-model": {"consecutive_misses": 9, "first_seen": "2026-01-01"}}
    changes, _ = compute_diff(
        current, proposed, set(), set(),
        last_seen=last_seen, min_misses=1, enable_stale=False,
    )
    assert changes == []


def test_stale_enabled_still_flags():
    """Sanity: with the default (enabled), the same model is flagged once over threshold."""
    current = {"legacy-model": {"provider": "openai", "input": 1.0, "output": 2.0}}
    proposed = {}
    last_seen = {"legacy-model": {"consecutive_misses": 9, "first_seen": "2026-01-01"}}
    changes, _ = compute_diff(
        current, proposed, set(), set(),
        last_seen=last_seen, min_misses=1, enable_stale=True,
    )
    assert len(changes) == 1
    assert changes[0].classification == "manual_only"


def test_comment_entries_ignored():
    current = {
        "_comment_openai": "=== OpenAI Models ===",
        "gpt-5.4": {"provider": "openai", "input": 2.5, "output": 15.0},
    }
    proposed = {
        "_comment_openai": "=== OpenAI Models ===",
        "gpt-5.4": {"provider": "openai", "input": 2.5, "output": 15.0},
    }
    changes, _ = compute_diff(current, proposed, set(), set(), {})
    assert len(changes) == 0
