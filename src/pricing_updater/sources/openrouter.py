"""OpenRouter /api/v1/models scraper."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import httpx

from .base import CORE_PRICE_FIELDS, ScrapedModel, clean_price

OPENROUTER_API = "https://openrouter.ai/api/v1/models"

_MILLION = Decimal("1000000")


# The four price fields OpenRouter publishes, as (canonical, api) pairs.
PRICE_KEYS = (
    ("input", "prompt"),
    ("output", "completion"),
    ("cache_read", "input_cache_read"),
    ("cache_write", "input_cache_write"),
)


def _parse_per_mtok(raw) -> tuple[Decimal | None, str]:
    """Parse one OpenRouter per-token price into per-MTok.

    Returns ``(value, kind)``, where kind says what the field actually was:

    - ``(Decimal, "price")`` a real price.
    - ``(None, "zero")`` the source published 0, i.e. the field is free.
    - ``(None, "absent")`` the key is missing, i.e. we do not know.
    - ``(None, "invalid")`` a value that cannot be a price: negative, NaN,
      infinity, or unparseable. A broken field is not a price, and a number
      that survives parsing looks valid to every guard downstream.

    The distinction between absent and zero is load-bearing. Treating an absent
    price as free is how a paid model ends up recorded at $0 and bills as
    nothing forever.
    """
    if raw is None:
        return None, "absent"
    value = clean_price(raw)
    if value is None:
        return None, "invalid"
    if value == 0:
        return None, "zero"
    return value * _MILLION, "price"


def fetch_openrouter(
    tracked_providers: dict[str, str],
    skip_patterns: list[str],
    timeout: int = 30,
    save_raw: Path | None = None,
    stats: dict | None = None,
) -> list[ScrapedModel]:
    """Fetch all models from OpenRouter, filter to tracked providers.

    Args:
        tracked_providers: {openrouter_slug: tokdash_provider}, e.g. {"openai": "openai"}.
        skip_patterns: regex patterns to skip model IDs.
        timeout: HTTP timeout in seconds.
        save_raw: if set, write the raw API response to this path.
        stats: optional dict the caller reads back for the run summary.

    Returns:
        List of ScrapedModel for tracked providers. Models with no billable
        price on either side are dropped and counted in ``stats``.
    """
    resp = httpx.get(OPENROUTER_API, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()

    if save_raw is not None:
        save_raw.parent.mkdir(parents=True, exist_ok=True)
        save_raw.write_text(json.dumps(payload, indent=2) + "\n")

    models_raw = payload.get("data", [])
    compiled_skips = [re.compile(p) for p in skip_patterns]

    if stats is not None:
        stats["rows_seen"] = len(models_raw)
        stats["free_skipped"] = 0
        stats["unpriced_skipped"] = 0
        stats["partial_pricing"] = 0

    results: list[ScrapedModel] = []
    for m in models_raw:
        model_id: str = m.get("id", "")
        if "/" not in model_id:
            continue

        provider_slug, _, short_id = model_id.partition("/")
        if provider_slug not in tracked_providers:
            continue

        if any(p.search(model_id) for p in compiled_skips):
            continue

        pricing = m.get("pricing") or {}

        values: dict[str, Decimal | None] = {}
        zeros: set[str] = set()
        invalid: set[str] = set()
        for field_name, api_key in PRICE_KEYS:
            value, kind = _parse_per_mtok(pricing.get(api_key))
            values[field_name] = value
            if kind == "zero":
                zeros.add(field_name)
            elif kind == "invalid":
                invalid.add(field_name)

        # A published zero is known data, not a gap: it stays out of missing and
        # lands in explicit_zero_fields, where the guards can hold it for a human
        # instead of the model vanishing from the proposal. An invalid value is
        # the opposite: it is a gap the source did not admit to, so it counts as
        # missing rather than being reported as a free or negative price.
        missing = ({n for n, v in values.items() if v is None} - zeros) | invalid

        core_missing = missing.intersection(CORE_PRICE_FIELDS)
        # No billable rate on either side, whether absent or published free.
        core_absent = {f for f in CORE_PRICE_FIELDS if values[f] is None}
        if len(core_absent) == len(CORE_PRICE_FIELDS):
            # Nothing billable on either side. Free variants stay out of the DB
            # as they always have; a truly unpriced row is a source problem and
            # gets counted so the run summary can shout about it.
            if zeros.issuperset(CORE_PRICE_FIELDS):
                if stats is not None:
                    stats["free_skipped"] += 1
            elif stats is not None:
                stats["unpriced_skipped"] += 1
            continue
        # Free on one side only: keep it, flag it, let the guard decide.
        zeros = zeros.intersection(values.keys())

        if core_missing and stats is not None:
            stats["partial_pricing"] += 1
        if invalid and stats is not None:
            stats.setdefault("invalid_fields", {})[model_id] = sorted(invalid)

        results.append(
            ScrapedModel(
                source_id=model_id,
                provider_slug=provider_slug,
                short_id=short_id,
                input_per_mtok=values["input"] or Decimal("0"),
                output_per_mtok=values["output"] or Decimal("0"),
                cache_read_per_mtok=values["cache_read"],
                cache_write_per_mtok=values["cache_write"],
                source_name="openrouter",
                source_url=f"openrouter.ai/{model_id}",
                context_length=m.get("context_length"),
                missing_fields=missing,
                explicit_zero_fields=zeros,
                invalid_fields=invalid,
                raw_extra={
                    k: v
                    for k, v in pricing.items()
                    if k not in ("prompt", "completion", "input_cache_read", "input_cache_write")
                },
            )
        )

    return results
