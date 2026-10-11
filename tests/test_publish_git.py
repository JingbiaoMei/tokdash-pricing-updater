"""Publication against real git repositories.

The fake runner in test_publish.py asserts the commands publish() asks for.
These tests assert what git actually does with them, because the two failures
that mattered in review were invisible to a mock: a dry run that moved the
branch head, and a force-with-lease that had no tracking ref to lease against on
a fresh runner and died with "stale info".

Only temporary directories are touched. gh is stubbed; nothing reaches GitHub.
"""

import copy
import json
import subprocess
from pathlib import Path

import pytest

from pricing_updater.github_pr import TARGET_REL_PATH, publish

BASE = {"version": "2.0.1", "aliases": {}, "models": {
    "alpha": {"provider": "openai", "input": 1, "output": 2,
               "cache_read": 0.5, "cache_write": 1},
}}
BOT = "bot/pricing-db"
# Swapped by tests that want gh to report an already-open PR for the bot branch.
_OPEN_PR = ""


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(repo), text=True, capture_output=True,
        check=check,
    )


def stubbed_github(args, cwd):
    """Stand in for the gh calls and for origin URLs, which a temp repo lacks."""
    if args[:4] == ["git", "remote", "get-url", "origin"]:
        return subprocess.CompletedProcess(args, 0, "https://github.com/example/Tokdash.git\n", "")
    if args[0] == "gh":
        out = _OPEN_PR if _OPEN_PR and args[1:3] == ["pr", "list"] else (
            "" if args[1:3] == ["pr", "ready"] else "https://github.com/example/Tokdash/pull/1\n"
        )
        return subprocess.CompletedProcess(args, 0, out, "")
    return subprocess.run([str(a) for a in args], cwd=str(cwd), text=True,
                          capture_output=True)


def write_db(repo, data):
    path = Path(repo) / TARGET_REL_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def commit_db(repo, data, message):
    write_db(repo, data)
    git(repo, "add", str(TARGET_REL_PATH))
    git(repo, "commit", "-qm", message)


@pytest.fixture
def remote(tmp_path):
    """A bare origin with a main branch, seeded with BASE."""
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(bare))
    seed = tmp_path / "seed"
    git(tmp_path, "init", "-b", "main", str(seed))
    git(seed, "config", "user.name", "Test")
    git(seed, "config", "user.email", "test@example.invalid")
    commit_db(seed, BASE, "base")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "-u", "origin", "main")
    git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
    return bare


def push_bot_branch(bare, workdir_parent, data):
    """Open a bot branch on the remote built from an older main."""
    work = workdir_parent / "bot-setup"
    git(work.parent, "clone", str(bare), str(work))
    git(work, "config", "user.name", "Bot")
    git(work, "config", "user.email", "bot@example.invalid")
    git(work, "checkout", "-B", BOT)
    commit_db(work, data, "existing bot proposal")
    git(work, "push", "origin", f"{BOT}:{BOT}")
    return git(work, "rev-parse", "HEAD").stdout.strip()


def test_dry_run_leaves_the_checked_out_branch_alone(remote, tmp_path):
    work = tmp_path / "work"
    git(tmp_path, "clone", str(remote), str(work))
    git(work, "config", "user.name", "Bot")
    git(work, "config", "user.email", "bot@example.invalid")
    git(work, "checkout", "-B", BOT)
    head_before = git(work, "rev-parse", "HEAD").stdout.strip()

    tip = git(work, "rev-parse", "origin/main").stdout.strip()
    proposal = tmp_path / "proposal.json"
    newer = copy.deepcopy(BASE)
    newer["models"]["alpha"]["input"] = 1.2
    proposal.write_text(json.dumps(newer))

    result = publish(work, proposal, "body", "title", dry_run=True,
                     generated_at_sha=tip, run=stubbed_github)

    assert (result.ok, result.action) == (True, "dry_run")
    assert git(work, "rev-parse", "HEAD").stdout.strip() == head_before
    assert json.loads((work / TARGET_REL_PATH).read_text()) == BASE


def test_refreshing_an_existing_bot_branch_works_on_a_fresh_runner(remote, tmp_path):
    """The branch move is leased against the fetched commit, not a tracking ref.

    A --single-branch clone, which is what CI makes, has never heard of the bot
    branch, so --force-with-lease with no explicit expectation refuses the push.
    """
    older = copy.deepcopy(BASE)
    older["models"]["alpha"]["input"] = 1.1
    push_bot_branch(remote, tmp_path, older)

    # A later commit on main that the proposal must not overwrite.
    later = copy.deepcopy(BASE)
    later["models"]["human-added"] = {"provider": "openai", "input": 3, "output": 6,
                                       "cache_read": 0.3, "cache_write": 3}
    seed = tmp_path / "seed"
    commit_db(seed, later, "main moves on")
    git(seed, "push", "origin", "main")

    single = tmp_path / "single"
    git(tmp_path, "clone", "--single-branch", "--branch", "main", str(remote), str(single))
    git(single, "config", "user.name", "Bot")
    git(single, "config", "user.email", "bot@example.invalid")
    tip = git(single, "rev-parse", "origin/main").stdout.strip()

    proposal = tmp_path / "proposal.json"
    proposed = copy.deepcopy(later)
    proposed["models"]["alpha"]["input"] = 1.2
    proposal.write_text(json.dumps(proposed))

    result = publish(single, proposal, "body", "title",
                     generated_at_sha=tip, run=stubbed_github)

    assert (result.ok, result.action) == (True, "opened"), result.detail
    published = json.loads(
        git(remote, "show", f"refs/heads/{BOT}:{TARGET_REL_PATH}").stdout
    )
    assert published["models"]["alpha"]["input"] == 1.2
    # The proposal was built on the new tip, so the human's model survives.
    assert "human-added" in published["models"]


def test_a_proposal_built_on_an_older_base_is_refused(remote, tmp_path):
    """Publishing a stale proposal would revert a merge, inside an allowed file."""
    work = tmp_path / "work"
    git(tmp_path, "clone", str(remote), str(work))
    git(work, "config", "user.name", "Bot")
    git(work, "config", "user.email", "bot@example.invalid")
    generated_at = git(work, "rev-parse", "origin/main").stdout.strip()

    newer = copy.deepcopy(BASE)
    newer["models"]["human-added"] = {"provider": "openai", "input": 3, "output": 6,
                                       "cache_read": 0.3, "cache_write": 3}
    git(work, "checkout", "main")
    commit_db(work, newer, "someone else's merge")
    git(work, "push", "origin", "main")

    # The proposal only knows about the older database.
    proposal = tmp_path / "proposal.json"
    stale = copy.deepcopy(BASE)
    stale["models"]["alpha"]["input"] = 1.2
    proposal.write_text(json.dumps(stale))

    result = publish(work, proposal, "body", "generated before the merge", "title",
                     generated_at_sha=generated_at, run=stubbed_github)

    assert result.ok is False
    assert "moved from" in result.detail
    bot = git(remote, "rev-parse", "--verify", f"refs/heads/{BOT}", check=False)
    assert bot.returncode != 0, "a refused publication must not create the bot branch"


def test_a_new_bot_branch_is_pushed_without_force(remote, tmp_path):
    work = tmp_path / "work"
    git(tmp_path, "clone", str(remote), str(work))
    git(work, "config", "user.name", "Bot")
    git(work, "config", "user.email", "bot@example.invalid")
    tip = git(work, "rev-parse", "origin/main").stdout.strip()

    proposal = tmp_path / "proposal.json"
    newer = copy.deepcopy(BASE)
    newer["models"]["alpha"]["input"] = 1.2
    proposal.write_text(json.dumps(newer))

    result = publish(work, proposal, "body", "title",
                     generated_at_sha=tip, run=stubbed_github)

    assert (result.ok, result.action) == (True, "opened")
    published = json.loads(
        git(remote, "show", f"refs/heads/{BOT}:{TARGET_REL_PATH}").stdout
    )
    assert published["models"]["alpha"]["input"] == 1.2


def test_a_second_run_refreshes_the_same_branch(remote, tmp_path):
    work = tmp_path / "work"
    git(tmp_path, "clone", str(remote), str(work))
    git(work, "config", "user.name", "Bot")
    git(work, "config", "user.email", "bot@example.invalid")

    global _OPEN_PR
    for value, expected in ((1.2, "opened"), (1.4, "updated")):
        # The first run opens the PR; the second finds it and refreshes it, which
        # is the path a scheduled rerun takes.
        _OPEN_PR = (
            '[{"number": 7, "url": "https://github.com/example/Tokdash/pull/7"}]'
            if expected == "updated" else ""
        )
        tip = git(work, "rev-parse", "origin/main").stdout.strip()
        proposal = tmp_path / "proposal.json"
        newer = copy.deepcopy(BASE)
        newer["models"]["alpha"]["input"] = value
        proposal.write_text(json.dumps(newer))
        result = publish(work, proposal, "body", "title",
                         generated_at_sha=tip, run=stubbed_github)
        assert (result.ok, result.action) == (True, expected), result.detail

    branches = sorted(git(remote, "for-each-ref", "--format=%(refname)",
                           "refs/heads").stdout.split())
    assert branches == [f"refs/heads/{BOT}", "refs/heads/main"]
    published = json.loads(
        git(remote, "show", f"refs/heads/{BOT}:{TARGET_REL_PATH}").stdout
    )
    assert published["models"]["alpha"]["input"] == 1.4
