"""Coverage makes the frozen slice of the database visible instead of implied."""

from pricing_updater.coverage import build_coverage, summarize
from pricing_updater.diff import PricingChange
from pricing_updater.guards import Decision


def _current():
    return {
        "_comment_openai": "=== OpenAI ===",
        "gpt-5.4": {"provider": "openai", "input": 2.5, "output": 15.0},
        "gpt-4.1": {"provider": "openai", "input": 2.0, "output": 8.0},
        "gpt-5-preview": {"provider": "openai", "input": 1.25, "output": 10.0},
        "gpt-chat-latest": {"provider": "openai", "input": 1.0, "output": 4.0},
        "step-5-preview": {"provider": "stepfun", "input": 1.0, "output": 2.7},
    }


def _proposed():
    return {
        "_comment_openai": "=== OpenAI ===",
        "gpt-5.4": {"provider": "openai", "input": 3.0, "output": 15.0},
        "gpt-5.5": {"provider": "openai", "input": 4.0, "output": 20.0},
    }


def test_every_key_lands_in_exactly_one_state():
    decisions = [
        Decision(PricingChange("gpt-5.4", "price_change", "review_required",
                               fields_changed=["input"]), True),
        Decision(PricingChange("gpt-5.5", "new", "safe_add"), True),
    ]
    dropped = [{"model_id": "gpt-chat-latest", "provider": "openai",
                "missing_fields": ["input"], "source": "s", "reason": "no price"}]

    coverage = build_coverage(
        _current(), _proposed(), decisions, dropped,
        manual_ids={"step-5-preview"}, derived_ids=set(),
    )
    states = coverage["models"]

    assert states["gpt-5.4"]["state"] == "reprice_proposed"
    assert states["gpt-5.5"]["state"] == "add_proposed"
    assert states["gpt-4.1"]["state"] == "not_discovered"
    assert states["gpt-5-preview"]["state"] == "not_discovered"
    assert states["gpt-chat-latest"]["state"] == "unpriceable"
    assert states["step-5-preview"]["state"] == "protected_manual"
    assert "_comment_openai" not in states


def test_frozen_count_tells_the_real_story():
    coverage = build_coverage(
        _current(), _proposed(), [], [], manual_ids={"step-5-preview"}, derived_ids=set(),
    )
    summary = coverage["summary"]

    # All five existing model entries are frozen, including discovered ones.
    assert summary["not_discovered"] == 3
    assert summary["protected_manual"] == 1
    assert summary["frozen"] == 5
    assert summary["existing_preserved"] == 5
    assert summary["movable_by_bot"] == 0
    assert summary["total_keys"] == 6
    assert "5 existing models preserved" in summarize(coverage)


def test_discovered_existing_models_do_not_count_as_new_additions():
    proposed = dict(_current(), **{"gpt-5.5": {"provider": "openai", "input": 4.0}})
    decision = Decision(PricingChange("gpt-5.5", "new", "safe_add"), True)

    coverage = build_coverage(_current(), proposed, [decision], [], set(), set())

    assert coverage["summary"]["movable_by_bot"] == 1
    assert coverage["summary"]["existing_preserved"] == 5


def test_held_changes_are_reported_as_held():
    decision = Decision(
        PricingChange("gpt-5.5", "new", "safe_add"),
        False,
        ["new entry prices input at zero"],
    )
    coverage = build_coverage(
        _current(), _proposed(), [decision], [], set(), set(),
    )

    row = coverage["models"]["gpt-5.5"]
    assert row["state"] == "held"
    assert "at zero" in row["detail"]
