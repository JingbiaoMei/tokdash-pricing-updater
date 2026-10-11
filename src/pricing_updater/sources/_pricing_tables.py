"""Read labelled pricing tables without executing provider JavaScript."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from html.parser import HTMLParser

from .base import clean_price


def plain(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("*", "").replace("`", "")).strip()


def usd_price(text: str) -> Decimal | None:
    """Accept an explicit USD amount, never a context length or RMB price."""
    match = re.fullmatch(r"\$\s*(\d+(?:\.\d+)?)", plain(text))
    return clean_price(match.group(1)) if match else None


@dataclass
class _Cell:
    text: str
    rowspan: int = 1
    colspan: int = 1


class _Tables(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[_Cell]]] = []
        self.depth = 0
        self.ignore = 0
        self.rows = []
        self.row = None
        self.cell = None
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "sup"):
            self.ignore += 1
        if tag == "table":
            self.depth += 1
            if self.depth == 1:
                self.rows = []
        if self.depth != 1 or self.ignore:
            return
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            attributes = dict(attrs)
            def span(name):
                try:
                    return min(100, max(1, int(attributes.get(name, "1"))))
                except (ValueError, TypeError):
                    return 1
            self.cell = _Cell("", span("rowspan"), span("colspan"))
            self.parts = []
        elif tag == "br" and self.cell is not None:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "sup") and self.ignore:
            self.ignore -= 1
        if self.depth == 1 and tag in ("td", "th") and self.cell is not None:
            self.cell.text = plain("".join(self.parts))
            self.row.append(self.cell)
            self.cell = None
        elif self.depth == 1 and tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        if tag == "table":
            if self.depth == 1:
                self.tables.append(self.rows)
            self.depth = max(0, self.depth - 1)

    def handle_data(self, data):
        if self.depth == 1 and not self.ignore and self.cell is not None:
            self.parts.append(data)


def html_tables(text: str) -> list[list[list[str]]]:
    """Expand row/column spans so every price stays under its own label."""
    parser = _Tables()
    parser.feed(text)
    tables = []
    for raw_rows in parser.tables:
        pending: dict[int, tuple[int, str]] = {}
        rows = []
        for raw_row in raw_rows:
            occupied = {column: value for column, (_, value) in pending.items()}
            pending = {column: (remaining - 1, value)
                       for column, (remaining, value) in pending.items() if remaining > 1}
            column = 0
            for cell in raw_row:
                while column in occupied:
                    column += 1
                for offset in range(cell.colspan):
                    index = column + offset
                    occupied[index] = cell.text
                    if cell.rowspan > 1:
                        pending[index] = (cell.rowspan - 1, cell.text)
                column += cell.colspan
            if occupied:
                rows.append([occupied.get(i, "") for i in range(max(occupied) + 1)])
        tables.append(rows)
    return tables


def mdx_tables(text: str) -> list[list[list[str]]]:
    """Read Kimi's literal DocTable data; expressions are never evaluated."""
    tables = []
    pattern = r"<DocTable\b\s+columns=\{\s*\[(.*?)\]\s*\}\s+rows=\{\s*(\[.*?\])\s*\}\s*/>"
    for match in re.finditer(pattern, text, re.S):
        headers = re.findall(r'title:\s*"([^"\n]+)"', match.group(1))
        rows_text = re.sub(r'<\>\{\s*"\$"\s*\}\s*(\d+(?:\.\d+)?)\s*</>',
                           r'"$\1"', match.group(2))
        rows_text = re.sub(r",\s*]", "]", rows_text)
        try:
            rows = json.loads(rows_text)
        except (ValueError, TypeError):
            continue
        if headers and isinstance(rows, list) and all(
            isinstance(row, list) and len(row) == len(headers)
            and all(isinstance(cell, str) for cell in row) for row in rows
        ):
            tables.append([headers, *rows])
    return tables


def price_columns(headers: list[str]) -> dict[str, int]:
    columns = {}
    for index, header in enumerate(headers):
        label = plain(header).lower()
        if "cache write" in label:
            # The database has one cache tier; Kimi documents 5min as default.
            if "ttl" not in label or "5min" in label.replace(" ", ""):
                columns["cache_write"] = index
        elif "cached input" in label or ("input" in label and "cache hit" in label):
            columns["cache_read"] = index
        elif "input" in label and ("cache miss" in label or "cache" not in label):
            columns["input"] = index
        elif "output" in label and "max" not in label:
            columns["output"] = index
    return columns
