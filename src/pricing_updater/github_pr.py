"""Publish a validated pricing proposal to the tokdash repo.

Ordering is the design. Read the database from a freshly fetched ``main``,
generate and validate against that commit, and only then move a branch. The bot
owns exactly one branch and at most one open PR against it, so a rerun refreshes
a proposal instead of stacking a second one, and a proposal is never computed
against a database that has already moved on.

Three rules do the work:

* A dry run changes nothing at all, including the local branch head.
* A proposal is pinned to the base commit it was computed against, and is
  refused once that commit is no longer the tip of the base branch. Publishing
  anyway would overwrite edits made after the proposal was generated, and a
  changed-files check cannot see that because the stale content is inside the
  one file this bot is allowed to touch.
* The force push is leased against the remote bot branch as it was just
  observed, not against a tracking ref a fresh runner may never have had.

The bot never merges anything.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

TARGET_REL_PATH = Path("src") / "tokdash" / "pricing_db.json"
BOT_BRANCH = "bot/pricing-db"


@dataclass
class PublishResult:
    """Outcome of a publication attempt. ``ok`` is False only for real failures."""

    ok: bool
    action: str  # opened | updated | no_change | dry_run | failed
    branch: str = BOT_BRANCH
    pr_url: str = ""
    detail: str = ""
    warnings: list[str] = field(default_factory=list)


def _default_run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)


def slug_from_remote(tokdash_repo: Path, run=_default_run) -> str:
    """Derive ``owner/repo`` from origin, the only form ``gh --repo`` accepts."""
    proc = run(["git", "remote", "get-url", "origin"], tokdash_repo)
    if proc.returncode != 0:
        raise RuntimeError(f"cannot read origin URL: {proc.stderr.strip()}")
    url = proc.stdout.strip()
    if url.endswith(".git"):
        url = url[:-4]
    if url.startswith("git@"):
        prefix, _, tail = url.partition(":")
        return tail
    if "://" in url:
        return url.split("://", 1)[1].split("/", 1)[1]
    raise RuntimeError(f"unrecognized origin URL: {url!r}")


def fetch_ref(tokdash_repo: Path, branch: str, run=_default_run) -> str | None:
    """Fetch one branch and return its remote SHA, or None when it does not exist.

    The refspec is spelled out because a ``--single-branch`` clone, which is what
    a CI runner makes of itself, has no remote-tracking ref for any branch other
    than the one it cloned. Without one there is nothing to lease a force push
    against and the refresh of an existing bot branch fails with "stale info".
    """
    proc = run(
        ["git", "fetch", "--quiet", "origin",
         f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
        tokdash_repo,
    )
    if proc.returncode != 0:
        # Either the branch is absent or the fetch failed; both mean "no
        # observed remote value", and the caller decides which is fatal.
        return None
    proc = run(["git", "rev-parse", "--verify", f"refs/remotes/origin/{branch}"], tokdash_repo)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def fetch_base(tokdash_repo: Path, base: str = "main", run=_default_run) -> str:
    """Fetch origin and return the SHA the proposal must be generated against."""
    sha = fetch_ref(tokdash_repo, base, run)
    if not sha:
        raise RuntimeError(f"cannot resolve origin/{base}")
    return sha


def read_db_at(tokdash_repo: Path, sha: str, run=_default_run) -> dict:
    """Read the pricing DB as of ``sha``, so generation sees what is published."""
    proc = run(["git", "show", f"{sha}:{TARGET_REL_PATH}"], tokdash_repo)
    if proc.returncode != 0:
        raise RuntimeError(
            f"cannot read {TARGET_REL_PATH} at {sha[:7]}: {proc.stderr.strip()}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{TARGET_REL_PATH} at {sha[:7]} is not valid JSON: {exc}") from exc


def _load_proposal(path: Path) -> tuple[dict | None, str]:
    """Parse the proposed database before any repository is touched."""
    try:
        raw = Path(path).read_text()
    except OSError as exc:
        return None, f"cannot read the proposed database: {exc}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"the proposed database is not valid JSON: {exc}"
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
        return None, "the proposed database has no models section"
    return data, ""


def publish(
    tokdash_repo: Path,
    proposed_db_path: Path,
    body: str,
    title: str,
    branch: str = BOT_BRANCH,
    base: str = "main",
    labels: list[str] | None = None,
    dry_run: bool = False,
    generated_at_sha: str | None = None,
    draft: bool = False,
    run=_default_run,
) -> PublishResult:
    """Commit the proposed database onto the bot branch and open or refresh a PR.

    Args:
        tokdash_repo: path to the tokdash checkout.
        proposed_db_path: validated pricing_db.json to publish.
        body: PR body.
        title: PR title.
        branch: the bot-owned branch. Refuses to be the base branch.
        base: branch the PR targets.
        labels: applied best-effort after creation, never fatal.
        dry_run: report the plan and change nothing, not even the checked-out
            branch.
        generated_at_sha: the base commit the proposal was computed against.
            Required. Publishing a proposal that is not pinned to a commit is
            how a stale run overwrites work merged in the meantime.
        draft: open the PR as a draft, for a run a guard found suspicious.
        run: command runner, injectable for tests.
    """
    tokdash_repo = Path(tokdash_repo)
    target = tokdash_repo / TARGET_REL_PATH

    if branch == base:
        return PublishResult(
            ok=False, action="failed", branch=branch,
            detail=f"refusing to reset the base branch {base!r}",
        )
    if not generated_at_sha:
        return PublishResult(
            ok=False, action="failed", branch=branch,
            detail="refusing to publish a proposal that is not pinned to a base commit; "
                   "pass generated_at_sha from the same fetch the proposal was built on",
        )

    proposal, problem = _load_proposal(proposed_db_path)
    if problem:
        return PublishResult(ok=False, action="failed", branch=branch, detail=problem)

    try:
        remote_base = fetch_base(tokdash_repo, base, run)
    except RuntimeError as exc:
        return PublishResult(ok=False, action="failed", branch=branch, detail=str(exc))

    if remote_base != generated_at_sha:
        return PublishResult(
            ok=False, action="failed", branch=branch,
            detail=(
                f"{base} moved from {generated_at_sha[:7]} to {remote_base[:7]} while this "
                "proposal was being built; the diff was computed against the older database, "
                "so publishing it would overwrite newer edits. Regenerate against the new tip."
            ),
        )

    # Checked before the first write and before the branch move: a dry run that
    # resets HEAD is not a dry run.
    remote_branch = fetch_ref(tokdash_repo, branch, run)
    if dry_run:
        return PublishResult(
            ok=True, action="dry_run", branch=branch,
            detail=(
                f"would write {TARGET_REL_PATH} from {remote_base[:7]} and push "
                f"{branch} ({'refresh ' + remote_branch[:7] if remote_branch else 'new branch'})"
            ),
        )

    checkout = run(["git", "checkout", "-q", "-B", branch, remote_base], tokdash_repo)
    if checkout.returncode != 0:
        return PublishResult(
            ok=False, action="failed", branch=branch,
            detail=f"checkout -B {branch} failed: {checkout.stderr.strip()}",
        )

    shutil.copyfile(proposed_db_path, target)

    run(["git", "add", "--", str(TARGET_REL_PATH)], tokdash_repo)
    staged = run(["git", "diff", "--cached", "--name-only"], tokdash_repo)
    changed = [line for line in staged.stdout.splitlines() if line.strip()]

    # A pricing PR that touches anything else is a bug in the caller, and once
    # pushed it is somebody else's review problem.
    if set(changed) - {str(TARGET_REL_PATH)}:
        return PublishResult(
            ok=False, action="failed", branch=branch,
            detail="refusing to publish a diff wider than the pricing DB: " + ", ".join(changed),
        )
    if not changed:
        warnings = []
        if remote_branch and remote_branch != remote_base:
            warnings.append(
                f"{branch} still holds an older proposal at {remote_branch[:7]} while the "
                "current database needs no change; any open PR from it is now empty and "
                "should be closed."
            )
        return PublishResult(
            ok=True, action="no_change", branch=branch,
            detail="proposed database matches the current base; nothing to publish",
            warnings=warnings,
        )

    commit = run(["git", "commit", "-q", "-m", f"Pricing DB update ({branch})"], tokdash_repo)
    if commit.returncode != 0:
        return PublishResult(
            ok=False, action="failed", branch=branch,
            detail=f"commit failed: {commit.stderr.strip()}",
        )

    # Force is legitimate only because this branch is bot-owned. The lease names
    # the commit just fetched, so it holds on a runner that has never seen this
    # branch, and a surprise pusher is still refused.
    push_cmd = ["git", "push"]
    if remote_branch:
        push_cmd.append(f"--force-with-lease=refs/heads/{branch}:{remote_branch}")
    push_cmd += ["origin", f"{branch}:{branch}"]
    push = run(push_cmd, tokdash_repo)
    if push.returncode != 0:
        return PublishResult(
            ok=False, action="failed", branch=branch,
            detail=f"push failed: {push.stderr.strip()}",
        )

    try:
        slug = slug_from_remote(tokdash_repo, run)
    except RuntimeError as exc:
        return PublishResult(ok=False, action="failed", branch=branch, detail=str(exc))

    open_pr = _find_open_pr(slug, branch, tokdash_repo, run)

    if open_pr:
        pr_url = open_pr.get("url", "")
        edit = run(
            ["gh", "pr", "edit", str(open_pr["number"]), "--title", title, "--body", body],
            tokdash_repo,
        )
        if edit.returncode != 0:
            return PublishResult(
                ok=False, action="failed", branch=branch, pr_url=pr_url,
                detail=f"gh pr edit failed: {edit.stderr.strip()}",
            )
        warnings = _reconcile_draft(slug, open_pr, draft, tokdash_repo, run)
        warnings += _apply_labels(slug, str(open_pr["number"]), labels, tokdash_repo, run)
        return PublishResult(
            ok=True, action="updated", branch=branch, pr_url=pr_url,
            detail=f"refreshed existing PR #{open_pr['number']}", warnings=warnings,
        )

    create_cmd = [
        "gh", "pr", "create",
        "--repo", slug,
        "--head", branch,
        "--base", base,
        "--title", title,
        "--body", body,
    ]
    if draft:
        create_cmd.append("--draft")
    created = run(create_cmd, tokdash_repo)
    if created.returncode != 0:
        return PublishResult(
            ok=False, action="failed", branch=branch,
            detail=f"gh pr create failed: {created.stderr.strip()}",
        )

    pr_url = created.stdout.strip().splitlines()[-1] if created.stdout.strip() else ""
    number = pr_url.rsplit("/", 1)[-1] if pr_url else ""
    warnings = _apply_labels(slug, number, labels, tokdash_repo, run)
    return PublishResult(
        ok=True, action="opened", branch=branch, pr_url=pr_url, warnings=warnings,
    )


def _find_open_pr(slug: str, branch: str, cwd: Path, run) -> dict | None:
    proc = run(
        [
            "gh", "pr", "list",
            "--repo", slug,
            "--head", branch,
            "--state", "open",
            "--json", "number,url,isDraft",
        ],
        cwd,
    )
    if proc.returncode != 0:
        return None
    try:
        found = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return found[0] if found else None


def _reconcile_draft(slug, open_pr, draft, cwd, run) -> list[str]:
    """Match the PR's draft state to what this run's guards think.

    A draft that never leaves draft is a silent stall, so a clean rerun marks it
    ready. The reverse is impossible: gh has no way to put an open PR back into
    draft, so an anomalous rerun over a PR already marked ready says so in the
    warnings and leaves the judgement to the reviewer.
    """
    number = open_pr["number"]
    is_draft = bool(open_pr.get("isDraft"))
    if draft:
        if is_draft:
            return []
        return [
            f"this run tripped a structural guard, but PR #{number} is already open for "
            "review and cannot be put back into draft; check it before merging"
        ]
    if not is_draft:
        return []
    proc = run(["gh", "pr", "ready", str(number), "--repo", slug], cwd)
    if proc.returncode != 0:
        return [f"could not mark PR #{number} ready: {proc.stderr.strip()}"]
    return []


def _apply_labels(slug, number, labels, cwd, run) -> list[str]:
    """Best-effort labelling, so a missing label cannot orphan a pushed branch."""
    if not labels or not number:
        return []
    proc = run(
        ["gh", "pr", "edit", str(number), "--repo", slug, "--add-label", ",".join(labels)],
        cwd,
    )
    if proc.returncode != 0:
        return [f"could not apply labels {labels}: {proc.stderr.strip()}"]
    return []
