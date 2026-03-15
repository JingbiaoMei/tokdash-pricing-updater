"""Render the final pricing_db.json for tokdash.

Combines scraped canonical models, manual models, derived models, and
aliases into a single JSON file matching tokdash's expected schema.
The renderer preserves section-comment keys and provider ordering.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from datetime import datetime, timezone
from decimal import Decimal

from .canonicalize import CanonicalModel


def _decimal_to_float(d: Decimal) -> float:
    """Convert Decimal to float, stripping needless trailing zeros."""
    return float(d)


def _model_to_entry(m: CanonicalModel) -> dict:
    entry: dict = OrderedDict()
    entry["provider"] = m.provider
    entry["input"] = _decimal_to_float(m.input_per_mtok)
    entry["output"] = _decimal_to_float(m.output_per_mtok)
    entry["cache_read"] = _decimal_to_float(m.cache_read_per_mtok)
    entry["cache_write"] = _decimal_to_float(m.cache_write_per_mtok)
    entry["unit"] = m.unit
    if m.source_url:
        entry["source"] = m.source_url
    if m.note:
        entry["note"] = m.note
    return entry


def _manual_entry(cfg: dict) -> dict:
    """Convert a manual_models.yaml entry into a pricing_db entry."""
    entry: dict = OrderedDict()
    entry["provider"] = cfg["provider"]
    entry["input"] = cfg["input"]
    entry["output"] = cfg["output"]
    entry["cache_read"] = cfg.get("cache_read", cfg["input"] * 0.1)
    entry["cache_write"] = cfg.get("cache_write", cfg["input"])
    entry["unit"] = cfg.get("unit", "per_million_tokens")
    if "source" in cfg:
        entry["source"] = cfg["source"]
    if "note" in cfg:
        entry["note"] = cfg["note"]
    return entry


def render(
    canonical_models: list[CanonicalModel],
    manual_models: dict[str, dict],
    derived_models: dict[str, dict],
    aliases: dict[str, str],
    provider_order: list[dict],
    current_version: str,
    db_note: str = "",
) -> dict:
    """Produce a complete pricing_db.json dict.

    Args:
        canonical_models: scraped + canonicalized + policy-applied models.
        manual_models: from config/manual_models.yaml.
        derived_models: from config/derived_models.yaml (after derivation resolved).
        aliases: from config/aliases.yaml.
        provider_order: section ordering from pricing_policy.yaml.
        current_version: current version string to bump (e.g. "2.0.1").
        db_note: the top-level "note" field.
    """
    # Index canonical models by ID for fast lookup.
    canonical_by_id: dict[str, CanonicalModel] = {}
    for m in canonical_models:
        # If duplicate IDs, last one wins (shouldn't happen with clean data).
        canonical_by_id[m.canonical_id] = m

    # Bump patch version.
    parts = current_version.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    new_version = ".".join(parts)

    result = OrderedDict()
    result["version"] = new_version
    result["lastUpdated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if db_note:
        result["note"] = db_note
    result["aliases"] = OrderedDict(sorted(aliases.items()))

    models_section: dict = OrderedDict()

    # Emit models grouped by provider_order sections.
    for section in provider_order:
        comment_key = section["key"]
        label = section["label"]
        providers_in_section = set(section.get("providers", []))

        models_section[comment_key] = label

        # Add canonical (scraped) models for this section's providers.
        section_models = sorted(
            (m for m in canonical_by_id.values() if m.provider in providers_in_section),
            key=lambda m: m.canonical_id,
        )
        for m in section_models:
            models_section[m.canonical_id] = _model_to_entry(m)

        # Add manual models whose provider matches this section.
        for mid, mcfg in sorted(manual_models.items()):
            if mcfg.get("provider") in providers_in_section and mid not in models_section:
                models_section[mid] = _manual_entry(mcfg)

    # Derived models go into the antigravity section (last in provider_order).
    for mid, entry in sorted(derived_models.items()):
        if mid not in models_section:
            models_section[mid] = entry

    result["models"] = models_section
    return result


def resolve_derived_models(
    derived_cfg: dict[str, dict],
    canonical_by_id: dict[str, CanonicalModel],
    manual_models: dict[str, dict],
) -> dict[str, dict]:
    """Resolve derived_models.yaml: copy pricing from derive_from target.

    Returns dict of {model_id: pricing_db_entry}.
    """
    resolved: dict[str, dict] = {}

    for model_id, cfg in derived_cfg.items():
        derive_from = cfg["derive_from"]
        provider = cfg.get("provider", "")
        note = cfg.get("note", "")

        # Look up the base model in canonical models first, then manual.
        base = canonical_by_id.get(derive_from)
        if base is not None:
            entry: dict = OrderedDict()
            entry["provider"] = provider
            entry["input"] = _decimal_to_float(base.input_per_mtok)
            entry["output"] = _decimal_to_float(base.output_per_mtok)
            entry["cache_read"] = _decimal_to_float(base.cache_read_per_mtok)
            entry["cache_write"] = _decimal_to_float(base.cache_write_per_mtok)
            entry["unit"] = "per_million_tokens"
            if note:
                entry["note"] = note
        elif derive_from in manual_models:
            entry = _manual_entry(manual_models[derive_from])
            entry["provider"] = provider
            if note:
                entry["note"] = note
        else:
            # Base model not found — skip with warning.
            print(f"  WARNING: derived model {model_id!r} references "
                  f"unknown base {derive_from!r}, skipping")
            continue

        resolved[model_id] = entry

    return resolved


def dump_json(db: dict) -> str:
    """Serialize pricing_db dict to pretty JSON matching tokdash style."""

    def _default(o):
        if isinstance(o, Decimal):
            return float(o)
        raise TypeError(f"Object of type {type(o)} is not JSON serializable")

    return json.dumps(db, indent=2, default=_default, ensure_ascii=False) + "\n"
