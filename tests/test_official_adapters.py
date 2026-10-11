"""Regression coverage from current public provider pricing table excerpts."""

from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from pricing_updater.sources import official_deepseek, official_moonshot, official_xiaomi
from pricing_updater.sources._pricing_tables import usd_price

DATA = Path(__file__).parent / "data" / "official"


def _fixture(name):
    return (DATA / name).read_text()


def test_deepseek_current_models_peak_prices_and_explicit_legacy_aliases():
    models = {m.short_id: m for m in official_deepseek._try_parse_table(_fixture("deepseek_pricing.html"))}
    assert set(models) == {
        "deepseek-flash", "deepseek-v4.1-flash", "deepseek-v4-pro", "deepseek-v4-pro-0813",
        "deepseek-v4-flash", "deepseek-v4-flash-vision-exp",
    }
    flash = models["deepseek-flash"]
    assert flash.input_per_mtok == Decimal("0.3")
    assert flash.output_per_mtok == Decimal("1.2")
    assert flash.cache_read_per_mtok == Decimal("0.006")
    assert flash.cache_write_per_mtok is None
    assert models["deepseek-v4-pro"].input_per_mtok == Decimal("1.32")
    assert models["deepseek-v4-pro"].output_per_mtok == Decimal("3.96")
    assert models["deepseek-v4-pro"].cache_read_per_mtok == Decimal("0.044")
    assert models["deepseek-v4-flash"].input_per_mtok == flash.input_per_mtok


def test_deepseek_does_not_guess_prices_from_surrounding_prose():
    assert official_deepseek._try_parse_table("deepseek-chat input $2, output $8") == []


def test_deepseek_legacy_aliases_require_the_official_statement():
    document = _fixture("deepseek_pricing.html").split("<p>")[0]
    names = {m.short_id for m in official_deepseek._try_parse_table(document)}
    assert "deepseek-v4-flash" not in names
    assert "deepseek-chat" not in names
    assert "deepseek-r1" not in names


@pytest.mark.parametrize("unusable", ["$NaN", "$-0.3", "N/A"])
def test_deepseek_unusable_peak_does_not_fall_back_to_discount(unusable):
    document = _fixture("deepseek_pricing.html").replace("<td>$0.3</td>", f"<td>{unusable}</td>")
    names = {m.short_id for m in official_deepseek._try_parse_table(document)}
    assert "deepseek-flash" not in names
    assert "deepseek-v4-pro" in names


def test_deepseek_published_zero_stays_zero():
    document = _fixture("deepseek_pricing.html").replace("<td>$0.006</td>", "<td>$0</td>")
    models = {m.short_id: m for m in official_deepseek._try_parse_table(document)}
    assert models["deepseek-flash"].cache_read_per_mtok == 0


def test_deepseek_rejects_a_different_billing_unit():
    document = _fixture("deepseek_pricing.html").replace("1M", "1K")
    assert official_deepseek._try_parse_table(document) == []


def test_kimi_named_columns_keep_cache_input_and_output_separate():
    models = official_moonshot._parse_pricing(_fixture("kimi_pricing.mdx"))
    assert set(models) == {"kimi-k3", "kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6"}
    assert models["kimi-k2.7-code"]["cache_read"] == 0.19
    assert models["kimi-k2.7-code"]["input"] == 0.95
    assert models["kimi-k2.7-code"]["output"] == 4
    assert models["kimi-k2.7-code-highspeed"]["input"] == 1.9
    assert "cache_write" not in models["kimi-k2.7-code"]


def test_kimi_uses_published_default_five_minute_cache_tier():
    models = official_moonshot._parse_pricing(_fixture("kimi_pricing.mdx"))
    assert models["kimi-k3"]["cache_write"] == 3
    assert models["kimi-k3"]["cache_read"] == 0.3
    assert models["kimi-k3"]["input"] == 3
    assert models["kimi-k3"]["output"] == 15


def test_kimi_rejects_a_different_billing_unit():
    document = _fixture("kimi_pricing.mdx").replace("1M tokens", "1K tokens")
    assert official_moonshot._parse_pricing(document) == {}


def test_kimi_does_not_execute_mdx_expressions():
    document = _fixture("kimi_pricing.mdx").replace('<>{"$"}0.19</>', 'globalThis.stealSecrets()')
    models = official_moonshot._parse_pricing(document)
    assert set(models) == {"kimi-k3"}


def test_kimi_published_zero_is_not_discarded():
    document = _fixture("kimi_pricing.mdx").replace('<>{"$"}0.19</>', '<>{"$"}0</>')
    assert official_moonshot._parse_pricing(document)["kimi-k2.7-code"]["cache_read"] == 0


def test_xiaomi_uses_usd_realtime_rows_and_expands_shared_prices():
    models = official_xiaomi._parse_pricing(_fixture("xiaomi_pricing.md"))
    assert set(models) == {
        "mimo-v2.6-pro", "mimo-v2.5-pro", "mimo-v2.6-flash", "mimo-v2.5", "mimo-v2.6-pro-ultraspeed",
    }
    assert models["mimo-v2.6-pro"]["input"] == 0.435
    assert models["mimo-v2.6-pro"]["output"] == 0.87
    assert models["mimo-v2.6-pro"]["cache_read"] == 0.0036
    assert models["mimo-v2.5-pro"]["input"] == 0.435
    assert models["mimo-v2.5"]["input"] == 0.14
    assert models["mimo-v2.6-pro-ultraspeed"]["input"] == 4.35
    assert all(row["currency"] == "USD" for row in models.values())
    assert all(row["cache_write"] == 0 for row in models.values())


def test_xiaomi_free_cache_write_requires_the_published_tariff():
    document = _fixture("xiaomi_pricing.md").replace("Cache Write: Limited-time Free", "")
    models = official_xiaomi._parse_pricing(document)
    assert models and all("cache_write" not in row for row in models.values())


def test_xiaomi_cny_tables_cannot_be_labelled_usd():
    document = _fixture("xiaomi_pricing.md").replace("$", "¥")
    assert official_xiaomi._parse_pricing(document) == {}


def test_xiaomi_rejects_a_different_billing_unit():
    document = _fixture("xiaomi_pricing.md").replace("dollar / M tokens", "dollar / K tokens")
    assert official_xiaomi._parse_pricing(document) == {}


@pytest.mark.parametrize("text", ["$-1", "$NaN", "$Infinity", "¥3.00", "256K", "$0.5 /h", "", "N/A"])
def test_non_token_prices_and_invalid_values_are_not_usd_prices(text):
    assert usd_price(text) is None


class _Response:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


@pytest.mark.parametrize("module,fixture,expected", [
    (official_deepseek, "deepseek_pricing.html", 6),
    (official_moonshot, "kimi_pricing.mdx", 4),
    (official_xiaomi, "xiaomi_pricing.md", 5),
])
def test_fetchers_use_current_public_documents(monkeypatch, module, fixture, expected):
    requested = []
    def get(url, **kwargs):
        requested.append(url)
        assert kwargs["follow_redirects"] is True
        return _Response(_fixture(fixture))
    monkeypatch.setattr(module.httpx, "get", get)
    fetcher = getattr(module, next(name for name in dir(module) if name.startswith("fetch_")))
    assert len(fetcher(timeout=7)) == expected
    assert requested
    if module is official_moonshot:
        assert requested == [module.MOONSHOT_PRICING_DOCUMENT_URL]
    elif module is official_xiaomi:
        assert requested == [module.XIAOMI_PRICING_URL]


def test_kimi_can_fall_back_to_rendered_html_when_markdown_is_unavailable(monkeypatch):
    html = ('<table><tr><th>Model</th><th>Unit</th><th>Input Price</th><th>Output Price</th></tr>'
            '<tr><td>kimi-k3</td><td>1M tokens</td><td>$3</td><td>$15</td></tr></table>')
    def get(url, **kwargs):
        if url.endswith(".md"):
            raise httpx.HTTPError("markdown unavailable")
        return _Response(html)
    monkeypatch.setattr(official_moonshot.httpx, "get", get)
    assert official_moonshot.fetch_moonshot_official()["kimi-k3"]["output"] == 15
