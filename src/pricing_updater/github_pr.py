"""Create a PR against the tokdash repo via `gh` CLI.

Assumes `gh` is authenticated and the tokdash repo is at the given path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def create_pr(
    tokdash_repo: Path,
    proposed_db_path: Path,
    diff_report: str,
    branch_name: str,
    dry_run: bool = False,
) -> str | None:
    """Copy proposed pricing_db.json into tokdash and open a PR.

    Args:
        tokdash_repo: path to the tokdash git checkout.
        proposed_db_path: path to the generated pricing_db.json.
        diff_report: markdown report for the PR body.
        branch_name: git branch name, e.g. "pricing-update-2026-03-15".
        dry_run: if True, print commands instead of running them.

    Returns:
        PR URL on success, None on failure or dry_run.
    """
    target = tokdash_repo / "src" / "tokdash" / "pricing_db.json"

    cmds = [
        ["git", "-C", str(tokdash_repo), "checkout", "-b", branch_name],
        ["cp", str(proposed_db_path), str(target)],
        ["git", "-C", str(tokdash_repo), "add", str(target)],
        [
            "git", "-C", str(tokdash_repo), "commit", "-m",
            f"Auto-update pricing DB ({branch_name})",
        ],
        ["git", "-C", str(tokdash_repo), "push", "-u", "origin", branch_name],
    ]

    if dry_run:
        for cmd in cmds:
            print(f"  [dry-run] {' '.join(cmd)}")
        print(f"  [dry-run] gh pr create ...")
        return None

    for cmd in cmds:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"ERROR running {' '.join(cmd)}:")
            print(result.stderr)
            return None

    pr_result = subprocess.run(
        [
            "gh", "pr", "create",
            "--repo", str(tokdash_repo),
            "--title", f"Pricing DB update ({branch_name})",
            "--body", diff_report,
            "--label", "pricing,automated",
        ],
        capture_output=True,
        text=True,
        cwd=str(tokdash_repo),
    )
    if pr_result.returncode != 0:
        print(f"ERROR creating PR: {pr_result.stderr}")
        return None

    url = pr_result.stdout.strip()
    print(f"PR created: {url}")
    return url
