"""Read DeepSeek's model columns and labelled USD-per-million price rows.

Use published peak rates for the database's single tariff. Historical model
names receive live prices only if the current page explicitly lists them.
"""
from __future__ import annotations

import re
from dataclasses import replace

import httpx

from ._pricing_tables import html_tables, plain, usd_price
from .base import ScrapedModel

DEEPSEEK_PRICING_URL = "https://api-docs.deepseek.com/quick_start/pricing/"
_MODEL = re.compile(r"deepseek-[a-z0-9][a-z0-9.-]*", re.I)


def fetch_deepseek_official(timeout: int = 30) -> list[ScrapedModel]:
    try:
        response = httpx.get(DEEPSEEK_PRICING_URL, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"  WARNING: Could not fetch DeepSeek pricing: {exc}")
        return []
    result = _try_parse_table(response.text)
    if not result:
        print("  WARNING: Could not parse DeepSeek pricing table, using config fallback")
    return result


def _try_parse_table(html: str) -> list[ScrapedModel]:
    results: dict[str, ScrapedModel] = {}
    for table in html_tables(html):
        columns = {}
        for row in table:
            if row and plain(row[0]).lower() == "model":
                columns = {i: [cell.lower()] for i, cell in enumerate(row)
                           if _MODEL.fullmatch(cell)}
                break
        if not columns:
            continue
        rates = {column: {} for column in columns}
        for row in table:
            label = plain(" ".join(row[:min(columns)])).lower()
            if label.startswith("model version"):
                for column in columns:
                    if column < len(row) and _MODEL.fullmatch(row[column]):
                        columns[column].append(row[column].lower())
                continue
            if "input" in label and "cache hit" in label:
                field = "cache_read"
            elif "input" in label and "cache write" in label:
                field = "cache_write"
            elif "input" in label and ("cache miss" in label or "cache" not in label):
                field = "input"
            elif "output" in label and "max" not in label:
                field = "output"
            else:
                continue
            if not re.search(r"\b1\s*M\b|\bmillion\b", label, re.I):
                continue
            tier = "off-peak" if "off-peak" in label or "off peak" in label else (
                "peak" if re.search(r"\bpeak\b", label) else "standard"
            )
            for column in columns:
                value = usd_price(row[column]) if column < len(row) else None
                rates[column].setdefault(field, {}).setdefault(tier, []).append(value)
        for column, names in columns.items():
            prices = {}
            for field, tiers in rates[column].items():
                # An invalid peak must not silently become a discounted rate.
                values = tiers.get("peak", tiers.get("standard", []))
                if values and all(value is not None for value in values):
                    prices[field] = max(values)
            if "input" not in prices or "output" not in prices:
                continue
            for name in names:
                canonical = "deepseek-r1" if name == "deepseek-reasoner" else name
                results[canonical] = ScrapedModel(
                    source_id=f"deepseek/{canonical}", provider_slug="deepseek",
                    short_id=canonical, input_per_mtok=prices["input"],
                    output_per_mtok=prices["output"],
                    cache_read_per_mtok=prices.get("cache_read"),
                    cache_write_per_mtok=prices.get("cache_write"),
                    source_name="deepseek_official", source_url=DEEPSEEK_PRICING_URL,
                )
    # The current page explicitly says these legacy API names are still billed
    # at the Flash rate. Do not extend that statement to historical v3/R1 models.
    text = plain(re.sub(r"<[^>]+>", " ", html))
    aliases = re.search(
        r"The legacy names (deepseek-[a-z0-9.-]+) and (deepseek-[a-z0-9.-]+) "
        r"are still accepted.*?billed at the Flash price", text, re.I,
    )
    if aliases and "deepseek-flash" in results:
        for alias in aliases.groups():
            name = alias.lower()
            results[name] = replace(results["deepseek-flash"],
                                    source_id=f"deepseek/{name}", short_id=name)
    return list(results.values())
