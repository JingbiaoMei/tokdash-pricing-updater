"""The split between a malformed database and a repriced one.

Publication used to gate on tokdash's whole consumer suite, so a guard-approved
price move that broke a test asserting literal numbers stopped the proposal
entirely. These tests hold the distinction the checker is built on: multiply the
prices and whatever fails is a price expectation; whatever fails only under the
proposal is structural and never publishes.
"""

import json
import subprocess
from pathlib import Path

import pytest

from pricing_updater.consumer_checks import (
    DB_REL_PATH,
    DEFAULT_CHECKS,
    Report,
    classify,
    render_markdown,
    scaled_db,
)

DB = {
    "version": "2.0.1",
    "aliases": {"alpha-alias": "alpha"},
    "models": {
        "alpha": {"provider": "openai", "input": 1.0, "output": 2.0,
                   "cache_read": 0.5, "cache_write": 1.0},
    },
}

# A stand-in for tokdash's consumer checks: one test about the shape, one about
# the literal numbers, and a package that reads the database off disk.
CONSUMER_TESTS = '''\
import json
from pathlib import Path

import tokdash


def _db():
    return json.loads((Path(tokdash.__file__).parent / "pricing_db.json").read_text())


def test_the_alias_resolves():
    db = _db()
    assert db["aliases"]["alpha-alias"] in db["models"]


def test_the_shape_survives():
    db = _db()
    assert db["models"]["alpha"]["input"] > 0
    assert db["models"]["alpha"]["input"] <= db["models"]["alpha"]["output"]


def test_alpha_costs_exactly_this():
    db = _db()
    assert db["models"]["alpha"]["input"] == 1.0
'''


@pytest.fixture
def tokdash_like(tmp_path: Path) -> Path:
    """A minimal checkout with a pricing DB, an importable package and tests."""
    pkg = tmp_path / "src" / "tokdash"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "pricing_db.json").write_text(json.dumps(DB, indent=2))
    tests = tmp_path / "tests"
    tests.mkdir()
    # Named for what the checker asks for by default, so the default path is the
    # one under test and a rename in tokdash has to be noticed here.
    (tests / "test_pricing_db_contract.py").write_text(CONSUMER_TESTS)
    for other in DEFAULT_CHECKS[1:]:
        (tmp_path / other).write_text("")
    return tmp_path


def _proposal(tmp_path: Path, models=None, aliases=None) -> Path:
    db = json.loads(json.dumps(DB))
    if models is not None:
        db["models"] = models
    if aliases is not None:
        db["aliases"] = aliases
    path = tmp_path / "proposed_pricing_db.json"
    path.write_text(json.dumps(db))
    return path


def test_multiplying_prices_only_moves_the_numbers():
    scaled = json.loads(scaled_db(json.dumps(DB)))

    assert scaled["models"]["alpha"]["input"] == pytest.approx(1.1)
    assert scaled["aliases"] == DB["aliases"]
    assert scaled["version"] == DB["version"]


def test_a_repricing_breaks_expectations_not_structure(tokdash_like, tmp_path):
    """The reviewer's case: an approved price move must still be publishable."""
    models = json.loads(json.dumps(DB["models"]))
    models["alpha"]["input"] = 1.1
    report = classify(tokdash_like, _proposal(tmp_path, models=models))

    assert report.ran
    assert report.publishable, report.detail
    assert report.needs_draft
    assert not report.structural_failures
    assert any("costs_exactly" in name for name in report.expectation_failures)


def test_a_broken_reference_is_structural_and_never_publishes(tokdash_like, tmp_path):
    """An alias pointing at nothing is malformed, whatever the prices say."""
    report = classify(tokdash_like, _proposal(tmp_path, aliases={"alpha-alias": "gone"}))

    assert not report.publishable
    assert any("alias_resolves" in name for name in report.structural_failures)
    assert not report.needs_draft


def test_a_price_that_inverts_the_relationship_is_structural(tokdash_like, tmp_path):
    """A cost ordering the consumer relies on is a shape fact, not a number.

    The probe scales every price together, so it cannot reproduce a broken
    ordering between two fields, and the failure stays structural.
    """
    models = json.loads(json.dumps(DB["models"]))
    models["alpha"]["input"] = 5.0
    report = classify(tokdash_like, _proposal(tmp_path, models=models))

    assert report.structural_failures, report.detail


def test_no_proposal_is_no_verdict(tokdash_like):
    report = classify(tokdash_like, tokdash_like / "absent.json")

    assert not report.ran and report.publishable is False
    assert "no proposed database" in report.skipped_reason


def test_the_checkout_comes_back_as_it_was_found(tokdash_like, tmp_path):
    """The checker borrows the database file; the next step owns it."""
    target = tokdash_like / DB_REL_PATH
    before = target.read_text()

    models = json.loads(json.dumps(DB["models"]))
    models["alpha"]["input"] = 1.4
    classify(tokdash_like, _proposal(tmp_path, models=models))

    assert target.read_text() == before


def test_a_broken_proposal_leaves_the_checkout_alone_too(tokdash_like, tmp_path):
    target = tokdash_like / DB_REL_PATH
    before = target.read_text()

    classify(tokdash_like, _proposal(tmp_path, aliases={"alpha-alias": "gone"}))

    assert target.read_text() == before


def test_the_draft_is_explained_in_the_body():
    report = Report(ran=True, probe_failures={"tests/test_pricing_db_contract.py::test_alpha_costs_exactly_this"},
                    proposal_failures={"tests/test_pricing_db_contract.py::test_alpha_costs_exactly_this"})

    body = render_markdown(report)

    assert "draft" in body
    assert "tests/test_pricing_db_contract.py::test_alpha_costs_exactly_this" in body


def test_a_clean_run_says_so_briefly():
    body = render_markdown(Report(ran=True))

    assert "pass against the proposed database" in body


def test_the_command_reports_a_structural_failure(tokdash_like, tmp_path, capsys):
    from pricing_updater.consumer_checks import main

    code = main(["--tokdash-repo", str(tokdash_like),
                 "--proposed", str(_proposal(tmp_path, aliases={"alpha-alias": "gone"}))])

    assert code == 1
    assert "structural" in capsys.readouterr().err


def test_the_default_checks_are_the_consumers_release_safe_trio():
    """The trio tokdash documents as its release-safe pricing checks.

    A rename on the consumer side has to be noticed here, and the checker refuses
    to run against a checkout missing one rather than validating nothing.
    """
    assert DEFAULT_CHECKS == (
        "tests/test_pricing_db_contract.py",
        "tests/test_model_name_normalization.py",
        "tests/test_pricing_normalization.py",
    )


def test_a_checkout_without_the_checks_is_an_error_not_a_pass(tokdash_like, tmp_path):
    """Silently collecting nothing would green-light every proposal."""
    for rel in DEFAULT_CHECKS:
        (tmp_path / rel).unlink()

    report = classify(tokdash_like, _proposal(tmp_path, models=DB["models"]))

    assert not report.publishable
    assert "missing from the checkout" in report.error
