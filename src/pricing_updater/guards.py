"""Publication guards: what the bot may propose, and what it must hold back.

This vocabulary is deliberately separate from ``diff.PricingChange.classification``,
which groups changes for a human reader:

    proposable   computed from evidence, goes into the PR diff.
    held         computed but blocked by a guard; reported, never applied.

Manual and derived entries are protected upstream: ``diff.compute_diff`` never
emits a change for them, so they are absent from these lists entirely.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .canonicalize import CanonicalModel
from .diff import PricingChange

CORE_FIELDS = ("input", "output")


@dataclass
class GuardPolicy:
    """Thresholds from config/anomaly_policy.yaml."""

    max_changes_per_run: int = 25
    max_single_field_change_pct: float = 50.0
    min_openrouter_rows: int = 300
    hold_explicit_zero: bool = True

    @classmethod
    def load(cls, cfg: dict | None) -> "GuardPolicy":
        cfg = cfg or {}
        return cls(
            max_changes_per_run=int(cfg.get("max_changes_per_run", cls.max_changes_per_run)),
            max_single_field_change_pct=float(
                cfg.get("max_single_field_change_pct", cls.max_single_field_change_pct)
            ),
            min_openrouter_rows=int(cfg.get("min_openrouter_rows", cls.min_openrouter_rows)),
            hold_explicit_zero=bool(
                cfg.get("hold_explicit_zero", cls.hold_explicit_zero)
            ),
        )


@dataclass
class Decision:
    """One change plus the guard verdict and, when held, why."""

    change: PricingChange
    proposable: bool
    reasons: list[str] = field(default_factory=list)


def partition_unpriceable(
    models: list[CanonicalModel],
) -> tuple[list[CanonicalModel], list[dict]]:
    """Split models whose core prices are all present from those that are not.

    A model missing input or output must not be rendered with a zero, which is
    what a plain ``or Decimal("0")`` fallback would do. Dropping it silently
    would be just as bad, so the dropped rows come back for reporting.
    """
    usable: list[CanonicalModel] = []
    dropped: list[dict] = []

    for m in models:
        missing_core = sorted(set(m.missing_fields).intersection(CORE_FIELDS))
        if missing_core:
            dropped.append({
                "model_id": m.canonical_id,
                "provider": m.provider,
                "missing_fields": missing_core,
                "source": m.source_url or m.source_name,
                "reason": "source supplied no price for " + ", ".join(missing_core),
            })
        else:
            usable.append(m)

    return usable, dropped


def check_source_volume(stats: dict | None, policy: GuardPolicy) -> list[str]:
    """Run-level warnings for a source response too thin to trust."""
    warnings: list[str] = []
    if not stats:
        return warnings

    rows = stats.get("rows_seen")
    if rows is not None and rows < policy.min_openrouter_rows:
        warnings.append(
            f"OpenRouter returned {rows} rows, below min_openrouter_rows="
            f"{policy.min_openrouter_rows}; refusing to treat this as a pricing day"
        )
    return warnings


def classify(
    changes: list[PricingChange],
    policy: GuardPolicy,
) -> list[Decision]:
    """Decide which computed changes may enter the PR diff.

    Repricing an existing model is proposable: a human reviews it in the PR,
    which is the whole point of the bot. What stays held is anything a guard
    distrusts, and deletions, which are never automatic.
    """
    decisions: list[Decision] = []

    for change in changes:
        reasons = _hold_reasons(change, policy)
        decisions.append(Decision(change=change, proposable=not reasons, reasons=reasons))

    # Only source-derived changes count toward the volume gate. Alias additions
    # come from config/aliases.yaml, so their number says nothing about whether
    # a scraper broke.
    volumetric = [
        d for d in decisions
        if d.proposable and d.change.change_type in ("new", "price_change", "stale")
    ]
    proposable = volumetric
    if len(proposable) > policy.max_changes_per_run:
        volume_reason = (
            f"run proposes {len(proposable)} changes, above max_changes_per_run="
            f"{policy.max_changes_per_run}; a mass move is a scraper signal, not a pricing day"
        )
        for d in proposable:
            d.proposable = False
            d.reasons.append(volume_reason)

    return decisions


def _hold_reasons(change: PricingChange, policy: GuardPolicy) -> list[str]:
    reasons: list[str] = []

    # Deletions and staleness are manual-only by design, guard or no guard.
    if change.change_type == "stale":
        reasons.append("removals are never automatic")
        return reasons

    if change.change_type == "alias_change":
        reasons.append("alias retargeting changes model identity, needs a human")
        return reasons

    entry = change.new_entry or {}

    if change.change_type == "new" and policy.hold_explicit_zero:
        zeroed = [f for f in CORE_FIELDS if _is_zero(entry.get(f))]
        if zeroed:
            reasons.append(
                "new entry prices " + ", ".join(zeroed) + " at zero; confirm this is a real free tier"
            )

    if change.change_type == "price_change":
        for fld in change.fields_changed:
            old_v = _as_float((change.old_entry or {}).get(fld))
            new_v = _as_float(entry.get(fld))
            if new_v is not None and new_v <= 0:
                reasons.append(f"{fld} would drop to {new_v}; a zero price needs a human")
            elif old_v and new_v is not None:
                pct = abs(new_v - old_v) / abs(old_v) * 100.0
                if pct > policy.max_single_field_change_pct:
                    reasons.append(
                        f"{fld} moves {pct:.0f}%, above max_single_field_change_pct="
                        f"{policy.max_single_field_change_pct:g}%"
                    )

    return reasons


def _is_zero(value) -> bool:
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def proposable_changes(decisions: list[Decision]) -> list[PricingChange]:
    return [d.change for d in decisions if d.proposable]


def held_changes(decisions: list[Decision]) -> list[Decision]:
    return [d for d in decisions if not d.proposable]
