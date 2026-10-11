"""Run tokdash's consumer checks, split into what a repricing is allowed to break.

The proposed database has to satisfy tokdash's own tests before it is published,
but those tests ask two different questions. Some ask whether a model or alias
resolves at all: repricing must never break one, and a failure there means the
database is malformed. Others assert that a named model costs an amount written
into the test file, and a price change is exactly what this bot is for, so
holding a real repricing hostage to those numbers means nothing can ever be
published (round-2 review, item 3: a guard-approved 10% move to one model failed
20 of 48 consumer tests and blocked publication).

Which test is which is not written down anywhere, and a hand-kept list rots the
first time tokdash adds a test. So the split is measured instead: run the suite
against the current database with every price multiplied by a probe factor, and
whatever fails there is, by construction, sensitive to the numbers rather than to
the shape. Failures that survive only the proposal are structural.

Outcomes are three, not two. Clean publishes as usual; expectation-only failures
publish as a draft with the failures in the body, so the human reviews the numbers
and the test update together; a structural failure blocks publication outright.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

DB_REL_PATH = Path("src") / "tokdash" / "pricing_db.json"
DEFAULT_CHECKS = (
    "tests/test_pricing_db_contract.py",
    "tests/test_model_name_normalization.py",
    "tests/test_pricing_normalization.py",
)
# Enough to move every pinned number off its literal, small enough that rounding
# cannot be mistaken for a price the test would still accept.
PROBE_FACTOR = 1.1
PRICE_FIELDS = ("input", "output", "cache_read", "cache_write")


@dataclass
class Report:
    """What the consumer checks said, split by whether a repricing may cause it."""

    ran: bool = False
    skipped_reason: str = ""
    error: str = ""
    probe_failures: set[str] = field(default_factory=set)
    proposal_failures: set[str] = field(default_factory=set)
    detail: str = ""

    @property
    def structural_failures(self) -> set[str]:
        """Failures the probe did not cause, so not explained by moving prices."""
        return self.proposal_failures - self.probe_failures

    @property
    def expectation_failures(self) -> set[str]:
        """Failures an ordinary repricing is expected to cause."""
        return self.proposal_failures & self.probe_failures

    @property
    def publishable(self) -> bool:
        return self.ran and not self.error and not self.structural_failures

    @property
    def needs_draft(self) -> bool:
        return self.publishable and bool(self.expectation_failures)

    def to_json(self) -> dict:
        return {
            "ran": self.ran,
            "skipped_reason": self.skipped_reason,
            "error": self.error,
            "structural_failures": sorted(self.structural_failures),
            "expectation_failures": sorted(self.expectation_failures),
            "publishable": self.publishable,
            "needs_draft": self.needs_draft,
            "detail": self.detail,
        }


def _run_pytest(tokdash_repo: Path, node_ids: list[str]) -> tuple[int, set[str], str]:
    """Run the given tests and return exit code, failing node ids, and tail output."""
    if not node_ids:
        return 0, set(), ""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
         *node_ids],
        cwd=str(tokdash_repo),
        capture_output=True,
        text=True,
        env=_test_env(tokdash_repo),
    )
    failed = {
        line.split(" ", 1)[1].split(" - ", 1)[0].strip()
        for line in proc.stdout.splitlines()
        if line.startswith("FAILED ")
    }
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-40:])
    return proc.returncode, failed, tail


def _test_env(tokdash_repo: Path) -> dict:
    """Inherit the environment, but import tokdash from this checkout.

    A CI runner installs tokdash editable from the same commit, so pointing
    PYTHONPATH at its src/ is belt and braces; a local run against a sibling
    checkout depends on it.
    """
    env = dict(os.environ)
    src = str(tokdash_repo / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{src}{os.pathsep}{existing}" if existing else src
    return env


def collect_node_ids(tokdash_repo: Path, checks: tuple[str, ...]) -> list[str]:
    """Every test id in the check files, read from pytest rather than a list."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "--collect-only", "-p",
         "no:cacheprovider", *checks],
        cwd=str(tokdash_repo),
        capture_output=True,
        text=True,
        env=_test_env(tokdash_repo),
    )
    return [
        line.strip()
        for line in proc.stdout.splitlines()
        if "::" in line and line.strip().startswith("tests/")
    ]


def scaled_db(db_text: str, factor: float = PROBE_FACTOR) -> str:
    """The same database with every price multiplied, shape untouched."""
    db = json.loads(db_text)
    for entry in db.get("models", {}).values():
        if not isinstance(entry, dict):
            continue
        for name in PRICE_FIELDS:
            value = entry.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                entry[name] = round(value * factor, 6)
    return json.dumps(db, indent=2) + "\n"


def classify(
    tokdash_repo: Path,
    proposed_db_path: Path,
    checks: tuple[str, ...] = DEFAULT_CHECKS,
    factor: float = PROBE_FACTOR,
) -> Report:
    """Split the proposal's consumer-check failures by their likely cause."""
    target = tokdash_repo / DB_REL_PATH
    if not proposed_db_path.exists():
        return Report(ran=False, skipped_reason="no proposed database to check")
    if not target.exists():
        return Report(ran=False,
                      skipped_reason=f"{DB_REL_PATH} is missing from the checkout")

    missing = [c for c in checks if not (tokdash_repo / c).exists()]
    if missing:
        # A renamed or deleted check file would otherwise collect nothing and
        # wave every proposal through, which is the failure this whole split is
        # meant to avoid.
        return Report(ran=False,
                      error="consumer check files are missing from the checkout: "
                            + ", ".join(missing))

    node_ids = collect_node_ids(tokdash_repo, checks)
    if not node_ids:
        return Report(ran=False,
                      error="the consumer check files collected no tests, so nothing "
                            "was actually validated")

    baseline = target.read_text()
    report = Report(ran=True)
    try:
        # Probe: same tests, same shape, different numbers. What fails here is
        # sensitive to prices, so it cannot indict a repricing on its own.
        target.write_text(scaled_db(baseline, factor))
        _, report.probe_failures, _ = _run_pytest(tokdash_repo, node_ids)

        target.write_text(proposed_db_path.read_text())
        _, report.proposal_failures, tail = _run_pytest(tokdash_repo, node_ids)
        report.detail = tail if report.proposal_failures else ""
    finally:
        # The checkout belongs to the next step.
        target.write_text(baseline)

    return report


def render_markdown(report: Report) -> str:
    """The PR body section, so a reviewer sees what a draft is waiting on."""
    if not report.ran:
        reason = report.error or report.skipped_reason or "not applicable"
        return f"## Consumer checks\n\nNot run: {reason}\n"
    lines = ["## Consumer checks", ""]
    if report.publishable and not report.expectation_failures:
        lines.append("tokdash's consumer checks pass against the proposed database.")
        return "\n".join(lines) + "\n"
    if report.expectation_failures:
        lines += [
            f"{len(report.expectation_failures)} consumer check(s) assert literal "
            "prices, and this change moves those numbers. The PR is a draft so the "
            "price change and the test update can be reviewed together.",
            "",
        ]
        lines += [f"- `{name}`" for name in sorted(report.expectation_failures)]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pricing_updater.consumer_checks",
        description="Split tokdash's consumer checks into structural and price-expectation "
                    "failures, so a repricing can be proposed as a draft instead of blocked.",
    )
    parser.add_argument("--tokdash-repo", required=True, type=Path)
    parser.add_argument("--proposed", required=True, type=Path,
                        help="The proposed pricing_db.json to validate")
    parser.add_argument("--json-out", type=Path, help="Write the machine-readable report")
    parser.add_argument("--md-out", type=Path, help="Write the PR body section")
    args = parser.parse_args(argv)

    report = classify(args.tokdash_repo, args.proposed)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report.to_json(), indent=2) + "\n")
    if args.md_out:
        args.md_out.parent.mkdir(parents=True, exist_ok=True)
        args.md_out.write_text(render_markdown(report) + "\n")

    if not report.ran:
        if report.error:
            print(f"::error::consumer checks could not run: {report.error}",
                  file=sys.stderr)
            return 1
        print(f"consumer checks skipped: {report.skipped_reason}")
        return 0
    if report.structural_failures:
        print("::error::structural consumer checks failed; the proposed database is "
              "malformed, not merely repriced: "
              + ", ".join(sorted(report.structural_failures)), file=sys.stderr)
        return 1
    print(f"consumer checks passed: {len(report.expectation_failures)} price-expectation "
          f"failure(s) routed to a draft")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
