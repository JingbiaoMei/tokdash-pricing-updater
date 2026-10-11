"""Publication: one bot branch, generated against a freshly fetched base.

A fake runner stands in for git and gh, so the ordering rules are asserted
rather than trusted: fetch first, refuse a wide diff, never touch the base
branch, and treat a failed push or PR as a failed run.
"""

from dataclasses import dataclass
from pathlib import Path

import pytest

from pricing_updater.github_pr import (
    BOT_BRANCH,
    TARGET_REL_PATH,
    fetch_base,
    publish,
    slug_from_remote,
)

DB_REL = str(TARGET_REL_PATH)
SHA = "a" * 40


@dataclass
class _Proc:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class Runner:
    """Substring-matched fake for git and gh."""

    def __init__(self, responses=None, failures=()):
        self.calls: list[str] = []
        self.responses = {"rev-parse": SHA, **(responses or {})}
        self.failures = tuple(failures)

    def __call__(self, cmd, cwd):
        joined = " ".join(cmd)
        self.calls.append(joined)
        for bad in self.failures:
            if bad in joined:
                return _Proc(1, "", f"boom: {bad}")
        for key, out in self.responses.items():
            if key in joined:
                return _Proc(0, out)
        return _Proc(0, "")

    def ran(self, needle: str) -> bool:
        return any(needle in call for call in self.calls)

    def order(self, first: str, second: str) -> bool:
        a = [c for c in self.calls if first in c]
        b = [c for c in self.calls if second in c]
        return bool(a) and bool(b) and self.calls.index(a[0]) < self.calls.index(b[0])


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    target = tmp_path / "checkout" / TARGET_REL_PATH
    target.parent.mkdir(parents=True)
    target.write_text('{"version": "2.0.1"}')
    return tmp_path / "checkout"


@pytest.fixture
def proposed(tmp_path: Path) -> Path:
    path = tmp_path / "proposed_pricing_db.json"
    path.write_text('{"version": "2.0.2"}')
    return path


def _ssh():
    return {"remote get-url": "git@github.com:JingbiaoMei/Tokdash.git\n"}


def test_opens_a_pr_on_the_bot_branch(repo, proposed):
    run = Runner({**_ssh(), "diff --cached": DB_REL + "\n",
                  "pr list": "[]", "pr create": "https://github.com/JingbiaoMei/Tokdash/pull/9\n"})

    result = publish(repo, proposed, body="body", title="Pricing DB update", run=run)

    assert (result.ok, result.action) == (True, "opened")
    assert result.pr_url.endswith("/pull/9")
    assert run.ran("--force-with-lease")
    assert run.ran(f"checkout -B {BOT_BRANCH} {SHA}")
    # The branch must be built from the fetched base, not from whatever was checked out.
    assert run.order("fetch", "checkout -B")


def test_refreshes_the_existing_pr_instead_of_stacking_another(repo, proposed):
    run = Runner({
        **_ssh(),
        "diff --cached": DB_REL + "\n",
        "pr list": '[{"number": 4, "url": "https://github.com/JingbiaoMei/Tokdash/pull/4"}]',
    })

    result = publish(repo, proposed, body="new body", title="T", run=run)

    assert (result.ok, result.action) == (True, "updated")
    assert not run.ran("pr create")
    assert run.ran("pr edit 4")


def test_no_content_change_is_a_clean_no_op(repo, proposed):
    run = Runner({**_ssh(), "diff --cached": ""})

    result = publish(repo, proposed, body="b", title="T", run=run)

    assert (result.ok, result.action) == (True, "no_change")
    assert not run.ran("push")
    assert not run.ran("pr create")


def test_refuses_a_diff_wider_than_the_pricing_db(repo, proposed):
    run = Runner({**_ssh(), "diff --cached": f"{DB_REL}\nsrc/tokdash/app.py\n"})

    result = publish(repo, proposed, body="b", title="T", run=run)

    assert result.ok is False
    assert "wider than the pricing DB" in result.detail
    assert not run.ran("push")


def test_refuses_to_reset_the_base_branch(repo, proposed):
    run = Runner({**_ssh()})

    result = publish(repo, proposed, body="b", title="T", branch="main", base="main", run=run)

    assert result.ok is False
    assert run.calls == []


def test_push_failure_is_a_failed_run(repo, proposed):
    run = Runner({**_ssh(), "diff --cached": DB_REL + "\n"}, failures=("push",))

    result = publish(repo, proposed, body="b", title="T", run=run)

    assert result.ok is False
    assert result.action == "failed"
    assert "push failed" in result.detail


def test_failed_pr_creation_is_reported_not_swallowed(repo, proposed):
    run = Runner({**_ssh(), "diff --cached": DB_REL + "\n", "pr list": "[]"},
                 failures=("pr create",))

    result = publish(repo, proposed, body="b", title="T", run=run)

    assert result.ok is False
    assert "gh pr create failed" in result.detail


def test_missing_label_cannot_orphan_a_pushed_branch(repo, proposed):
    run = Runner(
        {**_ssh(), "diff --cached": DB_REL + "\n", "pr list": "[]",
         "pr create": "https://github.com/JingbiaoMei/Tokdash/pull/9\n"},
        failures=("add-label",),
    )

    result = publish(repo, proposed, body="b", title="T", labels=["pricing"], run=run)

    assert result.ok is True
    assert result.warnings and "labels" in result.warnings[0]


def test_dry_run_touches_nothing(repo, proposed):
    run = Runner({**_ssh()})
    before = (repo / TARGET_REL_PATH).read_text()

    result = publish(repo, proposed, body="b", title="T", dry_run=True, run=run)

    assert (result.ok, result.action) == (True, "dry_run")
    assert (repo / TARGET_REL_PATH).read_text() == before
    assert not run.ran("push")
    assert not run.ran("commit")


def test_fetch_failure_stops_before_any_branch_move(repo, proposed):
    run = Runner(_ssh(), failures=("fetch",))

    result = publish(repo, proposed, body="b", title="T", run=run)

    assert result.ok is False
    assert not run.ran("checkout")


def test_fetch_base_returns_the_remote_sha():
    run = Runner({"rev-parse": SHA + "\n"})

    assert fetch_base(Path("."), "main", run) == SHA


@pytest.mark.parametrize(
    "url,expected",
    [
        ("git@github.com:JingbiaoMei/Tokdash.git", "JingbiaoMei/Tokdash"),
        ("https://github.com/JingbiaoMei/Tokdash.git", "JingbiaoMei/Tokdash"),
        ("https://github.com/JingbiaoMei/Tokdash", "JingbiaoMei/Tokdash"),
    ],
)
def test_slug_forms_that_gh_accepts(tmp_path, url, expected):
    run = Runner({"remote get-url": url + "\n"})

    assert slug_from_remote(tmp_path, run) == expected
