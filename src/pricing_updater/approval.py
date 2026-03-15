"""Interactive approval workflow.

Presents each PricingChange to the operator and collects accept/reject.
Changes classified as manual_only are shown but skipped by default.
"""

from __future__ import annotations

import sys

from .diff import PricingChange


def interactive_review(changes: list[PricingChange]) -> list[PricingChange]:
    """Walk through changes interactively, return approved subset.

    Classification guide:
        safe_add        → auto-accepted (Y to confirm, n to reject)
        review_required → must explicitly accept
        manual_only     → shown for awareness, skipped unless forced
    """
    if not changes:
        print("No changes to review.")
        return []

    approved: list[PricingChange] = []
    skip_rest = False

    for i, change in enumerate(changes, 1):
        if skip_rest:
            break

        prefix = f"[{i}/{len(changes)}]"
        print(f"\n{prefix} {change.summary_line()}")

        if change.classification == "manual_only":
            print("  (manual_only — skipped. Edit config files to handle this.)")
            continue

        default = "Y" if change.classification == "safe_add" else "?"
        prompt = f"  Accept? [Y/n/s(kip rest)/q(uit)] (default={default}): "

        try:
            choice = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return approved

        if choice in ("q", "quit"):
            print("Quitting review.")
            break
        elif choice in ("s", "skip"):
            skip_rest = True
            # Accept the current one if it's safe_add.
            if change.classification == "safe_add" and choice == "s":
                approved.append(change)
            break
        elif choice in ("n", "no"):
            continue
        else:
            # Empty or "y" → accept.
            approved.append(change)

    print(f"\nApproved {len(approved)} of {len(changes)} changes.")
    return approved


def auto_review(changes: list[PricingChange]) -> list[PricingChange]:
    """Non-interactive review for CI: accept safe_add only.

    review_required and manual_only changes are included in the PR body
    but NOT applied — they require human review in the PR.
    """
    approved = [c for c in changes if c.classification == "safe_add"]
    if approved:
        print(f"Auto-approved {len(approved)} safe_add changes.")
    flagged = [c for c in changes if c.classification != "safe_add"]
    if flagged:
        print(f"Flagged {len(flagged)} changes for manual review in PR body.")
    return approved
