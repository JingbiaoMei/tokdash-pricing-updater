"""Run tokdash's consumer checks and report what they actually said.

Publication gates on structural_check, which decides from the two databases
alone whether a proposal is well-formed. This module is the second opinion: it
runs tokdash's own release-safe trio against the proposed database, because those
checks are the earliest sign that a repricing needs a test update in the same PR.
Its findings route the PR to draft. They no longer decide whether it exists.

They used to, via a split that could not work. The theory was to run the suite
twice, once with every price multiplied, and treat anything the multiplier broke
as a price expectation. But one test can assert both that an alias resolves and
that a model costs 2.0, so a failure there says "something is wrong" and never
what. Multiplying prices identifies tests that are sensitive to numbers, which is
not the same as attributing a cause. (Round-3 review, item 2: deleting a model
outright was sorted into "price expectation" and stayed publishable.)

What this module must never do is report success for a run that did not happen, so
every verdict comes from structured results rather than summary text: pytest's
exit code, and a JUnit XML record per test. Three holes that reading FAILED lines
left open are now closed and tested.

  * a fixture that errors makes the run red without emitting a FAILED line, and
    used to be counted as a pass;
  * a check file that fails to import aborts collection, so the run tests nothing
    and says so only on stderr;
  * a required check file that is empty collects nothing, exits zero, and leaves
    the whole suite green while validating nothing at all.

A test that fails or errors because of the proposal is a finding, not a broken
run: an assertion failure and a fixture that can no longer find its model both
mean a reviewer should look. A run is "could not run" when pytest itself refused
or stopped (exit codes 2 to 5), wrote no result file, produced records that
cannot be traced back to a collected test, or left a required file with no
records. It is also "could not run" when a test errors against the database as it
already is: a checkout whose fixtures cannot set up has not tested anything, and
reporting that as a clean bill of health is the same mistake in a different coat.
An assertion failure already red on the current database is different in kind,
and is recorded as pre-existing rather than blamed on the proposal.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
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

# pytest's documented exit codes. 0 and 1 both mean the tests ran; the rest mean
# it did not, and only the tests disagreeing with us is a finding.
EXIT_OK = 0
EXIT_TESTS_FAILED = 1
INCOMPLETE_EXITS = {
    2: "interrupted before the tests finished",
    3: "internal error",
    4: "usage error, usually a path pytest could not find",
    5: "collected no tests at all",
}


@dataclass
class Run:
    """One pytest execution, read from its exit code and its JUnit records."""

    exit_code: int = EXIT_OK
    passed: int = 0
    #: Failures and execution errors, as collected test ids. Both are findings.
    failures: set[str] = field(default_factory=set)
    errors: set[str] = field(default_factory=set)
    #: How many records each check file produced, so an empty file is visible.
    per_file: dict[str, int] = field(default_factory=dict)
    #: Why this run cannot be trusted, as opposed to what it found.
    problems: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def completed(self) -> bool:
        return not self.problems

    @property
    def findings(self) -> set[str]:
        return self.failures | self.errors


@dataclass
class Report:
    """What the consumer checks saw, with the judgement left to the reviewer."""

    ran: bool = False
    skipped_reason: str = ""
    error: str = ""
    #: Failures already there before the proposal was applied, so not its doing.
    base_failures: set[str] = field(default_factory=set)
    proposal_failures: set[str] = field(default_factory=set)
    #: Failures the price multiplier also caused: labels, not an explanation.
    price_sensitive: set[str] = field(default_factory=set)
    detail: str = ""

    @property
    def new_failures(self) -> set[str]:
        return self.proposal_failures - self.base_failures

    @property
    def likely_price_expectations(self) -> set[str]:
        return self.new_failures & self.price_sensitive

    @property
    def unexplained(self) -> set[str]:
        """New failures a price move did not cause, so read these first."""
        return self.new_failures - self.price_sensitive

    @property
    def publishable(self) -> bool:
        """Whether the checks ran at all. They are not the publication gate."""
        return self.ran and not self.error

    @property
    def needs_draft(self) -> bool:
        return self.publishable and bool(self.new_failures)

    def to_json(self) -> dict:
        return {
            "ran": self.ran,
            "skipped_reason": self.skipped_reason,
            "error": self.error,
            "preexisting_failures": sorted(self.base_failures),
            "new_failures": sorted(self.new_failures),
            "likely_price_expectations": sorted(self.likely_price_expectations),
            "needs_a_look": sorted(self.unexplained),
            "publishable": self.publishable,
            "needs_draft": self.needs_draft,
            "detail": self.detail,
        }


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


def _pytest(tokdash_repo: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args],
        cwd=str(tokdash_repo),
        capture_output=True,
        text=True,
        env=_test_env(tokdash_repo),
    )


def collect_node_ids(tokdash_repo: Path,
                     checks: tuple[str, ...]) -> dict[str, list[str]]:
    """Collected test ids per check file, read from pytest rather than a list.

    Keyed by file so a required file that collects nothing is distinguishable from
    one that collected and passed, which is the difference between a green run and
    a run that validated nothing.
    """
    proc = _pytest(tokdash_repo, ["-q", "--no-header", "--collect-only",
                                  "-p", "no:cacheprovider", *checks])
    collected: dict[str, list[str]] = {c: [] for c in checks}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if "::" not in line:
            continue
        file_part, _, name = line.partition("::")
        # pytest prints the collected path relative to its rootdir, which is the
        # repo most of the time and something further up when it is not. Keying on
        # the check path we asked for keeps the record count per file comparable.
        wanted = next((c for c in checks
                       if file_part == c or file_part.replace("\\", "/").endswith("/" + c)),
                      None)
        if wanted:
            collected[wanted].append(f"{wanted}::{name}")
    return collected


def _index(collected: dict[str, list[str]]) -> dict[tuple[str, str], str]:
    """Map a JUnit (classname, name) pair back to the test id it belongs to.

    The classname pytest writes is a dotted path, and how much of the directory
    prefix it carries depends on rootdir, so the check file's module name is the
    reliable part. An ambiguous or missing key is a problem, never a guess.
    """
    index: dict[tuple[str, str], str] = {}
    for node_id in {n for ids in collected.values() for n in ids}:
        file_part, _, name = node_id.partition("::")
        stem = Path(file_part).stem
        index[(stem, name)] = node_id
    return index


def _read_results(xml_path: Path, index: dict[tuple[str, str], str],
                  run: Run) -> None:
    """Fold one JUnit file into the run, recording problems as well as findings."""
    if not xml_path.exists():
        run.problems.append(f"pytest wrote no result file at {xml_path.name}")
        return
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError as exc:
        run.problems.append(f"the result file could not be parsed: {exc}")
        return

    for case in root.iter("testcase"):
        classname = case.get("classname") or ""
        name = case.get("name") or ""
        kind = next((child.tag for child in case
                     if child.tag in ("failure", "error", "skipped")), "passed")
        node_id = index.get((classname.rsplit(".", 1)[-1], name))
        if node_id is None:
            # Includes the record pytest writes for a file it could not import,
            # which has no classname at all and would otherwise vanish.
            run.problems.append(
                f"a result for {classname or '<no module>'}.{name} matches no "
                "collected test, so this run did not test what was asked")
            continue
        file_part = node_id.split("::", 1)[0]
        run.per_file[file_part] = run.per_file.get(file_part, 0) + 1
        if kind == "failure":
            run.failures.add(node_id)
        elif kind == "error":
            run.errors.add(node_id)
        elif kind == "passed":
            run.passed += 1


def _run_pytest(tokdash_repo: Path, checks: tuple[str, ...],
                collected: dict[str, list[str]], xml_path: Path) -> Run:
    """Run the check files and turn pytest's output into a Run."""
    xml_path.unlink(missing_ok=True)
    proc = _pytest(tokdash_repo, ["-q", "--no-header", "-p", "no:cacheprovider",
                                  "--junit-xml", str(xml_path), *checks])
    run = Run(exit_code=proc.returncode,
              detail="\n".join((proc.stdout + proc.stderr).splitlines()[-40:]))
    if proc.returncode in INCOMPLETE_EXITS:
        run.problems.append(f"pytest {INCOMPLETE_EXITS[proc.returncode]} "
                            f"(exit code {proc.returncode})")
    _read_results(xml_path, _index(collected), run)
    for file_part in collected:
        if not run.per_file.get(file_part):
            run.problems.append(f"{file_part} produced no test results, so it "
                                "validated nothing this run")
    return run


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
    """Run the consumer checks against the proposal and label what came back."""
    target = tokdash_repo / DB_REL_PATH
    if not proposed_db_path.exists():
        return Report(ran=False, skipped_reason="no proposed database to check")
    if not target.exists():
        return Report(ran=False,
                      skipped_reason=f"{DB_REL_PATH} is missing from the checkout")

    missing = [c for c in checks if not (tokdash_repo / c).exists()]
    if missing:
        # A renamed or deleted check file would otherwise collect nothing and wave
        # every proposal through, which is the failure this check exists to avoid.
        return Report(ran=False,
                      error="consumer check files are missing from the checkout: "
                            + ", ".join(missing))

    collected = collect_node_ids(tokdash_repo, checks)
    silent = sorted(c for c in checks if not collected.get(c))
    if silent:
        return Report(ran=False,
                      error="these consumer check files collected no tests, so nothing "
                            "would be validated: " + ", ".join(silent))

    baseline = target.read_text()
    report = Report(ran=True)
    runs: list[tuple[str, Run]] = []
    proposal: Run | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="pricing-consumer-checks-") as scratch:
            # Base first. A test already red on the current database is not the
            # proposal's doing, and blaming it would train everyone to ignore this.
            base = _run_pytest(tokdash_repo, checks, collected, Path(scratch) / "base.xml")
            runs.append(("the current database", base))
            report.base_failures = base.findings
            if base.errors:
                # Errors, not failures: an environment in which the checks cannot
                # even start is not a checkout that passed its checks.
                base.problems.append(
                    f"{len(base.errors)} of its tests errored against the database as it "
                    "already is, so this checkout could not be tested at all")

            # Probe: same shape, different numbers. What fails here is sensitive to
            # prices, which is a hint about a new failure and not an explanation.
            if base.completed:
                target.write_text(scaled_db(baseline, factor))
                probe = _run_pytest(tokdash_repo, checks, collected,
                                    Path(scratch) / "probe.xml")
                runs.append(("the price-multiplied database", probe))
                report.price_sensitive = probe.findings

            target.write_text(proposed_db_path.read_text())
            proposal = _run_pytest(tokdash_repo, checks, collected,
                                   Path(scratch) / "proposal.xml")
            runs.append(("the proposed database", proposal))
            report.proposal_failures = proposal.findings
    finally:
        # The checkout belongs to the next step.
        target.write_text(baseline)

    broken = [(label, run) for label, run in runs if not run.completed]
    if broken:
        label, run = broken[0]
        report.error = f"the consumer checks could not be trusted against {label}: " \
                       + "; ".join(run.problems)
        report.detail = run.detail
        return report

    if proposal is not None and report.new_failures:
        report.detail = proposal.detail
    return report


def render_markdown(report: Report) -> str:
    """The PR body section. It labels what came back and leaves the call away."""
    if not report.ran:
        reason = report.error or report.skipped_reason or "not applicable"
        return f"## Consumer checks\n\nNot run: {reason}\n"

    lines = ["## Consumer checks", ""]
    if not report.new_failures:
        lines.append("tokdash's consumer checks ran against the proposed database "
                     "and passed.")
        if report.base_failures:
            lines += ["", f"{len(report.base_failures)} of them were already failing "
                          "before this change, and are not counted against it."]
        return "\n".join(lines) + "\n"

    lines += [
        f"tokdash's consumer checks ran against the proposed database and "
        f"{len(report.new_failures)} of them failed. This PR is a draft: those "
        "checks assert literal prices, and this change moves prices. Read the "
        "failure list before marking it ready; a price change and the test update "
        "belong in the same review.",
        "",
    ]
    if report.unexplained:
        lines += [
            f"These {len(report.unexplained)} failure(s) did not fail when every "
            "price was multiplied, so a price change is not an obvious explanation. "
            "Read these first:",
            "",
        ]
        lines += [f"- `{name}`" for name in sorted(report.unexplained)]
    if report.likely_price_expectations:
        lines += [
            "",
            "These also failed with every price multiplied, so they are sensitive to "
            "the numbers rather than to the shape of the database:",
            "",
        ]
        lines += [f"- `{name}`" for name in sorted(report.likely_price_expectations)]
    if report.base_failures:
        lines += ["", f"{len(report.base_failures)} failure(s) were already failing "
                      "before this change and are not counted against it."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pricing_updater.consumer_checks",
        description="Run tokdash's consumer checks against a proposed pricing database "
                    "and report what failed, without judging whether a repricing is "
                    "allowed to cause it.",
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

    if report.error:
        # Either the checks could not run or could not be read. A proposal nobody
        # validated does not get to publish, so this is the one hard failure here.
        print(f"::error::consumer checks could not be trusted: {report.error}",
              file=sys.stderr)
        return 1
    if not report.ran:
        print(f"consumer checks skipped: {report.skipped_reason}")
        return 0
    print(f"consumer checks ran: {len(report.new_failures)} new failure(s), "
          f"{len(report.likely_price_expectations)} sensitive to prices, "
          f"{len(report.unexplained)} needing a look")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
