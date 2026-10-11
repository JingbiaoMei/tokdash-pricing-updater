"""End-to-end shape of the CI path: scrape, guard, propose, publish, or fail.

The network, the official scrapers and the publication commands are all
replaced, so what is under test is the wiring between them: what reaches the
diff, what gets held, which artifacts exist for a reviewer, and whether a
publication failure actually fails the run.
"""

import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from pricing_updater import cli, github_pr
from pricing_updater.github_pr import PublishResult
from pricing_updater.sources import openrouter

SHA = "b" * 40

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


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A fake tokdash checkout, a stubbed OpenRouter, and no live official scrapers."""
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

    monkeypatch.setattr(openrouter.httpx, "get", lambda *a, **k: _Resp())
    from pricing_updater.sources import official_fetch
    monkeypatch.setattr(official_fetch, "fetch_all_official", lambda timeout=30: {})
    # Tiny fixture response, so the row floor must not trip in tests.
    monkeypatch.setattr(cli, "_load_anomaly_cfg", lambda: {
        "max_changes_per_run": 25,
        "max_single_field_change_pct": 50,
        "min_openrouter_rows": 1,
        "hold_explicit_zero": True,
    })
    monkeypatch.setattr(github_pr, "fetch_base", lambda repo, base="main", run=None: SHA)
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
