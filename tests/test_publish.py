"""Publication: one bot branch, pinned to a base commit it was built against.

A fake runner stands in for git and gh, so the ordering rules are asserted
rather than trusted: fetch first, refuse a stale or malformed proposal before
touching a branch, lease the force push against an observed commit, and treat a
failed push or PR as a failed run. The filesystem and branch behaviour those
commands produce is covered in test_publish_git.py.
"""

import json
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
NEWER = "c" * 40
BOT_REF = f"refs/remotes/origin/{BOT_BRANCH}"
MAIN_REF = "refs/remotes/origin/main"


@dataclass
class _Proc:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class Runner:
    """Substring-matched fake for git and gh, with per-ref remote SHAs."""

    def __init__(self, responses=None, refs=None, failures=()):
        self.calls: list[str] = []
        self.responses = {"remote get-url": "git@github.com:JingbiaoMei/Tokdash.git\n",
                          **(responses or {})}
        self.refs = {MAIN_REF: SHA} if refs is None else dict(refs)
        self.failures = tuple(failures)

    def __call__(self, cmd, cwd):
        joined = " ".join(cmd)
        self.calls.append(joined)
        for bad in self.failures:
            if bad in joined:
                return _Proc(1, "", f"boom: {bad}")
        if "rev-parse --verify" in joined:
            ref = joined.split("--verify", 1)[1].strip()
            sha = self.refs.get(ref, "")
            return _Proc(0, sha + "\n") if sha else _Proc(1, "", f"unknown ref: {ref}")
        for key, out in self.responses.items():
            if key in joined:
                return _Proc(0, out)
        return _Proc(0, "")

    def ran(self, needle: str) -> bool:
        return any(needle in call for call in self.calls)

    def call_with(self, needle: str) -> str:
        return next((c for c in self.calls if needle in c), "")

    def order(self, first: str, second: str) -> bool:
        a = [c for c in self.calls if first in c]
        b = [c for c in self.calls if second in c]
        return bool(a) and bool(b) and self.calls.index(a[0]) < self.calls.index(b[0])

    @property
    def wrote_anything(self) -> bool:
        return any(any(w in c for w in ("checkout", "commit", "push", "pr create"))
                   for c in self.calls)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    target = tmp_path / "checkout" / TARGET_REL_PATH
    target.parent.mkdir(parents=True)
    target.write_text('{"version": "2.0.1"}')
    return tmp_path / "checkout"


@pytest.fixture
def proposed(tmp_path: Path) -> Path:
    path = tmp_path / "proposed_pricing_db.json"
    path.write_text(json.dumps({"version": "2.0.2", "models": {"a": {"input": 1}}}))
    return path


def _pub(repo, proposed, run, **kw):
    kw.setdefault("generated_at_sha", SHA)
    return publish(repo, proposed, body="b", title="T", run=run, **kw)


def test_opens_a_pr_on_the_bot_branch(repo, proposed):
    run = Runner({"diff --cached": DB_REL + "\n",
                  "pr list": "[]", "pr create": "https://github.com/JingbiaoMei/Tokdash/pull/9\n"})

    result = publish(repo, proposed, body="body", title="Pricing DB update",
                     generated_at_sha=SHA, run=run)

    assert (result.ok, result.action) == (True, "opened")
    assert result.pr_url.endswith("/pull/9")
    assert run.ran(f"checkout -q -B {BOT_BRANCH} {SHA}")
    # The branch is built from the fetched base, not from whatever was checked out.
    assert run.order("fetch", "checkout -q -B")
    # A branch that does not exist remotely needs no lease, and an empty
    # expectation would be refused by git.
    assert not run.ran("--force-with-lease")


def test_the_force_push_leases_the_observed_bot_branch_commit(repo, proposed):
    """A fresh runner has no tracking ref for the bot branch, so name the SHA.

    Without an explicit expectation git refuses the push with "stale info", which
    is what broke every rerun on CI after the first week.
    """
    run = Runner({"diff --cached": DB_REL + "\n", "pr list": "[]"},
                 refs={MAIN_REF: SHA, BOT_REF: NEWER})

    result = _pub(repo, proposed, run)

    assert result.ok, result.detail
    lease = run.call_with("--force-with-lease")
    assert f"--force-with-lease=refs/heads/{BOT_BRANCH}:{NEWER}" in lease
    assert run.order("fetch", "push")


def test_refreshes_the_existing_pr_instead_of_stacking_another(repo, proposed):
    run = Runner({
        "diff --cached": DB_REL + "\n",
        "pr list": '[{"number": 4, "url": "https://github.com/JingbiaoMei/Tokdash/pull/4"}]',
    })

    result = _pub(repo, proposed, run)

    assert (result.ok, result.action) == (True, "updated")
    assert not run.ran("pr create")
    assert run.ran("pr edit 4")


OPEN_DRAFT = '[{"number": 4, "url": "https://example.invalid/4", "isDraft": true}]'
OPEN_READY = '[{"number": 4, "url": "https://example.invalid/4", "isDraft": false}]'


def test_a_clean_rerun_takes_a_draft_back_out(repo, proposed):
    run = Runner({"diff --cached": DB_REL + "\n", "pr list": OPEN_DRAFT})

    result = _pub(repo, proposed, run)

    assert result.ok
    assert run.ran("pr ready 4")


def test_a_clean_rerun_of_an_ready_pr_does_not_fuss(repo, proposed):
    run = Runner({"diff --cached": DB_REL + "\n", "pr list": OPEN_READY})

    result = _pub(repo, proposed, run)

    assert result.ok and not result.warnings
    assert not run.ran("pr ready")


def test_an_anomalous_rerun_leaves_a_draft_alone(repo, proposed):
    run = Runner({"diff --cached": DB_REL + "\n", "pr list": OPEN_DRAFT})

    _pub(repo, proposed, run, draft=True)

    assert not run.ran("pr ready")


def test_an_anomalous_rerun_says_so_when_the_pr_is_already_open(repo, proposed):
    """gh cannot put a PR back into draft, so the warning is all the bot can do."""
    run = Runner({"diff --cached": DB_REL + "\n", "pr list": OPEN_READY})

    result = _pub(repo, proposed, run, draft=True)

    assert result.ok
    assert any("cannot be put back into draft" in w for w in result.warnings)


def test_anomalous_first_run_opens_a_draft(repo, proposed):
    run = Runner({"diff --cached": DB_REL + "\n", "pr list": "[]"})

    _pub(repo, proposed, run, draft=True)

    assert run.call_with("pr create").endswith("--draft")


def test_no_content_change_is_a_clean_no_op(repo, proposed):
    run = Runner({"diff --cached": ""})

    result = _pub(repo, proposed, run)

    assert (result.ok, result.action) == (True, "no_change")
    assert not run.ran("push")
    assert not run.ran("pr create")


def test_a_stale_bot_branch_is_called_out_when_the_new_run_needs_no_change(repo, proposed):
    """The open PR would otherwise sit there showing a diff that no longer exists."""
    run = Runner({"diff --cached": ""}, refs={MAIN_REF: SHA, BOT_REF: NEWER})

    result = _pub(repo, proposed, run)

    assert result.ok
    assert any("empty" in w for w in result.warnings)


def test_refuses_a_diff_wider_than_the_pricing_db(repo, proposed):
    run = Runner({"diff --cached": f"{DB_REL}\nsrc/tokdash/app.py\n"})

    result = _pub(repo, proposed, run)

    assert result.ok is False
    assert "wider than the pricing DB" in result.detail
    assert not run.ran("push")


def test_refuses_to_reset_the_base_branch(repo, proposed):
    run = Runner()

    result = _pub(repo, proposed, run, branch="main", base="main")

    assert result.ok is False
    assert run.calls == []


def test_refuses_a_proposal_that_is_not_pinned_to_a_commit(repo, proposed):
    run = Runner()

    result = publish(repo, proposed, body="b", title="T", run=run)

    assert result.ok is False
    assert "not pinned to a base commit" in result.detail
    assert run.calls == []


def test_refuses_once_the_base_branch_has_moved(repo, proposed):
    """Publishing a stale proposal would silently revert a merged edit."""
    run = Runner(refs={MAIN_REF: NEWER})

    result = _pub(repo, proposed, run)

    assert result.ok is False
    assert "moved from" in result.detail
    assert not run.wrote_anything


def test_a_malformed_proposal_fails_before_any_git_call(tmp_path, repo):
    bad = tmp_path / "proposed_pricing_db.json"
    bad.write_text('{"version": "2.0.2"}')  # no models section
    run = Runner()

    result = _pub(repo, bad, run)

    assert result.ok is False
    assert "models" in result.detail
    assert run.calls == []


def test_push_failure_is_a_failed_run(repo, proposed):
    run = Runner({"diff --cached": DB_REL + "\n"}, failures=("push",))

    result = _pub(repo, proposed, run)

    assert result.ok is False
    assert result.action == "failed"
    assert "push failed" in result.detail


def test_failed_pr_creation_is_reported_not_swallowed(repo, proposed):
    run = Runner({"diff --cached": DB_REL + "\n", "pr list": "[]"}, failures=("pr create",))

    result = _pub(repo, proposed, run)

    assert result.ok is False
    assert "gh pr create failed" in result.detail


def test_missing_label_cannot_orphan_a_pushed_branch(repo, proposed):
    run = Runner({"diff --cached": DB_REL + "\n", "pr list": "[]",
                  "pr create": "https://github.com/JingbiaoMei/Tokdash/pull/9\n"},
                 failures=("add-label",))

    result = _pub(repo, proposed, run, labels=["pricing"])

    assert result.ok is True
    assert result.warnings and "labels" in result.warnings[0]


def test_dry_run_touches_nothing(repo, proposed):
    run = Runner()
    before = (repo / TARGET_REL_PATH).read_text()

    result = _pub(repo, proposed, run, dry_run=True)

    assert (result.ok, result.action) == (True, "dry_run")
    assert (repo / TARGET_REL_PATH).read_text() == before
    # Not even the branch: a checkout -B moves HEAD, which is a change.
    assert not run.wrote_anything


def test_fetch_failure_stops_before_any_branch_move(repo, proposed):
    run = Runner(failures=("fetch",))

    result = _pub(repo, proposed, run)

    assert result.ok is False
    assert not run.ran("checkout")


def test_fetch_base_returns_the_remote_sha():
    run = Runner({"remote get-url": "git@github.com:JingbiaoMei/Tokdash.git\n"})

    assert fetch_base(Path("."), "main", run) == SHA


def test_fetch_base_refuses_to_invent_a_sha():
    run = Runner(refs={})

    with pytest.raises(RuntimeError, match="origin/main"):
        fetch_base(Path("."), "main", run)


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
