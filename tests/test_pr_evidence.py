"""The PR body must name the source that actually settled each number.

The Source column is the whole reason a reviewer can check a pricing PR without
rerunning the scraper. Crediting OpenRouter for a price that came from the
provider's own page sends them to the wrong URL, so these tests pin the
provenance sidecar being read rather than the entry's own source field.
"""

from pricing_updater.diff import generate_pr_body


class _Change:
    def __init__(self, model_id, change_type, old_entry, new_entry, fields):
        self.model_id = model_id
        self.change_type = change_type
        self.old_entry = old_entry
        self.new_entry = new_entry
        self.fields_changed = fields
        self.classification = "repricing"


class _Decision:
    def __init__(self, change, proposable=True, reasons=None):
        self.change = change
        self.proposable = proposable
        self.reasons = reasons or []


# openrouter listed 3/8, the official page published 1/2, and the official page
# won both fields on the max() rule only because ... it did not; it won because
# the scrape never filled these fields at all.
_PROV = {
    "model-a": {
        "field_sources": {"input": "official:price.example.com", "output": "openrouter"},
        "evidence": {
            "input": {"openrouter": 3.0, "official_usd": 1.0},
            "output": {"openrouter": 8.0},
        },
    },
}


def _body():
    change = _Change(
        "model-a", "price_change",
        {"input": 0.5, "output": 1.0}, {"input": 1.0, "output": 8.0}, ["input", "output"],
    )
    return generate_pr_body([_Decision(change)], meta={"provenance": _PROV})


def test_repricing_row_names_the_source_that_won_the_field():
    body = _body()
    assert "official:price.example.com" in body


def test_repricing_row_shows_the_price_the_other_source_listed():
    body = _body()
    assert "$3" in body


def test_a_field_the_scrape_won_does_not_credit_the_official_page():
    body = _body()
    row = [line for line in body.splitlines() if "output" in line and line.startswith("| `model-a`")]
    assert row and "openrouter" in row[0]
    assert "official:" not in row[0]


def test_a_pipe_in_a_source_label_cannot_break_the_table():
    change = _Change(
        "model-b", "price_change", {"input": 1.0}, {"input": 2.0}, ["input"],
    )
    prov = {"model-b": {"field_sources": {"input": "official:a|b"}, "evidence": {}}}
    body = generate_pr_body([_Decision(change)], meta={"provenance": prov})
    row = [line for line in body.splitlines() if line.startswith("| `model-b`")][0]
    # Five columns, so five cell edges plus one escaped pipe inside the label.
    assert row.count("|") == 7
    assert "official:a\\|b" in row
