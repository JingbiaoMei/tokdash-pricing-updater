"""Report new model additions, held findings and preserved existing entries.

Discovery and source-health evidence remain useful for manual maintenance, but
only new model entries count as movable by the additions-only bot.
"""

from __future__ import annotations

import json
from pathlib import Path

from .diff import PricingChange
from .guards import Decision


def build_coverage(
    current_models: dict,
    proposed_models: dict,
    decisions: list[Decision],
    dropped: list[dict],
    manual_ids: set[str],
    derived_ids: set[str],
) -> dict:
    """One row per pricing_db model key, plus summary counts."""
    current_keys = {k for k, v in current_models.items() if isinstance(v, dict)}
    proposed_keys = {k for k, v in proposed_models.items() if isinstance(v, dict)}

    decision_by_id = {d.change.model_id: d for d in decisions}
    dropped_by_id = {row["model_id"]: row for row in dropped}

    rows: dict[str, dict] = {}
    for key in sorted(current_keys | proposed_keys):
        if key in manual_ids:
            state, why = "protected_manual", "manual_models.yaml entry"
        elif key in derived_ids:
            state, why = "protected_derived", "derived_models.yaml entry"
        else:
            decision = decision_by_id.get(key)
            if decision is not None:
                if decision.proposable:
                    state = "reprice_proposed" if decision.change.change_type == "price_change" else "add_proposed"
                    why = ""
                else:
                    state = "held"
                    why = "; ".join(decision.reasons)
            elif key in dropped_by_id:
                state = "unpriceable"
                why = dropped_by_id[key]["reason"]
            elif key in proposed_keys and key in current_keys:
                state = "unchanged"
                why = ""
            elif key in proposed_keys:
                state = "proposed_not_diffed"
                why = ""
            else:
                state = "not_discovered"
                why = "present in the DB but not produced by any source, so no proposal can move it"

        rows[key] = {
            "state": state,
            "provider": _provider_of(current_models.get(key) or proposed_models.get(key)),
            "detail": why,
        }

    summary: dict[str, int] = {}
    for row in rows.values():
        summary[row["state"]] = summary.get(row["state"], 0) + 1
    summary["total_keys"] = len(rows)

    movable = sum(1 for key, row in rows.items()
                  if key not in current_keys and row["state"] == "add_proposed")
    summary["movable_by_bot"] = movable
    summary["existing_preserved"] = len(current_keys)
    summary["frozen"] = sum(
        1 for key, row in rows.items()
        if key in current_keys or row["state"] in
        {"protected_manual", "protected_derived", "unpriceable", "not_discovered"}
    )

    return {"summary": summary, "models": rows}


def write_coverage(path: Path, coverage: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(coverage, indent=2, sort_keys=True) + "\n")
    return path


def summarize(coverage: dict) -> str:
    """One line for the console and the run summary."""
    s = coverage["summary"]
    return (
        f"coverage: {s.get('total_keys', 0)} keys, "
        f"{s.get('movable_by_bot', 0)} new model additions, "
        f"{s.get('existing_preserved', 0)} existing models preserved, "
        f"{s.get('held', 0)} held"
    )


def _provider_of(entry) -> str:
    if isinstance(entry, dict):
        return str(entry.get("provider", ""))
    return ""
