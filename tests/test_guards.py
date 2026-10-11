"""Guards decide what enters the PR diff; a broken scraper is not a pricing day."""

from pricing_updater.diff import PricingChange
from pricing_updater.guards import (
    GuardPolicy,
    check_source_volume,
    classify,
    held_changes,
    proposable_changes,
)


def _policy(**kw):
    base = dict(
        max_changes_per_run=25,
        max_single_field_change_pct=50.0,
        min_openrouter_rows=300,
        hold_explicit_zero=True,
    )
    base.update(kw)
    return GuardPolicy(**base)


def _new(model_id, inp, out):
    return PricingChange(
        model_id=model_id,
        change_type="new",
        classification="safe_add",
        new_entry={"provider": "openai", "input": inp, "output": out},
    )


def _repriced(model_id, field, old, new):
    return PricingChange(
        model_id=model_id,
        change_type="price_change",
        classification="review_required",
        old_entry={"provider": "openai", field: old},
        new_entry={"provider": "openai", field: new, "source": "openrouter.ai/openai/x"},
        fields_changed=[field],
    )


def test_repricing_is_proposable():
    """A provider reprice belongs in the diff for a human to review."""
    decisions = classify([_repriced("gpt-5.4", "input", 2.5, 3.0)], _policy())

    assert len(proposable_changes(decisions)) == 1


def test_large_single_move_is_held():
    decisions = classify([_repriced("gpt-5.4", "input", 2.5, 10.0)], _policy())

    assert proposable_changes(decisions) == []
    assert "max_single_field_change_pct" in decisions[0].reasons[0]


def test_price_dropping_to_zero_is_held():
    decisions = classify([_repriced("gpt-5.4", "output", 15.0, 0)], _policy())

    assert "would drop to 0.0" in decisions[0].reasons[0]


def test_new_entry_at_zero_is_held():
    decisions = classify([_new("cheap-thing", 0, 2.0)], _policy())

    assert "at zero" in decisions[0].reasons[0]

    # ... unless the operator says zeros are expected here.
    permissive = classify([_new("cheap-thing", 0, 2.0)], _policy(hold_explicit_zero=False))
    assert len(proposable_changes(permissive)) == 1


def test_new_entry_with_real_prices_is_proposable():
    decisions = classify([_new("gpt-new", 5.0, 30.0)], _policy())

    assert len(proposable_changes(decisions)) == 1


def test_mass_change_holds_everything():
    changes = [_new(f"m{i}", 1.0, 2.0) for i in range(6)]

    decisions = classify(changes, _policy(max_changes_per_run=5))

    assert proposable_changes(decisions) == []
    assert len(held_changes(decisions)) == 6
    assert "scraper signal" in decisions[0].reasons[-1]


def test_removals_and_alias_retargeting_are_never_automatic():
    stale = PricingChange(
        model_id="old-model",
        change_type="stale",
        classification="manual_only",
        old_entry={"provider": "openai", "input": 1, "output": 2},
        consecutive_misses=4,
    )
    alias = PricingChange(
        model_id="k2p5",
        change_type="alias_change",
        classification="review_required",
        old_entry={"target": "kimi-k2.5"},
        new_entry={"target": "kimi-k3"},
        fields_changed=["target"],
    )

    decisions = classify([stale, alias], _policy())

    assert proposable_changes(decisions) == []
    assert "never automatic" in decisions[0].reasons[0]
    assert "model identity" in decisions[1].reasons[0]


def test_thin_source_response_is_fatal():
    warnings = check_source_volume({"rows_seen": 12}, _policy(min_openrouter_rows=300))

    assert len(warnings) == 1
    assert "refusing to treat this as a pricing day" in warnings[0]

    assert check_source_volume({"rows_seen": 512}, _policy()) == []


def test_policy_reads_config_values():
    policy = GuardPolicy.load(
        {"max_changes_per_run": 3, "max_single_field_change_pct": 10,
         "min_openrouter_rows": 100, "hold_explicit_zero": False}
    )

    assert policy.max_changes_per_run == 3
    assert policy.max_single_field_change_pct == 10.0
    assert policy.min_openrouter_rows == 100
    assert policy.hold_explicit_zero is False


def test_defaults_apply_when_config_is_absent():
    assert GuardPolicy.load(None).max_changes_per_run == 25
