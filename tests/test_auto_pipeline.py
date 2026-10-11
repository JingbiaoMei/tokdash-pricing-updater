"""End-to-end shape of the CI path: scrape, guard, propose, publish, or fail.

The network, the official scrapers and the publication commands are all
replaced, so what is under test is the wiring between them: what reaches the
diff, what gets held, which artifacts exist for a reviewer, and whether a
publication failure actually fails the run.
"""

import json
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from pricing_updater import cli, github_pr
from pricing_updater.github_pr import PublishResult
from pricing_updater.sources import openrouter


OPENROUTER_ROWS = [
    # Existing model, input moves 1.0 -> 1.2 (20%, inside the guard).
    {"id": "openai/unit-test-alpha", "pricing": {"prompt": "0.0000012", "completion": "0.000002"}},
    # Existing model, input halves. A 60% move is a scraper signal, not a price.
    {"id": "openai/unit-test-gamma", "pricing": {"prompt": "0.0000004", "completion": "0.000002"}},
    # Brand new model with real prices.
    {"id": "openai/unit-test-new", "pricing": {"prompt": "0.000003", "completion": "0.000008"}},
    # Brand new model priced at zero on input: held for a human.
    {"id": "openai/unit-test-free", "pricing": {"prompt": "0", "completion": "0.000001"}},
]


class _Resp:
    def __init__(self, rows=None):
        self.rows = OPENROUTER_ROWS if rows is None else rows

    def raise_for_status(self):
        return None

    def json(self):
        return {"data": self.rows}


def _git(repo: Path, *args):
    return subprocess.run(["git", *args], cwd=str(repo), text=True, capture_output=True)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A committed tokdash checkout, a stubbed OpenRouter, no live official scrapers.

    The checkout is a real git repo because cmd_auto reads the database as of the
    base commit it fetched, not off the working tree.
    """
    import yaml
    aliases = (yaml.safe_load(
        (cli._project_root() / "config/aliases.yaml").read_text()
    ) or {}).get("aliases", {})

    repo = tmp_path / "tokdash"
    target = repo / "src" / "tokdash" / "pricing_db.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({
        "version": "2.0.10",
        "lastUpdated": "2026-01-01T00:00:00Z",
        "aliases": aliases,
        "models": {
            "_comment_openai": "=== OpenAI Models ===",
            "unit-test-alpha": {"provider": "openai", "input": 1.0, "output": 2.0,
                                 "cache_read": 0.5, "cache_write": 1.0},
            "unit-test-gamma": {"provider": "openai", "input": 1.0, "output": 2.0,
                                 "cache_read": 0.5, "cache_write": 1.0},
        },
    }, indent=2))
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base database")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    monkeypatch.setattr(openrouter.httpx, "get", lambda *a, **k: _Resp())
    from pricing_updater.sources import official_fetch

    def _official(timeout=30, health=None):
        # One adapter down: the run must say so rather than quietly reading YAML.
        if health is not None:
            health.append({"source": "StepFun", "status": "failed", "models": 0,
                           "detail": "HTTP 403"})
        return {}

    monkeypatch.setattr(official_fetch, "fetch_all_official", _official)
    # Tiny fixture response, so the row floor must not trip in tests.
    monkeypatch.setattr(cli, "_load_anomaly_cfg", lambda: {
        "max_changes_per_run": 25,
        "max_single_field_change_pct": 50,
        "min_openrouter_rows": 1,
        "hold_explicit_zero": True,
    })
    # The real fetch needs a remote; the pinned commit is what matters here.
    monkeypatch.setattr(github_pr, "fetch_base", lambda repo, base="main", run=None: base_sha)
    # Artifacts and state stay in the test sandbox: a test run must not write
    # into the repository's own output/ or state/.
    monkeypatch.setenv("PRICING_UPDATER_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("PRICING_UPDATER_STATE_DIR", str(tmp_path / "state"))

    return repo


def _args(repo: Path, create_pr: bool = True) -> Namespace:
    return Namespace(
        tokdash_repo=str(repo),
        create_pr=create_pr,
        dry_run=False,
        branch="bot/pricing-db",
        base="main",
        labels="pricing",
        max_body_rows=60,
    )


@pytest.fixture
def recorder(monkeypatch):
    calls: list[dict] = []

    def fake_publish(**kwargs):
        calls.append(kwargs)
        return PublishResult(ok=True, action="opened", pr_url="https://example.invalid/pull/1")

    monkeypatch.setattr(github_pr, "publish", fake_publish)
    return calls


def test_repricing_and_additions_reach_the_diff(workspace, recorder, capsys):
    cli.cmd_auto(_args(workspace))

    kwargs = recorder[0]
    body = kwargs["body"]
    assert "### Repricing (1)" in body
    assert "### Additions (1)" in body
    assert "unit-test-alpha" in body
    assert "unit-test-new" in body

    proposed = json.loads(
        (cli._output_dir() / "proposals/proposed_pricing_db.json").read_text()
    )
    assert proposed["models"]["unit-test-alpha"]["input"] == 1.2
    assert "unit-test-new" in proposed["models"]
    # Held changes must not be applied.
    assert "unit-test-free" not in proposed["models"]
    assert proposed["models"]["unit-test-gamma"]["input"] == 1.0

    out = capsys.readouterr().out
    assert "2 proposable, 2 held" in out


def test_reviewer_evidence_artifacts_are_written(workspace, recorder):
    cli.cmd_auto(_args(workspace))

    out = cli._output_dir() / "proposals"
    today = __import__("datetime").date.today().isoformat()
    coverage = json.loads((out / f"coverage_{today}.json").read_text())
    provenance = json.loads((out / f"provenance_{today}.json").read_text())

    assert coverage["summary"]["held"] == 2
    assert coverage["models"]["unit-test-free"]["state"] == "held"
    assert "max_single_field_change_pct" in coverage["models"]["unit-test-gamma"]["detail"]
    assert provenance["unit-test-new"]["field_sources"]["input"] == "openrouter"


def test_held_changes_are_named_in_the_body(workspace, recorder):
    cli.cmd_auto(_args(workspace))

    body = recorder[0]["body"]
    assert "Held back, not in this diff (2)" in body
    assert "max_single_field_change_pct" in body
    assert "at zero" in body


def test_publication_failure_fails_the_run(workspace, monkeypatch):
    monkeypatch.setattr(
        github_pr, "publish",
        lambda **kw: PublishResult(ok=False, action="failed", detail="push failed"),
    )

    with pytest.raises(SystemExit) as exc:
        cli.cmd_auto(_args(workspace))

    assert exc.value.code == 1


def test_nothing_proposable_is_a_clean_no_op(workspace, recorder, monkeypatch):
    """Held findings are a successful run that publishes nothing."""
    rows = [
        {"id": "openai/unit-test-free",
         "pricing": {"prompt": "0", "completion": "0.000001"}},
        {"id": "openai/unit-test-alpha",
         "pricing": {"prompt": "0.0000001", "completion": "0.000002"}},
    ]
    monkeypatch.setattr(openrouter.httpx, "get", lambda *a, **k: _Resp(rows))

    cli.cmd_auto(_args(workspace))

    assert recorder == []


def test_thin_source_response_is_fatal(workspace, recorder, monkeypatch):
    monkeypatch.setattr(cli, "_load_anomaly_cfg", lambda: {
        "max_changes_per_run": 25,
        "max_single_field_change_pct": 50,
        "min_openrouter_rows": 100000,
        "hold_explicit_zero": True,
    })

    with pytest.raises(SystemExit) as exc:
        cli.cmd_auto(_args(workspace))

    assert exc.value.code == 1
    assert recorder == []

def test_official_adapter_failures_reach_the_reviewer(workspace, recorder, capsys):
    """A dead scraper otherwise looks exactly like a day with no price changes."""
    cli.cmd_auto(_args(workspace))

    today = __import__("datetime").date.today().isoformat()
    health = json.loads(
        (cli._output_dir() / "proposals" / f"source_health_{today}.json").read_text()
    )
    assert health == [{"source": "StepFun", "status": "failed", "models": 0,
                       "detail": "HTTP 403"}]
    assert "StepFun failed" in recorder[0]["body"]


def test_the_proposal_is_pinned_to_the_base_it_was_generated_against(workspace, recorder):
    """generate -> validate -> publish share one commit, or publication is refused."""
    cli.cmd_auto(_args(workspace))

    base_file = cli._output_dir() / "proposals" / "base_sha.txt"
    assert base_file.read_text().strip() == recorder[0]["generated_at_sha"]
    assert recorder[0]["generated_at_sha"]


def test_the_publish_decisions_survive_the_artifact_handoff(workspace, recorder):
    """CI publishes from artifacts, so title and draft state have to be in them.

    A publish job that rebuilds them invents a PR that disagrees with the run
    that produced the diff inside it.
    """
    cli.cmd_auto(_args(workspace))

    plan = json.loads(
        (cli._output_dir() / "proposals" / "publish_plan.json").read_text()
    )
    assert plan["base_sha"] == recorder[0]["generated_at_sha"]
    assert plan["title"] == recorder[0]["title"]
    assert plan["draft"] == recorder[0]["draft"]


def test_a_run_where_most_changes_look_wrong_opens_a_draft(workspace, recorder, monkeypatch):
    """Several models moving 60% at once is a scraper signal, not a pricing day."""
    rows = [
        # Two existing models moving 80% each, which no provider does quietly.
        {"id": "openai/unit-test-alpha", "pricing": {"prompt": "0.0000002",
                                                     "completion": "0.000002"}},
        {"id": "openai/unit-test-gamma", "pricing": {"prompt": "0.0000002",
                                                     "completion": "0.000002"}},
        # Three brand new models, so there is still something to propose.
        {"id": "openai/unit-test-new", "pricing": {"prompt": "0.000003",
                                                   "completion": "0.000008"}},
    ]
    monkeypatch.setattr(openrouter.httpx, "get", lambda *a, **k: _Resp(rows))

    cli.cmd_auto(_args(workspace))

    assert recorder[0]["draft"] is True


def test_one_ordinary_price_move_does_not_draft_the_whole_pr(workspace, recorder):
    """The everyday case: one reviewable repricing and one held outlier."""
    cli.cmd_auto(_args(workspace))

    assert recorder[0]["draft"] is False


def test_a_run_that_proposes_nothing_leaves_nothing_to_publish(workspace, recorder, monkeypatch):
    """Yesterday's proposal cannot ride along on today's base SHA.

    The publication artifacts are cleared before generation rather than written
    on the success path, because every early return below the diff is a run with
    no proposal, and the publish job reads those files by name.
    """
    cli.cmd_auto(_args(workspace))
    out = cli._output_dir() / "proposals"
    assert (out / "proposed_pricing_db.json").exists()
    assert (out / "publish_plan.json").exists()
    assert (out / "pr_body.md").exists()
    published = len(recorder)

    # Main moves, and every source-derived change is now too large to propose.
    target = workspace / github_pr.TARGET_REL_PATH
    db = json.loads(target.read_text())
    db["models"]["human-added"] = {"provider": "openai", "input": 3, "output": 6,
                                    "cache_read": 0.3, "cache_write": 3}
    target.write_text(json.dumps(db))
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-qm", "a human adds a model")
    tip = _git(workspace, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setattr(github_pr, "fetch_base", lambda repo, base="main", run=None: tip)
    monkeypatch.setattr(openrouter.httpx, "get", lambda *a, **k: _Resp([
        {"id": "openai/unit-test-alpha",
         "pricing": {"prompt": "0.0000001", "completion": "0.000002"}},
    ]))

    # Nothing proposable is a clean run that publishes nothing, so the artifacts a
    # publisher reads by name have to be gone, not merely unused.
    cli.cmd_auto(_args(workspace))

    assert len(recorder) == published, "a run with no proposal must not publish"
    assert not (out / "proposed_pricing_db.json").exists()
    assert not (out / "publish_plan.json").exists()
    assert not (out / "pr_body.md").exists()
    assert (out / "base_sha.txt").read_text().strip() == tip
