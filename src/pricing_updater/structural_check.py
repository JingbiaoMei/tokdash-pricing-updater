"""Decide whether a proposed database is well-formed, without asking the consumer.

Publication needs a gate that stands on its own. tokdash's checks were the gate,
but one test can assert both that an alias resolves and that a model costs 2.0, so
a failure in it says "something is wrong" and not what is wrong. Scaling every
price to sort failures into two buckets cannot fix that: it identifies which tests
are sensitive to numbers, which is not the same as attributing a cause. (Round-3
review, item 2: deleting a model outright was classified as a price expectation and
stayed publishable.)

So the gate here is what this repo can verify by itself, against the database the
proposal was built on: shape, per-entry fields, aliases that resolve, nothing
removed, no core price quietly becoming free. tokdash's suite still runs, but as a
reviewer rather than a judge, and its findings go to a draft.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

PRICE_FIELDS = ("input", "output", "cache_read", "cache_write")
CORE_PRICE_FIELDS = ("input", "output")
REQUIRED_ENTRY_FIELDS = ("provider",) + PRICE_FIELDS + ("unit",)
DB_REL_PATH = Path("src") / "tokdash" / "pricing_db.json"


def _is_number(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _entries(models: dict) -> dict:
    """Model entries only. Section comments are strings, not priced models."""
    return {k: v for k, v in models.items() if isinstance(v, dict)}


def check(proposed: dict, base: dict | None) -> list[str]:
    """Every way this proposal is malformed, as human-readable findings.

    Args:
        proposed: the database the bot wants to publish.
        base: the database it was computed against, or None to skip the
            comparisons that need one (used when validating a bare file).
    """
    findings: list[str] = []

    if not isinstance(proposed, dict):
        return ["the proposed database is not a JSON object"]
    models = proposed.get("models")
    if not isinstance(models, dict) or not models:
        return ["the proposed database has no models section"]
    aliases = proposed.get("aliases", {})
    if not isinstance(aliases, dict):
        findings.append("the aliases section is not a JSON object")
        aliases = {}

    entries = _entries(models)
    if not entries:
        findings.append("the models section holds no model entries")

    for model_id, entry in entries.items():
        for field in REQUIRED_ENTRY_FIELDS:
            if field not in entry:
                findings.append(f"{model_id}: missing {field!r}")
        if not str(entry.get("provider") or "").strip():
            findings.append(f"{model_id}: provider is empty")
        if not str(entry.get("unit") or "").strip():
            findings.append(f"{model_id}: unit is empty")
        for field in PRICE_FIELDS:
            if field not in entry:
                continue
            value = entry[field]
            if not _is_number(value):
                findings.append(f"{model_id}: {field} is not a finite number ({value!r})")
            elif value < 0:
                findings.append(f"{model_id}: {field} is negative ({value!r})")

    if base is None:
        # Validating a bare file: nothing to compare against, so every alias has
        # to resolve on its own merits.
        for alias, target in aliases.items():
            if not isinstance(target, str) or not target:
                findings.append(f"alias {alias!r} has no target")
            elif target not in entries:
                findings.append(
                    f"alias {alias!r} points at {target!r}, which is not a model")
    else:
        findings += _check_against_base(proposed, base, entries, aliases)

    return findings


def _check_against_base(proposed: dict, base: dict, entries: dict, aliases: dict) -> list[str]:
    """A proposal may add and reprice. It may not delete, or make a paid field free."""
    findings: list[str] = []
    base_models = _entries(base.get("models") or {})
    base_aliases = base.get("aliases") or {}

    removed = sorted(set(base_models) - set(entries))
    for model_id in removed[:10]:
        findings.append(f"{model_id} exists in the current database and not in the proposal")
    if len(removed) > 10:
        findings.append(f"...and {len(removed) - 10} more models removed")

    lost_aliases = sorted(set(base_aliases) - set(aliases))
    for alias in lost_aliases[:10]:
        findings.append(f"alias {alias!r} exists in the current database and not in the proposal")
    if len(lost_aliases) > 10:
        findings.append(f"...and {len(lost_aliases) - 10} more aliases removed")

    # An alias that used to resolve has to keep resolving, and a new one has to
    # resolve from the start. What the proposal inherited -- a target that never
    # resolved in the base either -- is not this proposal's doing, and blocking on
    # it is how a gate gets switched off.
    for alias, target in aliases.items():
        if not isinstance(target, str) or not target:
            findings.append(f"alias {alias!r} has no target")
        elif target in entries:
            continue
        elif target in base_models:
            findings.append(
                f"alias {alias!r} points at {target!r}, which this proposal removed")
        elif alias not in base_aliases:
            findings.append(
                f"alias {alias!r} is new and points at {target!r}, which is not a model")
        elif base_aliases.get(alias) in base_models:
            findings.append(
                f"alias {alias!r} used to resolve and now points at {target!r}, "
                "which is not a model")

    for model_id, entry in entries.items():
        previous = base_models.get(model_id)
        if not previous:
            continue
        for field in CORE_PRICE_FIELDS:
            was, now = previous.get(field), entry.get(field)
            if _is_number(was) and _is_number(now) and was > 0 and now == 0:
                findings.append(
                    f"{model_id}: {field} goes from {was} to 0, which makes a paid model free")
    return findings


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pricing_updater.structural_check",
        description="Verify a proposed pricing_db.json is well-formed and deletes nothing.",
    )
    parser.add_argument("--proposed", required=True, type=Path)
    parser.add_argument("--base", type=Path,
                        help=f"Current database (default: <tokdash-repo>/{DB_REL_PATH})")
    parser.add_argument("--tokdash-repo", type=Path, help="Where to find the current database")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)

    base_path = args.base or ((args.tokdash_repo / DB_REL_PATH) if args.tokdash_repo else None)
    try:
        proposed = load(args.proposed)
        base = load(base_path) if base_path else None
    except (OSError, json.JSONDecodeError) as exc:
        print(f"::error::cannot read the databases to compare: {exc}", file=sys.stderr)
        return 1

    findings = check(proposed, base)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(
            {"ok": not findings, "findings": findings}, indent=2) + "\n")

    if findings:
        listed = "; ".join(findings[:8])
        more = f" (and {len(findings) - 8} more)" if len(findings) > 8 else ""
        print(f"::error::the proposed database is malformed: {listed}{more}", file=sys.stderr)
        return 1
    print(f"structural check passed ({len(_entries(proposed.get('models') or {}))} models)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
