"""The publication gate: is this database well-formed, without asking the consumer.

tokdash's suite cannot tell a malformed database from a repriced one, because a
single test can assert both that an alias resolves and that a model costs 2.0.
These are the checks that need no consumer opinion at all, so they can be the
gate: shape, fields, aliases that resolve, nothing removed, no paid price turned
free. Every case here is a way a proposal could otherwise publish a broken
database, starting with the one that started it: a deleted model that the old
price-scaling heuristic filed under "price expectation".
"""

import json

import pytest

from pricing_updater.structural_check import check, main

DB = {
    "version": "2.0.1",
    "aliases": {"alpha-alias": "alpha"},
    "models": {
        "alpha": {"provider": "openai", "input": 1.0, "output": 2.0,
                   "cache_read": 0.5, "cache_write": 1.0, "unit": "1M tokens"},
        "beta": {"provider": "anthropic", "input": 3.0, "output": 15.0,
                  "cache_read": 0.3, "cache_write": 3.75, "unit": "1M tokens"},
    },
}


@pytest.fixture
def base(tmp_path):
    path = tmp_path / "base.json"
    path.write_text(json.dumps(DB))
    return path


@pytest.fixture
def proposed(tmp_path):
    def write(data) -> str:
        path = tmp_path / "proposed.json"
        path.write_text(json.dumps(data))
        return str(path)
    return write


def _mutate(**changes):
    """The base database with models or aliases swapped wholesale."""
    db = json.loads(json.dumps(DB))
    db.update(changes)
    return db


def test_an_ordinary_repricing_is_not_a_finding():
    models = json.loads(json.dumps(DB["models"]))
    models["alpha"]["input"] = 1.1
    models["alpha"]["output"] = 2.2

    assert check(_mutate(models=models), DB) == []


def test_a_new_model_is_not_a_finding():
    models = json.loads(json.dumps(DB["models"]))
    models["gamma"] = {"provider": "deepseek", "input": 0.3, "output": 1.2,
                        "cache_read": 0.05, "cache_write": 0.6, "unit": "1M tokens"}

    assert check(_mutate(models=models), DB) == []


def test_a_removed_model_is_a_finding():
    """The bug this gate exists for: a vanished model is not a price change."""
    models = {k: v for k, v in DB["models"].items() if k != "beta"}

    findings = check(_mutate(models=models), DB)

    assert any("beta" in f and "not in the proposal" in f for f in findings)


def test_a_removed_alias_is_a_finding():
    findings = check(_mutate(aliases={}), DB)

    assert any("alpha-alias" in f for f in findings)


def test_an_alias_that_resolves_to_nothing_is_a_finding():
    findings = check(_mutate(aliases={"alpha-alias": "nowhere"}), DB)

    assert any("nowhere" in f and "not a model" in f for f in findings)


def test_repointing_an_alias_at_a_deleted_model_is_a_finding():
    """The deletion bug wearing a different hat: the alias survives, its target does not."""
    models = {k: v for k, v in DB["models"].items() if k != "alpha"}

    findings = check(_mutate(models=models, aliases={"alpha-alias": "alpha"}), DB)

    assert any("alpha-alias" in f and "removed" in f for f in findings)


def test_repointing_an_alias_at_a_real_model_is_not_a_finding():
    """Alias maintenance is ordinary work, and the target resolving is all that matters."""
    assert check(_mutate(aliases={"alpha-alias": "beta"}), DB) == []


def test_an_alias_already_dangling_in_the_base_is_carried_over():
    """A quirk shipped in the current database is not this proposal's doing.

    Without this the gate would block every proposal over a defect it inherited,
    which is how a gate gets switched off.
    """
    base = json.loads(json.dumps(DB))
    base["aliases"]["ghost"] = "nobody"

    assert check(dict(DB, aliases={"alpha-alias": "alpha", "ghost": "nobody"}), base) == []


def test_a_core_price_that_becomes_free_is_a_finding():
    models = json.loads(json.dumps(DB["models"]))
    models["alpha"]["output"] = 0

    findings = check(_mutate(models=models), DB)

    assert any("output" in f and "free" in f for f in findings)


def test_a_price_that_was_free_may_stay_free():
    """Zero was already the answer, so keeping it is not the bot making a model free."""
    models = json.loads(json.dumps(DB["models"]))
    models["alpha"]["input"] = 0.0
    base = json.loads(json.dumps(DB))
    base["models"]["alpha"]["input"] = 0.0

    assert check(_mutate(models=models), base) == []


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf"), "cheap", None])
def test_a_price_that_is_not_an_amount_is_a_finding(bad):
    models = json.loads(json.dumps(DB["models"]))
    models["alpha"]["input"] = bad

    assert any("input" in f for f in check(_mutate(models=models), DB))


def test_an_entry_missing_a_field_is_a_finding():
    models = json.loads(json.dumps(DB["models"]))
    del models["alpha"]["provider"]
    del models["beta"]["cache_write"]

    findings = check(_mutate(models=models), DB)

    assert any("alpha" in f and "provider" in f for f in findings)
    assert any("beta" in f and "cache_write" in f for f in findings)


def test_a_database_with_no_models_is_a_finding():
    assert check({"version": "1", "models": {}}, DB) == ["the proposed database has no models section"]


def test_a_section_comment_is_not_read_as_a_model():
    """The real database keeps a comment string at the top of the models section."""
    models = dict(DB["models"], note="section header, not a model")

    assert check(_mutate(models=models), DB) == []


def test_without_a_base_there_is_nothing_to_compare():
    """A bare file is still checked on its own merits, just not for deletions."""
    models = {k: v for k, v in DB["models"].items() if k != "beta"}

    findings = check(_mutate(models=models), None)

    assert findings == []


def test_every_problem_is_reported_at_once():
    models = json.loads(json.dumps(DB["models"]))
    models["alpha"]["input"] = -2.0

    findings = check(_mutate(models=models, aliases={"alpha-alias": "nowhere"}), DB)

    assert len(findings) == 2


def test_the_command_fails_on_a_malformed_proposal(base, proposed, capsys):
    models = {k: v for k, v in DB["models"].items() if k != "beta"}

    assert main(["--proposed", proposed(_mutate(models=models)), "--base", str(base)]) == 1
    assert "malformed" in capsys.readouterr().err


def test_the_command_passes_a_repriced_database(base, proposed, capsys, tmp_path):
    models = json.loads(json.dumps(DB["models"]))
    models["alpha"]["input"] = 1.1
    out = tmp_path / "structural.json"

    assert main(["--proposed", proposed(_mutate(models=models)),
                 "--base", str(base), "--json-out", str(out)]) == 0
    assert json.loads(out.read_text())["ok"] is True
    assert "passed" in capsys.readouterr().out


def test_the_command_cannot_read_its_input(proposed, capsys):
    assert main(["--proposed", "/nonexistent/proposed.json"]) == 1
    assert "cannot read" in capsys.readouterr().err
def test_additions_only_accepts_new_models_and_preserves_existing_entries():
    proposed = json.loads(json.dumps(DB))
    proposed["models"]["new-model"] = dict(proposed["models"]["alpha"])

    assert check(proposed, DB, additions_only=True) == []


def test_additions_only_refuses_a_repricing_that_manual_review_can_accept():
    proposed = json.loads(json.dumps(DB))
    proposed["models"]["alpha"]["input"] = 1.1

    assert check(proposed, DB) == []
    assert any("cannot modify an existing entry" in finding
               for finding in check(proposed, DB, additions_only=True))


def test_additions_only_requires_a_base_database():
    assert "requires the current database" in check(DB, None, additions_only=True)[0]


def test_the_command_enforces_additions_only(tmp_path):
    base = tmp_path / "base.json"
    proposal = tmp_path / "proposal.json"
    base.write_text(json.dumps(DB))
    proposed = json.loads(json.dumps(DB))
    proposed["models"]["alpha"]["input"] = 1.1
    proposal.write_text(json.dumps(proposed))

    assert main(["--base", str(base), "--proposed", str(proposal),
                 "--additions-only"]) == 1
