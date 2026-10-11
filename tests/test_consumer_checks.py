"""What the consumer checks are allowed to say, and what they may never claim.

The consumer checks used to be the publication gate, and the split between "this
database is malformed" and "this test pins a price" was inferred by multiplying
every price and seeing what broke. That inference cannot work: one test can assert
both that an alias resolves and that a model costs 2.0, so its failure carries no
information about cause. The gate is structural_check now. These tests hold what
is left of this module's job, which is mostly negative: it must not report success
for a run that did not happen, and it must not claim to know why something failed.
"""

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from pricing_updater.consumer_checks import (
    DB_REL_PATH,
    DEFAULT_CHECKS,
    Report,
    Run,
    _read_results,
    _run_pytest,
    classify,
    main,
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

# A stand-in for tokdash's consumer checks. test_alpha_costs_exactly_this reads a
# price, and test_alpha_exists reaches the model through a fixture, which is how a
# deleted model shows up as an execution error rather than a FAILED line.
CONSUMER_TESTS = '''\
import json
from pathlib import Path

import pytest
import tokdash


def _db():
    return json.loads((Path(tokdash.__file__).parent / "pricing_db.json").read_text())


@pytest.fixture
def alpha():
    return _db()["models"]["alpha"]


def test_the_alias_resolves():
    db = _db()
    assert db["aliases"]["alpha-alias"] in db["models"]


def test_alpha_exists(alpha):
    assert alpha["input"] > 0


def test_alpha_costs_exactly_this():
    assert _db()["models"]["alpha"]["input"] == 1.0
'''

TRIVIAL = "def test_nothing_priced_here():\n    assert True\n"


def a_checkout(where: Path) -> Path:
    """A minimal checkout: a pricing DB, an importable package, and the check files.

    Named for the paths the checker asks for by default, so the default is what is
    under test and a rename in tokdash has to be noticed here.
    """
    pkg = where / "src" / "tokdash"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "pricing_db.json").write_text(json.dumps(DB, indent=2))
    for rel in DEFAULT_CHECKS:
        (where / rel).parent.mkdir(parents=True, exist_ok=True)
        (where / rel).write_text(CONSUMER_TESTS if rel == DEFAULT_CHECKS[0] else TRIVIAL)
    return where


@pytest.fixture
def tokdash_like(tmp_path: Path) -> Path:
    return a_checkout(tmp_path)


def _proposal(tmp_path: Path, models=None, aliases=None) -> Path:
    db = json.loads(json.dumps(DB))
    if models is not None:
        db["models"] = models
    if aliases is not None:
        db["aliases"] = aliases
    path = tmp_path / "proposed_pricing_db.json"
    path.write_text(json.dumps(db))
    return path


def _replace_check(repo: Path, which: str = DEFAULT_CHECKS[0], text: str = "") -> None:
    (repo / which).write_text(text)


def test_multiplying_prices_only_moves_the_numbers():
    scaled = json.loads(scaled_db(json.dumps(DB)))

    assert scaled["models"]["alpha"]["input"] == pytest.approx(1.1)
    assert scaled["aliases"] == DB["aliases"]
    assert scaled["version"] == DB["version"]


def test_a_repricing_reaches_a_pr_as_a_draft(tokdash_like, tmp_path):
    """A guard-approved price move breaks pinned numbers; that is a draft, not a dead end."""
    models = json.loads(json.dumps(DB["models"]))
    models["alpha"]["input"] = 1.1
    report = classify(tokdash_like, _proposal(tmp_path, models=models))

    assert report.ran and not report.error, report.detail
    assert report.publishable and report.needs_draft
    assert any("costs_exactly" in name for name in report.new_failures)
    # The multiplier breaks the same test, so the honest label is "sensitive to
    # prices", which is a hint about the cause and never a verdict on it.
    assert report.likely_price_expectations == report.new_failures
    assert not report.unexplained


def test_a_fixture_error_is_a_finding_and_not_a_pass(tokdash_like, tmp_path):
    """A model the proposal deletes surfaces as an error, which used to read as green.

    The proposal removes the model a fixture depends on, so the test errors rather
    than fails and emits no FAILED line. Harvesting FAILED lines scored that as a
    clean run and let the proposal publish.
    """
    models = {}
    report = classify(tokdash_like, _proposal(tmp_path, models=models))

    assert report.publishable, report.detail
    assert report.needs_draft, report.to_json()
    assert any("alpha_exists" in name for name in report.new_failures)


def test_a_check_file_that_cannot_import_stops_the_verdict(tokdash_like, tmp_path):
    """Collection dies before any test runs, so nothing was validated."""
    _replace_check(tokdash_like, DEFAULT_CHECKS[1], "import a_module_nobody_installed\n")

    report = classify(tokdash_like, _proposal(tmp_path, models=DB["models"]))

    assert not report.publishable
    assert "collected no tests" in report.error


def test_an_empty_required_check_file_stops_the_verdict(tokdash_like, tmp_path):
    """An empty file collects nothing, exits zero, and would green-light anything."""
    _replace_check(tokdash_like, DEFAULT_CHECKS[1], "")

    report = classify(tokdash_like, _proposal(tmp_path, models=DB["models"]))

    assert not report.publishable
    assert DEFAULT_CHECKS[1] in report.error


def test_a_run_pytest_refused_is_an_error_not_an_empty_pass(tmp_path):
    """Exit codes outside 0 and 1 mean the tests did not run.

    Driven here rather than through classify, which refuses absent paths earlier
    and for a different reason, so this pins the exit-code mapping itself.
    """
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_real.py").write_text("def test_ok():\n    assert True\n")

    run = _run_pytest(repo, ("tests/test_real.py", "tests/test_absent.py"),
                      {"tests/test_real.py": ["tests/test_real.py::test_ok"]},
                      tmp_path / "run.xml")

    assert not run.completed
    assert "usage error" in run.problems[0]


def test_a_result_that_matches_no_collected_test_is_a_problem(tmp_path):
    """A record that cannot be traced back to a test is a run we cannot read."""
    xml = tmp_path / "run.xml"
    xml.write_text(
        '<testsuites><testsuite name="pytest" tests="2">'
        '<testcase classname="" name="somewhere.test_mystery"><error message="boom"/></testcase>'
        '<testcase classname="other.test_file" name="test_unasked"/></testsuite></testsuites>'
    )
    run = Run()

    _read_results(xml, {("test_real", "test_ok"): "tests/test_real.py::test_ok"}, run)

    assert not run.completed
    assert len(run.problems) == 2


def test_a_checkout_whose_checks_cannot_run_is_not_a_clean_bill(tmp_path):
    """Every test erroring against the untouched database used to read as a pass.

    This is what a missing dependency looks like: the fixtures never set up, every
    test errors, and a report that filed those as "pre-existing" would call the
    proposal validated when nothing was validated.
    """
    repo = a_checkout(tmp_path / "broken-env")
    # Imported inside the fixture, so collection succeeds and it is the run that
    # cannot start, which is the failure mode under test.
    _replace_check(repo, DEFAULT_CHECKS[1], """
import pytest


@pytest.fixture
def needs_it():
    import a_module_this_checkout_lacks
    return a_module_this_checkout_lacks


def test_that_cannot_start(needs_it):
    assert needs_it
""")

    report = classify(repo, _proposal(tmp_path, models=DB["models"]))

    assert not report.publishable
    assert "could not be tested at all" in report.error


def test_a_failure_already_there_is_not_blamed_on_the_proposal(tokdash_like, tmp_path):
    """Blaming a pre-existing failure would train everyone to ignore this output."""
    _replace_check(tokdash_like, text=CONSUMER_TESTS.replace("== 1.0", "== 99.0"))

    report = classify(tokdash_like, _proposal(tmp_path, models=DB["models"]))

    assert report.publishable, report.detail
    assert any("costs_exactly" in name for name in report.base_failures)
    assert not report.new_failures
    assert not report.needs_draft


def test_the_checkout_comes_back_as_it_was_found(tokdash_like, tmp_path):
    """The checker borrows the database file; the next step owns it."""
    target = tokdash_like / DB_REL_PATH
    before = target.read_text()

    models = json.loads(json.dumps(DB["models"]))
    models["alpha"]["input"] = 1.4
    classify(tokdash_like, _proposal(tmp_path, models=models))

    assert target.read_text() == before


def test_no_proposal_is_no_verdict(tokdash_like):
    report = classify(tokdash_like, tokdash_like / "absent.json")

    assert not report.ran and report.publishable is False
    assert "no proposed database" in report.skipped_reason


def test_the_body_labels_which_failures_to_read_first():
    """The reviewer gets the labels and the judgement, not a verdict from a heuristic."""
    report = Report(ran=True,
                    proposal_failures={"tests/test_pricing_db_contract.py::test_pinned",
                                       "tests/test_pricing_db_contract.py::test_broken"},
                    price_sensitive={"tests/test_pricing_db_contract.py::test_pinned"})

    body = render_markdown(report)

    assert "draft" in body
    assert "Read these first" in body
    assert "tests/test_pricing_db_contract.py::test_broken" in body
    assert "sensitive to" in body
    assert "tests/test_pricing_db_contract.py::test_pinned" in body


def test_a_clean_run_says_so_briefly():
    body = render_markdown(Report(ran=True))

    assert "ran against the proposed database and passed" in body


def test_the_command_passes_a_draft_and_fails_an_unvalidated_run(tokdash_like, tmp_path, capsys):
    models = json.loads(json.dumps(DB["models"]))
    models["alpha"]["input"] = 1.1
    assert main(["--tokdash-repo", str(tokdash_like),
                 "--proposed", str(_proposal(tmp_path, models=models))]) == 0
    assert "new failure(s)" in capsys.readouterr().out

    _replace_check(tokdash_like, DEFAULT_CHECKS[2], "")
    assert main(["--tokdash-repo", str(tokdash_like),
                 "--proposed", str(_proposal(tmp_path, models=models))]) == 1
    assert "could not be trusted" in capsys.readouterr().err


def test_the_default_checks_are_the_consumers_release_safe_trio():
    """The trio tokdash documents as its release-safe pricing checks."""
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
