"""Publish a validated pricing proposal to the tokdash repo.

Ordering is the design. Read the database from a freshly fetched ``main``,
generate and validate against that commit, and only then move a branch. The bot
owns exactly one branch and at most one open PR against it, so a rerun refreshes
a proposal instead of stacking a second one, and a proposal is never computed
against a database that has already moved on.

The bot never merges anything.
"""

from __future__ import annotations

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


def fetch_base(tokdash_repo: Path, base: str = "main", run=_default_run) -> str:
    """Fetch origin and return the SHA the proposal must be generated against."""
    proc = run(["git", "fetch", "--quiet", "origin", base], tokdash_repo)
    if proc.returncode != 0:
        raise RuntimeError(f"git fetch origin {base} failed: {proc.stderr.strip()}")
    proc = run(["git", "rev-parse", f"origin/{base}"], tokdash_repo)
    if proc.returncode != 0:
        raise RuntimeError(f"cannot resolve origin/{base}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def publish(
    tokdash_repo: Path,
    proposed_db_path: Path,
    body: str,
    title: str,
    branch: str = BOT_BRANCH,
    base: str = "main",
    labels: list[str] | None = None,
    dry_run: bool = False,
    base_sha: str | None = None,
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
        dry_run: print the plan, change nothing.
        base_sha: SHA already fetched, skips a second fetch when supplied.
        run: command runner, injectable for tests.
    """
    tokdash_repo = Path(tokdash_repo)
    target = tokdash_repo / TARGET_REL_PATH

    if branch == base:
        return PublishResult(
            ok=False, action="failed", branch=branch,
            detail=f"refusing to reset the base branch {base!r}",
        )

    try:
        sha = base_sha or fetch_base(tokdash_repo, base, run)
    except RuntimeError as exc:
        return PublishResult(ok=False, action="failed", branch=branch, detail=str(exc))

    checkout = run(["git", "checkout", "-B", branch, sha], tokdash_repo)
    if checkout.returncode != 0:
        return PublishResult(
            ok=False, action="failed", branch=branch,
            detail=f"checkout -B {branch} failed: {checkout.stderr.strip()}",
        )

    if dry_run:
        return PublishResult(
            ok=True, action="dry_run", branch=branch,
            detail=f"would write {TARGET_REL_PATH} from {sha[:7]} and push {branch}",
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
        return PublishResult(
            ok=True, action="no_change", branch=branch,
            detail="proposed database matches the current base; nothing to publish",
        )

    commit = run(["git", "commit", "-q", "-m", f"Pricing DB update ({branch})"], tokdash_repo)
    if commit.returncode != 0:
        return PublishResult(
            ok=False, action="failed", branch=branch,
            detail=f"commit failed: {commit.stderr.strip()}",
        )

    # Force is legitimate only because this branch is bot-owned, and
    # --force-with-lease keeps a surprise pusher from being overwritten.
    push = run(
        ["git", "push", "--force-with-lease", "origin", f"{branch}:{branch}"],
        tokdash_repo,
    )
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
        warnings = _apply_labels(slug, str(open_pr["number"]), labels, tokdash_repo, run)
        return PublishResult(
            ok=True, action="updated", branch=branch, pr_url=pr_url,
            detail=f"refreshed existing PR #{open_pr['number']}", warnings=warnings,
        )

    created = run(
        [
            "gh", "pr", "create",
            "--repo", slug,
            "--head", branch,
            "--base", base,
            "--title", title,
            "--body", body,
        ],
        tokdash_repo,
    )
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
            "--json", "number,url",
        ],
        cwd,
    )
    if proc.returncode != 0:
        return None
    import json

    try:
        found = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return found[0] if found else None


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
