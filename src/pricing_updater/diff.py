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
    change_type: str  # "new", "price_change", "stale"
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
        return f"  [???] {self.model_id}"


def compute_diff(
    current_models: dict[str, dict],
    proposed_models: dict[str, dict],
    manual_ids: set[str],
    derived_ids: set[str],
    last_seen: dict[str, dict],
    min_misses: int = 4,
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
    for k in sorted(current_keys - proposed_keys):
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


def generate_diff_report(changes: list[PricingChange], today: str = "") -> str:
    """Human-readable markdown diff report."""
    lines = ["# Pricing DB Update Proposal", ""]
    if today:
        lines.append(f"Generated: {today}")
        lines.append("")

    new = [c for c in changes if c.change_type == "new"]
    changed = [c for c in changes if c.change_type == "price_change"]
    stale = [c for c in changes if c.change_type == "stale"]

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
