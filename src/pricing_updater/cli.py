"""CLI entry point for tokdash-pricing-updater.

Usage:
    python -m pricing_updater scrape   [--tokdash-repo PATH]
    python -m pricing_updater diff     [--tokdash-repo PATH]
    python -m pricing_updater propose  [--tokdash-repo PATH]
    python -m pricing_updater auto     [--tokdash-repo PATH] [--create-pr]
                                       [--branch bot/pricing-db] [--base main]
                                       [--labels a,b] [--max-body-rows N]
                                       [--dry-run]

Artifacts and state default to ./output and ./state; set
PRICING_UPDATER_OUTPUT_DIR / PRICING_UPDATER_STATE_DIR to move them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import OrderedDict
from datetime import date
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _project_root() -> Path:
    """Return the project root (parent of src/)."""
    return Path(__file__).resolve().parent.parent.parent


def _output_dir() -> Path:
    """Where generated artifacts go.

    Overridable so a run against a read-only checkout, a CI workspace, or a test
    does not need the repository itself to be writable.
    """
    override = os.environ.get("PRICING_UPDATER_OUTPUT_DIR")
    return Path(override) if override else _project_root() / "output"


def _state_dir() -> Path:
    """Where last_seen state lives. Same override rationale as _output_dir()."""
    override = os.environ.get("PRICING_UPDATER_STATE_DIR")
    return Path(override) if override else _project_root() / "state"


def _load_yaml(name: str) -> dict:
    path = _project_root() / "config" / name
    return yaml.safe_load(path.read_text()) or {}


def _load_config():
    providers_cfg = _load_yaml("tracked_providers.yaml")
    aliases_cfg = _load_yaml("aliases.yaml")
    manual_cfg = _load_yaml("manual_models.yaml")
    derived_cfg = _load_yaml("derived_models.yaml")
    policy_cfg = _load_yaml("pricing_policy.yaml")
    official_cfg = _load_yaml("official_prices.yaml")
    family_map_cfg = _load_yaml("official_family_map.yaml")
    fx_cfg = _load_yaml("fx_rates.yaml")
    return (providers_cfg, aliases_cfg, manual_cfg, derived_cfg,
            policy_cfg, official_cfg, family_map_cfg, fx_cfg)


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
    print("Fetching OpenRouter API...", flush=True)
    stats: dict = {}
    scraped = fetch_openrouter(tracked, skip, save_raw=raw_path, stats=stats)
    print(
        f"  -> {len(scraped)} models from tracked providers "
        f"({stats.get('rows_seen', 0)} rows seen, "
        f"{stats.get('free_skipped', 0)} free, "
        f"{stats.get('unpriced_skipped', 0)} unpriced, "
        f"{stats.get('partial_pricing', 0)} partly priced)"
    )
    invalid = stats.get("invalid_fields") or {}
    if invalid:
        # A negative or NaN rate is not a missing price the heuristics can
        # paper over; it is a source that sent us something broken.
        print(f"  -> {len(invalid)} row(s) carried a value that cannot be a price")

    return scraped, tracked, stats


def _load_anomaly_cfg() -> dict:
    """Guard thresholds sit apart from pricing policy: they change for
    operational reasons, not pricing reasons."""
    return _load_yaml("anomaly_policy.yaml")


def _step_canonicalize(scraped, provider_map):
    from .canonicalize import canonicalize
    canonical = canonicalize(scraped, provider_map)
    print(f"  -> {len(canonical)} canonical models")
    return canonical


def _step_guards(canonical):
    """Hold back models the sources never priced, rather than rendering zeros.

    Returns (usable, dropped) where dropped rows are dicts ready for reporting.
    """
    from .guards import partition_unpriceable

    usable, dropped = partition_unpriceable(canonical)
    if dropped:
        print(f"  -> {len(usable)} priceable, {len(dropped)} unpriceable (not rendered)")
        for row in dropped[:10]:
            print(f"     unpriceable: {row['model_id']} ({row['provider']}): {row['reason']}")
        if len(dropped) > 10:
            print(f"     ... and {len(dropped) - 10} more")
    return usable, dropped


def _step_provenance(canonical) -> dict:
    """Per-field source of truth, as a sidecar rather than a DB schema change."""
    return {
        m.canonical_id: {
            "field_sources": m.field_sources,
            "evidence": m.evidence,
            "explicit_zero_fields": sorted(m.explicit_zero_fields),
        }
        for m in canonical
        if m.field_sources or m.evidence or m.explicit_zero_fields
    }


def _step_fetch_official(official_cfg, health: list | None = None):
    """Fetch from official provider sites, merge with YAML config.

    ``health`` collects one record per adapter. Falling back to YAML is the right
    behaviour for a broken scraper and the wrong thing to leave invisible: a
    reviewer reading a pricing PR needs to know that half the official sources
    were down and the numbers came from a hand-maintained file.
    """
    from .sources.official_fetch import fetch_all_official

    # Start with YAML config as baseline.
    official_prices = dict(official_cfg.get("models", {}))

    # Overlay with live-scraped official prices (these take precedence
    # over stale YAML entries, but YAML covers providers we can't scrape).
    print("Fetching official provider pricing...", flush=True)
    live = fetch_all_official(health=health)
    if live:
        print(f"  -> {len(live)} models from official sources")
        for model_id, price_data in live.items():
            # Merge: live scraped data overrides YAML for the same key.
            official_prices[model_id] = price_data
    else:
        print("  -> 0 models from official sources (using YAML config only)")

    return official_prices


def _health_line(health: list) -> str:
    """One line saying which official adapters did not answer."""
    if not health:
        return ""
    broken = [h for h in health if h["status"] != "ok"]
    ok = len(health) - len(broken)
    if not broken:
        return f"Official sources: all {len(health)} adapters answered."
    detail = "; ".join(f"{h['source']} {h['status']} ({h['detail']})" for h in broken)
    return (
        f"Official sources: {ok}/{len(health)} adapters answered; the rest fell back to "
        f"`config/official_prices.yaml` — {detail}."
    )


def _step_policy(canonical, policy_cfg, official_prices, family_map_cfg, fx_cfg):
    from .policy import apply_policy, resolve_family

    cache_fb = policy_cfg.get("cache_fallback", {})
    max_dp = policy_cfg.get("rounding", {}).get("max_decimal_places", 6)
    family_map = family_map_cfg.get("families", [])
    fx_rates = fx_cfg.get("rates", {"USD": 1.0})

    n_match = sum(
        1 for m in canonical
        if resolve_family(m.canonical_id, official_prices, family_map) is not None
    )
    if n_match:
        print(f"  -> {n_match} models have official price overlay (direct + family)")

    return apply_policy(canonical, cache_fb, max_dp, official_prices, family_map, fx_rates)


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
        compute_alias_diff, compute_diff, generate_diff_report, load_last_seen, save_last_seen,
    )

    last_seen_path = state_dir / "last_seen.json"
    last_seen = load_last_seen(last_seen_path)
    removal_cfg = policy_cfg.get("removal", {})
    min_misses = removal_cfg.get("min_consecutive_misses", 4)
    enable_stale = removal_cfg.get("enabled", True)

    manual_ids = set((manual_cfg.get("models") or {}).keys())
    derived_ids = set((derived_cfg.get("models") or {}).keys())

    changes, updated_last_seen = compute_diff(
        current_models=current_db.get("models", {}),
        proposed_models=proposed_db.get("models", {}),
        manual_ids=manual_ids,
        derived_ids=derived_ids,
        last_seen=last_seen,
        min_misses=min_misses,
        enable_stale=enable_stale,
    )
    changes.extend(
        compute_alias_diff(
            current_db.get("aliases", {}),
            proposed_db.get("aliases", {}),
        )
    )

    save_last_seen(last_seen_path, updated_last_seen)

    report = generate_diff_report(changes, today=date.today().isoformat())
    return changes, report


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_scrape(args):
    """Fetch from sources and save raw + canonical snapshots."""
    (providers_cfg, aliases_cfg, manual_cfg, derived_cfg,
     policy_cfg, official_cfg, family_map_cfg, fx_cfg) = _load_config()
    output_dir = _output_dir()

    scraped, provider_map, stats = _step_scrape(providers_cfg, policy_cfg, output_dir)
    canonical = _step_canonicalize(scraped, provider_map)
    official_prices = _step_fetch_official(official_cfg)
    canonical = _step_policy(canonical, policy_cfg, official_prices, family_map_cfg, fx_cfg)
    canonical, _dropped = _step_guards(canonical)

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
    (providers_cfg, aliases_cfg, manual_cfg, derived_cfg,
     policy_cfg, official_cfg, family_map_cfg, fx_cfg) = _load_config()
    output_dir = _output_dir()
    state_dir = _state_dir()

    current_db = _load_current_db(args.tokdash_repo)

    scraped, provider_map, stats = _step_scrape(providers_cfg, policy_cfg, output_dir)
    canonical = _step_canonicalize(scraped, provider_map)
    official_prices = _step_fetch_official(official_cfg)
    canonical = _step_policy(canonical, policy_cfg, official_prices, family_map_cfg, fx_cfg)
    canonical, _dropped = _step_guards(canonical)

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
    (providers_cfg, aliases_cfg, manual_cfg, derived_cfg,
     policy_cfg, official_cfg, family_map_cfg, fx_cfg) = _load_config()
    output_dir = _output_dir()
    state_dir = _state_dir()

    current_db = _load_current_db(args.tokdash_repo)

    scraped, provider_map, stats = _step_scrape(providers_cfg, policy_cfg, output_dir)
    canonical = _step_canonicalize(scraped, provider_map)
    official_prices = _step_fetch_official(official_cfg)
    canonical = _step_policy(canonical, policy_cfg, official_prices, family_map_cfg, fx_cfg)
    canonical, _dropped = _step_guards(canonical)

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
    final_db = _apply_approved(current_db, proposed_db, approved)
    out_path = output_dir / "proposals" / "proposed_pricing_db.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    from .render_tokdash_db import dump_json
    out_path.write_text(dump_json(final_db))
    print(f"\nWrote {out_path}")
    print(f"Version: {final_db.get('version')}")
    print(f"\nTo apply: cp {out_path} {args.tokdash_repo}/src/tokdash/pricing_db.json")


def cmd_auto(args):
    """CI mode: scrape, diff, guard, then publish one reviewable PR.

    Everything the guards accept goes into the PR diff, additions and repricing
    alike, so reviewing the diff and merging is the whole job. Anything a guard
    distrusts is held out of the diff and listed in the body instead. Publication
    failure is a failed run, never a green one.

    The base branch is fetched and read before anything is generated, and the
    proposal is pinned to that commit. Generating from whatever is on disk is how
    a scheduled run ends up publishing a database computed against an older one,
    quietly reverting an edit merged in the meantime.
    """
    (providers_cfg, aliases_cfg, manual_cfg, derived_cfg,
     policy_cfg, official_cfg, family_map_cfg, fx_cfg) = _load_config()
    from .guards import GuardPolicy

    guard_policy = GuardPolicy.load(_load_anomaly_cfg())
    output_dir = _output_dir()
    state_dir = _state_dir()
    today = date.today().isoformat()

    # ----------------------------------------------------------------- base
    # Read the database as of the base tip, then hold that SHA all the way to
    # publication so the proposal and the branch it lands on cannot drift apart.
    from .github_pr import fetch_base

    repo = Path(args.tokdash_repo)
    base_sha = None
    if args.create_pr:
        try:
            base_sha = fetch_base(repo, args.base)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"Base: {args.base} at {base_sha[:7]}")
        # The CI jobs pass this between them; publishing against anything else is
        # refused, so a stale proposal cannot overwrite a newer edit.
        base_path = output_dir / "proposals" / "base_sha.txt"
        base_path.parent.mkdir(parents=True, exist_ok=True)
        base_path.write_text(base_sha + "\n")

    current_db = _load_current_db(args.tokdash_repo, base_sha)

    scraped, provider_map, stats = _step_scrape(providers_cfg, policy_cfg, output_dir)

    from . import guards

    fatal = guards.check_source_volume(stats, guard_policy)
    if fatal:
        for note in fatal:
            print(f"FATAL: {note}", file=sys.stderr)
        sys.exit(1)

    canonical = _step_canonicalize(scraped, provider_map)
    health: list[dict] = []
    official_prices = _step_fetch_official(official_cfg, health)
    canonical = _step_policy(canonical, policy_cfg, official_prices, family_map_cfg, fx_cfg)
    canonical, dropped = _step_guards(canonical)
    invalid = [d for d in dropped if d.get("invalid_fields")]

    proposed_db = _step_render(
        canonical, manual_cfg, derived_cfg, aliases_cfg, policy_cfg, current_db,
    )

    changes, report = _step_diff(
        current_db, proposed_db, manual_cfg, derived_cfg, state_dir, policy_cfg,
    )

    decisions = guards.classify(changes, guard_policy)
    proposable = guards.proposable_changes(decisions)
    held = guards.held_changes(decisions)
    print(
        f"  -> {len(proposable)} proposable, {len(held)} held "
        f"(of {len(changes)} computed)"
    )
    if invalid:
        print(
            f"  -> {len(invalid)} model(s) held out with an unusable price value: "
            + ", ".join(sorted(d["model_id"] for d in invalid)[:8])
        )

    # Evidence a reviewer can check without rerunning anything.
    from .coverage import build_coverage, summarize, write_coverage
    from .render_tokdash_db import dump_json

    coverage = build_coverage(
        current_db.get("models", {}),
        proposed_db.get("models", {}),
        decisions,
        dropped,
        set((manual_cfg.get("models") or {}).keys()),
        set((derived_cfg.get("models") or {}).keys()),
    )
    coverage_path = write_coverage(
        output_dir / "proposals" / f"coverage_{today}.json", coverage,
    )
    print(summarize(coverage))

    prov_path = output_dir / "proposals" / f"provenance_{today}.json"
    prov_path.parent.mkdir(parents=True, exist_ok=True)
    provenance = _step_provenance(canonical)
    prov_path.write_text(json.dumps(provenance, indent=2) + "\n")

    # Which official adapters answered. A scraper that quietly died would
    # otherwise look like a day with no official price changes.
    health_path = output_dir / "proposals" / f"source_health_{today}.json"
    health_path.write_text(json.dumps(health, indent=2) + "\n")

    report_path = output_dir / "proposals" / f"diff_{today}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report)

    if not changes:
        print("No changes detected. Pricing DB is up to date.")
        return

    if not proposable:
        print("Nothing proposable. Findings are held; see the run artifacts.")
        for decision in held[:10]:
            print(f"  held: {decision.change.model_id} ({decision.change.change_type}): "
                  f"{'; '.join(decision.reasons)}")
        if len(held) > 10:
            print(f"  ... and {len(held) - 10} more")
        print(f"Report: {report_path}")
        return

    final_db = _apply_approved(current_db, proposed_db, proposable)
    out_path = output_dir / "proposals" / "proposed_pricing_db.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(dump_json(final_db))
    print(f"Wrote {out_path} ({len(proposable)} changes)")

    if not args.create_pr:
        if held:
            print(f"{len(held)} changes held for a human; see {report_path}")
        return

    from .diff import generate_pr_body
    from .github_pr import publish

    # A volume or magnitude breach says the source changed shape, not that the
    # world's prices did, so the PR opens as a draft for a longer look.
    draft = guards.should_draft(decisions)

    body = generate_pr_body(
        decisions,
        meta={
            "generated": today,
            "base_sha": base_sha,
            "run_url": os.environ.get("GITHUB_SERVER_URL", "")
            and f"{os.environ['GITHUB_SERVER_URL']}/{os.environ.get('GITHUB_REPOSITORY', '')}"
                   f"/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}",
            "source_line": (
                f"Sources: OpenRouter {stats.get('rows_seen', 0)} rows, "
                f"{len(scraped)} kept, {stats.get('unpriced_skipped', 0)} unpriced."
            ),
            "coverage_line": summarize(coverage),
            "health_line": _health_line(health),
            "provenance": provenance,
        },
        max_rows=args.max_body_rows,
    )

    body_path = output_dir / "proposals" / "pr_body.md"
    body_path.write_text(body + "\n")
    print(f"PR body: {body_path}")

    result = publish(
        tokdash_repo=repo,
        proposed_db_path=out_path,
        body=body,
        title=f"Pricing DB update ({today})",
        branch=args.branch,
        base=args.base,
        labels=[s for s in args.labels.split(",") if s] if args.labels else None,
        dry_run=args.dry_run,
        generated_at_sha=base_sha,
        draft=draft,
    )

    for note in result.warnings:
        print(f"WARNING: {note}", file=sys.stderr)

    if not result.ok:
        print(f"ERROR: publication failed: {result.detail}", file=sys.stderr)
        sys.exit(1)

    print(f"{result.action}: {result.pr_url or result.detail}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_current_db(tokdash_repo: str, at_sha: str | None = None) -> dict:
    """Read the pricing DB the run is diffing against.

    With a commit it reads what that commit holds rather than what happens to be
    on disk, so a CI checkout cannot propose against a stale working copy.
    """
    if at_sha:
        from .github_pr import read_db_at

        try:
            return read_db_at(Path(tokdash_repo), at_sha)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)

    db_path = Path(tokdash_repo) / "src" / "tokdash" / "pricing_db.json"
    if not db_path.exists():
        print(f"ERROR: {db_path} not found", file=sys.stderr)
        sys.exit(1)
    return json.loads(db_path.read_text())


def _apply_approved(current_db: dict, proposed_db: dict, approved) -> dict:
    """Apply approved changes while preserving the current key order.

    Optimized for a minimal, readable diff against the current pricing_db.json:
      * Existing entries keep their position; approved price changes are applied
        in place (so git sees an edit, not a move).
      * Approved new models are inserted right after their provider's last
        existing entry, keeping each provider contiguous.
      * Models from brand-new providers are appended as their own section
        (reusing the rendered section comment) at the end of the file.
      * Stale/legacy entries are never moved or removed.

    This deliberately does NOT re-emit the freshly rendered section order from
    ``proposed_db`` — doing so relocates every existing entry and turns a handful
    of additions into a huge delete/add diff.
    """
    import copy
    from datetime import datetime, timezone

    result = copy.deepcopy(current_db)
    current_models = current_db.get("models", {})
    proposed_models = proposed_db.get("models", {})

    approved_new = {c.model_id for c in approved if c.change_type == "new"}
    approved_chg = {c.model_id for c in approved if c.change_type == "price_change"}
    approved_aliases = {
        c.model_id for c in approved if c.change_type in {"alias_add", "alias_change"}
    }

    def _provider_of(model_id: str) -> str:
        entry = proposed_models.get(model_id)
        return entry.get("provider", "") if isinstance(entry, dict) else ""

    # 1. Copy current models in their existing order, applying approved price
    #    changes in place. Everything else (incl. stale entries) is untouched.
    models: dict = {}
    for key, value in current_models.items():
        if key in approved_chg and isinstance(proposed_models.get(key), dict):
            models[key] = proposed_models[key]
        else:
            models[key] = value

    # 2. Group approved-new models by provider (sorted for deterministic output).
    new_by_provider: dict[str, list[str]] = {}
    for model_id in approved_new:
        if model_id in models:
            continue
        new_by_provider.setdefault(_provider_of(model_id), []).append(model_id)
    for ids in new_by_provider.values():
        ids.sort()

    existing_providers = {
        v.get("provider")
        for v in current_models.values()
        if isinstance(v, dict) and v.get("provider")
    }

    # 2a. Existing providers: insert new models after the provider's last entry.
    for provider, ids in new_by_provider.items():
        if provider not in existing_providers:
            continue
        keys = list(models.keys())
        last_idx = max(
            (i for i, k in enumerate(keys)
             if isinstance(models[k], dict) and models[k].get("provider") == provider),
            default=-1,
        )
        if last_idx < 0:
            continue
        rebuilt: dict = {}
        for i, k in enumerate(keys):
            rebuilt[k] = models[k]
            if i == last_idx:
                for model_id in ids:
                    rebuilt[model_id] = proposed_models[model_id]
        models = rebuilt

    # 2b. Brand-new providers: append their rendered section (comment + models)
    #     at the end, preserving the proposed grouping.
    brand_new = {p for p in new_by_provider if p and p not in existing_providers}
    if brand_new:
        section = None
        appended_comments: set = set()
        for key, value in proposed_models.items():
            if not isinstance(value, dict):
                section = (key, value)  # a "_comment_*" header
                continue
            if (
                key in approved_new
                and key not in models
                and value.get("provider") in brand_new
            ):
                if section and section[0] not in models and section[0] not in appended_comments:
                    models[section[0]] = section[1]
                    appended_comments.add(section[0])
                models[key] = value

    # Safety net: guarantee every approved-new model is present even if its
    # provider could not be placed above (e.g. missing provider field).
    for model_id in sorted(approved_new):
        if model_id not in models and isinstance(proposed_models.get(model_id), dict):
            models[model_id] = proposed_models[model_id]

    result["models"] = models

    proposed_aliases = proposed_db.get("aliases", {})
    if approved_aliases:
        merged_aliases = dict(result.get("aliases", {}))
        for alias in sorted(approved_aliases):
            if alias in proposed_aliases:
                merged_aliases[alias] = proposed_aliases[alias]
        result["aliases"] = OrderedDict(sorted(merged_aliases.items()))

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

    auto_p = sub.add_parser(
        "auto",
        help="CI mode: scrape, diff, guard, and publish one reviewable PR",
    )
    auto_p.add_argument("--create-pr", action="store_true", help="Open or refresh the bot PR")
    auto_p.add_argument("--dry-run", action="store_true", help="Fetch and plan, publish nothing")
    auto_p.add_argument("--branch", default="bot/pricing-db", help="Bot-owned branch to publish")
    auto_p.add_argument("--base", default="main", help="Branch the PR targets")
    auto_p.add_argument("--labels", default="", help="Comma-separated labels, best-effort")
    auto_p.add_argument("--max-body-rows", type=int, default=60, help="Rows per PR body table")

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
