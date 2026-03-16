"""Shared HTML table parser for official pricing pages.

Most provider docs render as HTML tables (<tr>/<td>), not markdown.
This module extracts rows from HTML tables for use by all official scrapers.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation


def extract_table_rows(html: str) -> list[list[str]]:
    """Extract all <tr> rows as lists of cleaned cell text."""
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL)
    result = []
    for row in rows:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.DOTALL)
        clean = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        if clean:
            result.append(clean)
    return result


def parse_price(cell: str) -> float | None:
    """Extract a dollar price from a cell like '$0.60' or 'Free'."""
    cell = cell.strip()
    if not cell or "free" in cell.lower() or cell in ("-", "\\", "N/A", "n/a"):
        return None
    m = re.search(r"\$?([\d.]+)", cell)
    if m:
        try:
            return float(Decimal(m.group(1)))
        except InvalidOperation:
            pass
    return None
