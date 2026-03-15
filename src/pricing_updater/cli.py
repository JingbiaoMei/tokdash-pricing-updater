"""CLI entry point for tokdash-pricing-updater.

Usage:
    python -m pricing_updater scrape   [--tokdash-repo PATH]
    python -m pricing_updater diff     [--tokdash-repo PATH]
    python -m pricing_updater propose  [--tokdash-repo PATH]
    python -m pricing_updater auto     [--tokdash-repo PATH]  (CI mode)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _project_root() -> Path:
    """Return the project root (parent of src/)."""
    return Path(__file__).resolve().parent.parent.parent


def _load_yaml(name: str) -> dict:
    path = _project_root() / "config" / name
    return yaml.safe_load(path.read_text()) or {}


def _load_config():
    providers_cfg = _load_yaml("tracked_providers.yaml")
    aliases_cfg = _load_yaml("aliases.yaml")
    manual_cfg = _load_yaml("manual_models.yaml")
    derived_cfg = _load_yaml("derived_models.yaml")
    policy_cfg = _load_yaml("pricing_policy.yaml")
    return providers_cfg, aliases_cfg, manual_cfg, derived_cfg, policy_cfg


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def _step_scrape(providers_cfg: dict, policy_cfg: dict, output_dir: Path):
    """Fetch from all sources, return raw scraped models."""
    from .sources.openrouter import fetch_openrouter

    tracked = {
        p["slug"]: p["tokdash_provider"]
        for p in providers_cfg.get("providers", [])
    }
    skip = providers_cfg.get("skip_patterns", [])

    raw_path = output_dir / "raw" / f"openrouter_{date.today().isoformat()}.json"
    print(f"Fetching OpenRouter API...", flush=True)
    scraped = fetch_openrouter(tracked, skip, save_raw=raw_path)
    print(f"  -> {len(scraped)} models from tracked providers")

    return scraped, tracked


def _step_canonicalize(scraped, provider_map):
    from .canonicalize import canonicalize
    canonical = canonicalize(scraped, provider_map)
    print(f"  -> {len(canonical)} canonical models")
    return canonical


def _step_policy(canonical, policy_cfg):
    from .policy import apply_policy
    cache_fb = policy_cfg.get("cache_fallback", {})
    max_dp = policy_cfg.get("rounding", {}).get("max_decimal_places", 6)
    return apply_policy(canonical, cache_fb, max_dp)


def _step_render(
    canonical, manual_cfg, derived_cfg, aliases_cfg, policy_cfg,
    current_db,
):
    from .render_tokdash_db import render, resolve_derived_models, dump_json

    manual_models = manual_cfg.get("models", {})
    derived_models_cfg = derived_cfg.get("models", {})
    aliases = aliases_cfg.get("aliases", {})
    provider_order = policy_cfg.get("provider_order", [])

    canonical_by_id = {m.canonical_id: m for m in canonical}

    resolved_derived = resolve_derived_models(
        derived_models_cfg, canonical_by_id, manual_models,
    )

    db = render(
        canonical_models=canonical,
        manual_models=manual_models,
        derived_models=resolved_derived,
        aliases=aliases,
        provider_order=provider_order,
        current_version=current_db.get("version", "2.0.0"),
        db_note=current_db.get("note", ""),
    )
    return db


def _step_diff(current_db, proposed_db, manual_cfg, derived_cfg, state_dir, policy_cfg):
    from .diff import (
        compute_diff, generate_diff_report, load_last_seen, save_last_seen,
    )

    last_seen_path = state_dir / "last_seen.json"
    last_seen = load_last_seen(last_seen_path)
    min_misses = policy_cfg.get("removal", {}).get("min_consecutive_misses", 4)

    manual_ids = set((manual_cfg.get("models") or {}).keys())
    derived_ids = set((derived_cfg.get("models") or {}).keys())

    changes, updated_last_seen = compute_diff(
        current_models=current_db.get("models", {}),
        proposed_models=proposed_db.get("models", {}),
        manual_ids=manual_ids,
        derived_ids=derived_ids,
        last_seen=last_seen,
        min_misses=min_misses,
    )

    save_last_seen(last_seen_path, updated_last_seen)

    report = generate_diff_report(changes, today=date.today().isoformat())
    return changes, report


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_scrape(args):
    """Fetch from sources and save raw + canonical snapshots."""
    providers_cfg, aliases_cfg, manual_cfg, derived_cfg, policy_cfg = _load_config()
    output_dir = _project_root() / "output"

    scraped, provider_map = _step_scrape(providers_cfg, policy_cfg, output_dir)
    canonical = _step_canonicalize(scraped, provider_map)
    canonical = _step_policy(canonical, policy_cfg)

    # Save canonical snapshot.
    from .render_tokdash_db import _decimal_to_float
    snapshot = {
        m.canonical_id: {
            "provider": m.provider,
            "input": _decimal_to_float(m.input_per_mtok),
            "output": _decimal_to_float(m.output_per_mtok),
            "cache_read": _decimal_to_float(m.cache_read_per_mtok),
            "cache_write": _decimal_to_float(m.cache_write_per_mtok),
            "source": m.source_url,
        }
        for m in canonical
    }
    snap_path = output_dir / "canonical" / f"canonical_{date.today().isoformat()}.json"
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    snap_path.write_text(json.dumps(snapshot, indent=2) + "\n")
    print(f"Saved canonical snapshot: {snap_path}")


def cmd_diff(args):
    """Scrape, render, and diff against current tokdash pricing_db."""
    providers_cfg, aliases_cfg, manual_cfg, derived_cfg, policy_cfg = _load_config()
    output_dir = _project_root() / "output"
    state_dir = _project_root() / "state"

    current_db = _load_current_db(args.tokdash_repo)

    scraped, provider_map = _step_scrape(providers_cfg, policy_cfg, output_dir)
    canonical = _step_canonicalize(scraped, provider_map)
    canonical = _step_policy(canonical, policy_cfg)

    proposed_db = _step_render(
        canonical, manual_cfg, derived_cfg, aliases_cfg, policy_cfg, current_db,
    )

    changes, report = _step_diff(
        current_db, proposed_db, manual_cfg, derived_cfg, state_dir, policy_cfg,
    )

    print(f"\n{'=' * 60}")
    print(report)
    print(f"{'=' * 60}")

    report_path = output_dir / "proposals" / f"diff_{date.today().isoformat()}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report)
    print(f"\nDiff report saved: {report_path}")


def cmd_propose(args):
    """Full interactive pipeline: scrape -> diff -> review -> write proposed DB."""
    providers_cfg, aliases_cfg, manual_cfg, derived_cfg, policy_cfg = _load_config()
    output_dir = _project_root() / "output"
    state_dir = _project_root() / "state"

    current_db = _load_current_db(args.tokdash_repo)

    scraped, provider_map = _step_scrape(providers_cfg, policy_cfg, output_dir)
    canonical = _step_canonicalize(scraped, provider_map)
    canonical = _step_policy(canonical, policy_cfg)

    proposed_db = _step_render(
        canonical, manual_cfg, derived_cfg, aliases_cfg, policy_cfg, current_db,
    )

    changes, report = _step_diff(
        current_db, proposed_db, manual_cfg, derived_cfg, state_dir, policy_cfg,
    )

    if not changes:
        print("\nNo changes detected. Pricing DB is up to date.")
        return

    print(f"\n{'=' * 60}")
    print(report)
    print(f"{'=' * 60}\n")

    from .approval import interactive_review
    approved = interactive_review(changes)

    if not approved:
        print("No changes approved. Nothing to write.")
        return

    # Re-render with only approved changes applied to current DB.
    final_db = _apply_approved(current_db, approved)
    out_path = output_dir / "proposals" / "proposed_pricing_db.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    from .render_tokdash_db import dump_json
    out_path.write_text(dump_json(final_db))
    print(f"\nWrote {out_path}")
    print(f"Version: {final_db.get('version')}")
    print(f"\nTo apply: cp {out_path} {args.tokdash_repo}/src/tokdash/pricing_db.json")


def cmd_auto(args):
    """CI mode: scrape -> diff -> auto-approve safe_add -> write + optionally PR."""
    providers_cfg, aliases_cfg, manual_cfg, derived_cfg, policy_cfg = _load_config()
    output_dir = _project_root() / "output"
    state_dir = _project_root() / "state"

    current_db = _load_current_db(args.tokdash_repo)

    scraped, provider_map = _step_scrape(providers_cfg, policy_cfg, output_dir)
    canonical = _step_canonicalize(scraped, provider_map)
    canonical = _step_policy(canonical, policy_cfg)

    proposed_db = _step_render(
        canonical, manual_cfg, derived_cfg, aliases_cfg, policy_cfg, current_db,
    )

    changes, report = _step_diff(
        current_db, proposed_db, manual_cfg, derived_cfg, state_dir, policy_cfg,
    )

    if not changes:
        print("No changes detected. Pricing DB is up to date.")
        return

    from .approval import auto_review
    approved = auto_review(changes)

    # Save report regardless.
    report_path = output_dir / "proposals" / f"diff_{date.today().isoformat()}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report)

    if not approved and not [c for c in changes if c.classification != "safe_add"]:
        print("No actionable changes.")
        return

    if approved:
        final_db = _apply_approved(current_db, approved)
        out_path = output_dir / "proposals" / "proposed_pricing_db.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        from .render_tokdash_db import dump_json
        out_path.write_text(dump_json(final_db))
        print(f"Wrote {out_path} ({len(approved)} safe_add changes)")

        if args.create_pr:
            from .github_pr import create_pr
            branch = f"pricing-update-{date.today().isoformat()}"
            create_pr(
                tokdash_repo=Path(args.tokdash_repo),
                proposed_db_path=out_path,
                diff_report=report,
                branch_name=branch,
                dry_run=args.dry_run,
            )
    else:
        print("No safe_add changes to apply. Review-required changes in report.")
        print(f"Report: {report_path}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_current_db(tokdash_repo: str) -> dict:
    db_path = Path(tokdash_repo) / "src" / "tokdash" / "pricing_db.json"
    if not db_path.exists():
        print(f"ERROR: {db_path} not found", file=sys.stderr)
        sys.exit(1)
    return json.loads(db_path.read_text())


def _apply_approved(current_db: dict, approved) -> dict:
    """Apply approved changes to a copy of current_db."""
    import copy
    from datetime import datetime, timezone

    result = copy.deepcopy(current_db)
    models = result.setdefault("models", {})

    for change in approved:
        if change.change_type == "new" and change.new_entry:
            models[change.model_id] = change.new_entry
        elif change.change_type == "price_change" and change.new_entry:
            models[change.model_id] = change.new_entry
        elif change.change_type == "stale":
            # manual_only — should never reach here via auto, but guard anyway.
            pass

    # Bump version.
    old_ver = result.get("version", "2.0.0")
    parts = old_ver.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    result["version"] = ".".join(parts)
    result["lastUpdated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return result


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="pricing-updater",
        description="Layered pricing DB generator for tokdash",
    )
    parser.add_argument(
        "--tokdash-repo",
        default=str(_project_root().parent / "tokdash"),
        help="Path to the tokdash repo (default: ../tokdash relative to this project)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scrape", help="Fetch from sources and save snapshots")
    sub.add_parser("diff", help="Scrape and show diff against current DB")
    sub.add_parser("propose", help="Interactive: scrape, diff, review, write proposal")

    auto_p = sub.add_parser("auto", help="CI mode: scrape, diff, auto-approve safe_add")
    auto_p.add_argument("--create-pr", action="store_true", help="Open a PR on tokdash")
    auto_p.add_argument("--dry-run", action="store_true", help="Print PR commands only")

    args = parser.parse_args()

    dispatch = {
        "scrape": cmd_scrape,
        "diff": cmd_diff,
        "propose": cmd_propose,
        "auto": cmd_auto,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
