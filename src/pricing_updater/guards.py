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
from .sources.base import PRICE_ATTRS, clean_price

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


def invalid_prices(model: CanonicalModel) -> list[str]:
    """Fields holding a value that cannot be published.

    A None, NaN, infinite or negative rate. These are distinct from a *missing*
    field: the source said something, and what it said cannot be a price, so a
    guard that only asks "is this field missing?" would wave it through.
    """
    bad: list[str] = []
    for name, attr in PRICE_ATTRS:
        if clean_price(getattr(model, attr, None)) is None:
            bad.append(name)
    return bad


def partition_unpriceable(
    models: list[CanonicalModel],
) -> tuple[list[CanonicalModel], list[dict]]:
    """Split publishable models from the ones that must not be rendered.

    Two separate reasons a model is held out. A missing core price must not be
    rendered with a zero, which is what a plain ``or Decimal("0")`` fallback
    would do; and a value that is negative or non-finite must not be rendered at
    all. Dropping either silently would be as bad as publishing it, so the rows
    come back for reporting.
    """
    usable: list[CanonicalModel] = []
    dropped: list[dict] = []

    for m in models:
        bad = invalid_prices(m)
        if bad:
            dropped.append({
                "model_id": m.canonical_id,
                "provider": m.provider,
                "missing_fields": [],
                "invalid_fields": bad,
                "source": m.source_url or m.source_name,
                "reason": "source published an unusable value for " + ", ".join(bad),
            })
            continue

        missing_core = sorted(set(m.missing_fields).intersection(CORE_FIELDS))
        if missing_core:
            # "No price" and "a price that cannot be a price" send a reviewer to
            # different places, so say which one happened to each field.
            broken = sorted(set(m.invalid_fields).intersection(missing_core))
            absent = [f for f in missing_core if f not in broken]
            parts = []
            if broken:
                parts.append("published an unusable value for " + ", ".join(broken))
            if absent:
                parts.append("supplied no price for " + ", ".join(absent))
            reason = "source " + "; ".join(parts)
            dropped.append({
                "model_id": m.canonical_id,
                "provider": m.provider,
                "missing_fields": missing_core,
                "invalid_fields": broken,
                "source": m.source_url or m.source_name,
                "reason": reason,
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


# Guard reasons that say something about the run rather than one price.
RUN_LEVEL_MARKERS = ("max_changes_per_run", "min_openrouter_rows")
# The guard that fires when a source's numbers move further than a price should.
MAGNITUDE_MARKER = "max_single_field_change_pct"


def should_draft(decisions: list[Decision]) -> bool:
    """True when the run as a whole looks suspect, not when one price looks odd.

    One new entry at zero, or one field that moved a long way, is an ordinary
    review question and belongs in an ordinary PR. A volume breach, a thin source
    response, or a run where most of the computed changes tripped the magnitude
    guard means the source or a scraper changed shape; that PR should not sit
    there looking merge-ready, so it opens as a draft.
    """
    held = [d for d in decisions if not d.proposable]
    if not held or not decisions:
        return False

    if any(
        any(marker in reason for marker in RUN_LEVEL_MARKERS)
        for d in held for reason in d.reasons
    ):
        return True

    magnitude = [d for d in held if any(MAGNITUDE_MARKER in r for r in d.reasons)]
    return len(magnitude) >= max(2, len(decisions) // 2)


def proposable_changes(decisions: list[Decision]) -> list[PricingChange]:
    return [d.change for d in decisions if d.proposable]


def held_changes(decisions: list[Decision]) -> list[Decision]:
    return [d for d in decisions if not d.proposable]
