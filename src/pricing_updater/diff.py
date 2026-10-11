"""Diff engine: compare proposed pricing_db against current.

Classifies every change as one of:
  - safe_add:        new model from a tracked provider, no conflicts.
  - review_required: price changed on an existing model.
  - manual_only:     deletion (only after repeated last_seen misses).

Deletions are NEVER automatic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


@dataclass
class PricingChange:
    model_id: str
    change_type: str  # "new", "price_change", "stale", "alias_add", "alias_change"
    classification: str  # "safe_add", "review_required", "manual_only"
    old_entry: dict | None = None
    new_entry: dict | None = None
    fields_changed: list[str] = field(default_factory=list)
    consecutive_misses: int = 0

    def summary_line(self) -> str:
        tag = {"safe_add": "ADD ", "review_required": "CHG!", "manual_only": "STAL"}
        t = tag.get(self.classification, "????")
        if self.change_type == "new":
            p = self.new_entry or {}
            return (
                f"  [{t}] {self.model_id} ({p.get('provider', '?')}): "
                f"${p.get('input', '?')}/{p.get('output', '?')} per MTok"
            )
        elif self.change_type == "price_change":
            parts = [f"  [{t}] {self.model_id}:"]
            for f in self.fields_changed:
                ov = (self.old_entry or {}).get(f, "?")
                nv = (self.new_entry or {}).get(f, "?")
                parts.append(f"         {f}: ${ov} -> ${nv}")
            return "\n".join(parts)
        elif self.change_type == "stale":
            return (
                f"  [{t}] {self.model_id}: not seen for "
                f"{self.consecutive_misses} consecutive scrapes"
            )
        elif self.change_type == "alias_add":
            target = (self.new_entry or {}).get("target", "?")
            return f"  [{t}] alias {self.model_id} -> {target}"
        elif self.change_type == "alias_change":
            old = (self.old_entry or {}).get("target", "?")
            new = (self.new_entry or {}).get("target", "?")
            return f"  [{t}] alias {self.model_id}: {old} -> {new}"
        return f"  [???] {self.model_id}"


def compute_diff(
    current_models: dict[str, dict],
    proposed_models: dict[str, dict],
    manual_ids: set[str],
    derived_ids: set[str],
    last_seen: dict[str, dict],
    min_misses: int = 4,
    enable_stale: bool = True,
    today: str | None = None,
) -> tuple[list[PricingChange], dict[str, dict]]:
    """Compare current pricing_db models against proposed.

    Args:
        current_models: current pricing_db.json["models"] (includes comments).
        proposed_models: the new models dict from the renderer.
        manual_ids: model IDs from manual_models.yaml (never flag these).
        derived_ids: model IDs from derived_models.yaml (never flag these).
        last_seen: state/last_seen.json content.
        min_misses: consecutive scrape misses before proposing staleness.
        enable_stale: when False, skip stale detection entirely. pricing_db.json
            is a runtime compatibility layer — legacy keys are kept forever, so
            staleness has no actionable output and is just noise.
        today: override for today's date (ISO format), for testing.

    Returns:
        (list of changes, updated last_seen dict).
    """
    today_str = today or date.today().isoformat()
    changes: list[PricingChange] = []
    updated_last_seen = dict(last_seen)

    # Sets of real model keys (skip _comment entries).
    current_keys = {k for k, v in current_models.items() if isinstance(v, dict)}
    proposed_keys = {k for k, v in proposed_models.items() if isinstance(v, dict)}
    protected = manual_ids | derived_ids

    # --- New models ---
    for k in sorted(proposed_keys - current_keys):
        if k in protected:
            continue
        changes.append(
            PricingChange(
                model_id=k,
                change_type="new",
                classification="safe_add",
                new_entry=proposed_models[k],
            )
        )
        updated_last_seen[k] = {
            "last_seen": today_str,
            "first_seen": today_str,
            "consecutive_misses": 0,
        }

    # --- Price changes on existing models ---
    for k in sorted(current_keys & proposed_keys):
        if k in protected:
            continue
        old = current_models[k]
        new = proposed_models[k]
        changed_fields: list[str] = []
        for fld in ("input", "output", "cache_read", "cache_write"):
            ov = float(old.get(fld, 0))
            nv = float(new.get(fld, 0))
            if abs(ov - nv) > 0.0005:
                changed_fields.append(fld)
        if changed_fields:
            changes.append(
                PricingChange(
                    model_id=k,
                    change_type="price_change",
                    classification="review_required",
                    old_entry=old,
                    new_entry=new,
                    fields_changed=changed_fields,
                )
            )
        # Update last_seen regardless of price change.
        entry = updated_last_seen.get(k, {})
        entry["last_seen"] = today_str
        entry.setdefault("first_seen", today_str)
        entry["consecutive_misses"] = 0
        updated_last_seen[k] = entry

    # --- Stale models (in current but not in proposed, and not protected) ---
    # Skipped entirely when stale detection is disabled: the DB is a runtime
    # compatibility layer and legacy keys are never removed.
    for k in (sorted(current_keys - proposed_keys) if enable_stale else []):
        if k in protected:
            continue
        entry = updated_last_seen.get(k, {})
        misses = entry.get("consecutive_misses", 0) + 1
        entry["consecutive_misses"] = misses
        entry.setdefault("first_seen", today_str)
        # Don't update last_seen — it stays at the last time we saw it.
        updated_last_seen[k] = entry

        if misses >= min_misses:
            changes.append(
                PricingChange(
                    model_id=k,
                    change_type="stale",
                    classification="manual_only",
                    old_entry=current_models[k],
                    consecutive_misses=misses,
                )
            )

    return changes, updated_last_seen


def compute_alias_diff(
    current_aliases: dict[str, str],
    proposed_aliases: dict[str, str],
) -> list[PricingChange]:
    """Compare alias mappings from current and proposed pricing DBs."""
    changes: list[PricingChange] = []
    for alias in sorted(set(proposed_aliases) - set(current_aliases)):
        changes.append(
            PricingChange(
                model_id=alias,
                change_type="alias_add",
                classification="safe_add",
                new_entry={"target": proposed_aliases[alias]},
            )
        )
    for alias in sorted(set(current_aliases) & set(proposed_aliases)):
        old_target = current_aliases[alias]
        new_target = proposed_aliases[alias]
        if old_target != new_target:
            changes.append(
                PricingChange(
                    model_id=alias,
                    change_type="alias_change",
                    classification="review_required",
                    old_entry={"target": old_target},
                    new_entry={"target": new_target},
                    fields_changed=["target"],
                )
            )
    return changes


def generate_diff_report(changes: list[PricingChange], today: str = "") -> str:
    """Human-readable markdown diff report."""
    lines = ["# Pricing DB Update Proposal", ""]
    if today:
        lines.append(f"Generated: {today}")
        lines.append("")

    new = [c for c in changes if c.change_type == "new"]
    changed = [c for c in changes if c.change_type == "price_change"]
    stale = [c for c in changes if c.change_type == "stale"]
    aliases = [c for c in changes if c.change_type.startswith("alias_")]

    if new:
        lines.append(f"## New Models ({len(new)})")
        for c in new:
            lines.append(c.summary_line())
        lines.append("")

    if changed:
        lines.append(f"## Price Changes ({len(changed)})")
        for c in changed:
            lines.append(c.summary_line())
        lines.append("")

    if stale:
        lines.append(f"## Stale Models ({len(stale)}) — manual_only")
        lines.append("*(Not seen on any source for multiple consecutive scrapes.)*")
        for c in stale:
            lines.append(c.summary_line())
        lines.append("")

    if aliases:
        lines.append(f"## Aliases ({len(aliases)})")
        for c in aliases:
            lines.append(c.summary_line())
        lines.append("")

    if not changes:
        lines.append("No changes detected. Pricing DB is up to date.")

    return "\n".join(lines)


def load_last_seen(path: Path) -> dict[str, dict]:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_last_seen(path: Path, data: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def generate_pr_body(
    decisions,
    meta: dict | None = None,
    max_rows: int = 60,
    max_chars: int = 40000,
) -> str:
    """Shape the PR body so reviewing it is a two-minute read.

    Args:
        decisions: guards.Decision list, duck-typed here to keep the module
            free of an import cycle. Each row exposes .change, .proposable and
            .reasons.
        meta: run context (generated, base_sha, run_url, source_line,
            coverage_line, stats).
        max_rows: rows per table before truncating to the artifact.
        max_chars: hard ceiling, well inside GitHub's 65,536 character body cap.
    """
    meta = meta or {}
    provenance = meta.get("provenance") or {}
    proposable = [d for d in decisions if d.proposable]
    held = [d for d in decisions if not d.proposable]

    additions = [d.change for d in proposable if d.change.change_type in ("new", "alias_add")]
    repricing = [d.change for d in proposable if d.change.change_type == "price_change"]

    lines: list[str] = ["## Pricing DB update", ""]

    context = []
    if meta.get("generated"):
        context.append(f"Generated {meta['generated']}")
    if meta.get("base_sha"):
        context.append(f"against tokdash `main` at `{str(meta['base_sha'])[:7]}`")
    if meta.get("run_url"):
        context.append(f"[run]({meta['run_url']})")
    if context:
        lines += [" ".join(context), ""]

    if meta.get("source_line"):
        lines += [meta["source_line"], ""]
    if meta.get("coverage_line"):
        lines += [meta["coverage_line"], ""]

    lines += [
        f"**In this diff:** {_count(len(additions), 'addition')}, "
        f"{_count(len(repricing), 'repricing')}. **Held back:** {len(held)}.",
        "",
    ]

    if additions:
        lines += [
            f"### Additions ({len(additions)})",
            "",
            "| Model | Provider | In / Out per MTok | Source |",
            "|---|---|---|---|",
        ]
        lines += _rows(additions, _addition_row, max_rows)
        lines.append("")

    if repricing:
        lines += [
            f"### Repricing ({len(repricing)})",
            "",
            "| Model | Field | Old | New | Source |",
            "|---|---|---|---|---|",
        ]
        lines += _rows(repricing, lambda c: _repricing_rows(c, provenance), max_rows)
        lines.append("")

    if held:
        lines += [
            f"### Held back, not in this diff ({len(held)})",
            "",
            "| Model | Change | Why held |",
            "|---|---|---|",
        ]
        lines += _rows(held, _held_row, max_rows)
        lines.append("")

    lines += [
        "### Scope",
        "",
        "This PR changes `src/tokdash/pricing_db.json` and nothing else.",
        "Merging is a human call; the bot never merges.",
        "",
    ]

    body = "\n".join(lines)
    if len(body) > max_chars:
        body = body[:max_chars] + "\n\n... truncated; full detail is in the run artifacts.\n"
    return body


def _rows(items, render_row, max_rows: int) -> list[str]:
    rows: list[str] = []
    for item in items[:max_rows]:
        rows.extend(render_row(item))
    remaining = len(items) - max_rows
    if remaining > 0:
        rows.append(f"| ... | and {remaining} more | see run artifacts |")
    return rows


def _addition_row(change) -> list[str]:
    entry = change.new_entry or {}
    if change.change_type == "alias_add":
        return [f"| `{change.model_id}` | alias | -> `{entry.get('target', '?')}` | aliases |"]
    return [
        f"| `{change.model_id}` | {entry.get('provider', '?')} "
        f"| {entry.get('input', '?')} / {entry.get('output', '?')} "
        f"| {entry.get('source', '')} |"
    ]


def _repricing_rows(change, provenance: dict | None = None) -> list[str]:
    entry = change.new_entry or {}
    provenance = provenance or {}
    sources = (provenance.get(change.model_id) or {}).get("field_sources", {})
    rows = []
    for fld in change.fields_changed:
        old_v = (change.old_entry or {}).get(fld, "?")
        new_v = entry.get(fld, "?")
        # A cache rate that moved only because the input rate moved is arithmetic,
        # not a published change, and a reviewer should be able to see that.
        label = fld + " (derived)" if sources.get(fld) == "derived_from_input" else fld
        rows.append(
            f"| `{change.model_id}` | {label} | {old_v} | {new_v} | {entry.get('source', '')} |"
        )
    return rows or [f"| `{change.model_id}` | - | - | - | |"]


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _held_row(decision) -> list[str]:
    change = decision.change
    reason = "; ".join(decision.reasons) or "held"
    return [f"| `{change.model_id}` | {change.change_type} | {reason} |"]
