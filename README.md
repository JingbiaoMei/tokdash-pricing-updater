# tokdash-pricing-updater

Layered pricing DB generator for [tokdash](https://github.com/JingbiaoMei/tokdash).
Scrapes model pricing from provider APIs, diffs against the current `pricing_db.json`,
and produces human-reviewed updates.

This is a **private companion repo**. Tokdash itself remains open-source and only
consumes the generated JSON.

## Why a separate repo?

Tokdash's `pricing_db.json` is not a direct mirror of any provider's model list.
It is a **compatibility layer** used by `PricingDatabase` at runtime, containing:

- **Scraped models** from OpenRouter (the majority).
- **Manual models** that don't exist on any source (`gpt-5.3-codex`).
- **Derived models** whose pricing copies a base model (`antigravity-*`, `k2p5`).
- **Aliases** that map variant names to canonical keys for runtime lookup.

A naive scraper that mirrors a source directly would generate false removals and
break existing lookups. This updater uses a layered pipeline that keeps each
concern separate.

## Architecture

```
 OpenRouter API              Official provider sites
 sources/openrouter.py       sources/official_*.py
       │                            │
       │                     ┌──────┴──────────────────┐
       │                     │ Z.ai   docs.z.ai        │ live
       │                     │ MiniMax platform.minimax │ live
       │                     │ Moonshot (SPA fallback)  │ YAML
       │                     │ Xiaomi  (SPA fallback)   │ YAML
       │                     │ DeepSeek api-docs        │ live
       │                     │ Anthropic (matches OR)   │ YAML
       │                     └──────┬──────────────────┘
       │                            │
Layer 1: canonicalize.py            │
    Map source IDs to tokdash       │
    pricing_db keys.                │
       │                            │
Layer 2: policy.py ─────────────────┘
    a. Official overlay: resolve family map
       (config/official_family_map.yaml),
       normalize currency (config/fx_rates.yaml),
       apply max(openrouter, official) per field.
    b. Cache fallback: fill missing cache fields
       with per-provider heuristics.
    c. Round prices.
       │
Layer 3: render_tokdash_db.py
    Merge scraped + manual_models.yaml
    + derived_models.yaml + aliases.yaml
    → complete pricing_db.json.
       │
Layer 4: diff.py + approval.py
    Classify: safe_add / review_required /
    manual_only.  Gate removals via last_seen.
       │
       ▼
    output/proposals/proposed_pricing_db.json
```

## Quick start

```bash
# Clone next to your tokdash checkout.
cd ~/Projects
git clone <this-repo> tokdash-pricing-updater
cd tokdash-pricing-updater

# Create a dedicated conda env.
conda create -n pricing-updater python=3.12 -y
conda activate pricing-updater
pip install -e .

# See what changed since the last update.
python -m pricing_updater --tokdash-repo ../tokdash diff
```

## Typical workflow

```bash
conda activate pricing-updater
cd ~/Projects/tokdash-pricing-updater

# 1. Interactive review: scrape, diff, approve/reject each change.
python -m pricing_updater --tokdash-repo ../tokdash propose

# 2. Copy the approved result into tokdash.
cp output/proposals/proposed_pricing_db.json ../tokdash/src/tokdash/pricing_db.json

# 3. Verify nothing broke.
cd ../tokdash
python -m pytest tests/test_pricing_db_contract.py -v

# 4. Commit in tokdash when satisfied.
git add src/tokdash/pricing_db.json
git commit -m "Update pricing DB (YYYY-MM-DD)"
```

For non-interactive runs that only add new models (no price changes applied):

```bash
python -m pricing_updater --tokdash-repo ../tokdash auto
cp output/proposals/proposed_pricing_db.json ../tokdash/src/tokdash/pricing_db.json
```

### Dependencies

| Package | Purpose |
|---------|---------|
| `httpx` | HTTP client for OpenRouter API |
| `pyyaml` | Config file loading |

No other runtime dependencies. Python standard library is used for everything
else (`decimal`, `json`, `argparse`, `pathlib`, `re`, `dataclasses`).

## CLI reference

All commands can be invoked as `python -m pricing_updater` or, after
`pip install -e .`, as `pricing-updater` directly.

All commands accept `--tokdash-repo PATH` before the subcommand (default:
`../tokdash` relative to this project root).

### `scrape`

Fetch from OpenRouter, save raw API response and canonical snapshot.

```bash
python -m pricing_updater scrape
```

**Outputs:**
- `output/raw/openrouter_YYYY-MM-DD.json` — full API response.
- `output/canonical/canonical_YYYY-MM-DD.json` — filtered, canonicalized, policy-applied snapshot.

No comparison or approval; useful for inspecting what the source provides.

### `diff`

Scrape, render a complete proposed DB, and diff against the current tokdash
`pricing_db.json`. Prints a report and saves it.

```bash
python -m pricing_updater --tokdash-repo ../tokdash diff
```

**Outputs:**
- `output/proposals/diff_YYYY-MM-DD.md` — human-readable diff report.
- `state/last_seen.json` — updated model visibility tracking.

### `propose`

Full interactive pipeline. After diffing, walks through each change and asks
you to accept or reject it. Produces a ready-to-copy `pricing_db.json`.

```bash
python -m pricing_updater --tokdash-repo ../tokdash propose
```

**Interactive prompts:**
```
[1/87]   [ADD ] gpt-5.4-pro (openai): $30.0/$180.0 per MTok
  Accept? [Y/n/s(kip rest)/q(uit)] (default=Y):

[2/87]   [CHG!] glm-4.5:
           input: $0.35 -> $0.6
  Accept? [Y/n/s(kip rest)/q(uit)] (default=?):

[3/87]   [STAL] text-embedding-3-small: not seen for 4 consecutive scrapes
  (manual_only — skipped. Edit config files to handle this.)
```

- **`Y` / Enter** — accept the change.
- **`n`** — reject this change.
- **`s`** — skip all remaining changes (already-approved ones are kept).
- **`q`** — quit review entirely (already-approved ones are kept).
- **`manual_only`** changes are shown but automatically skipped.

**Outputs:**
- `output/proposals/proposed_pricing_db.json` — ready to copy.

After review:
```bash
cp output/proposals/proposed_pricing_db.json ../tokdash/src/tokdash/pricing_db.json
```

### `auto`

CI / non-interactive mode. Automatically approves `safe_add` changes only.
Price changes and stale models are included in the diff report but **not
applied** — they require human review.

```bash
# Just generate the proposal.
python -m pricing_updater --tokdash-repo ../tokdash auto

# Also open a PR against tokdash (requires gh CLI authenticated).
python -m pricing_updater --tokdash-repo ../tokdash auto --create-pr

# Dry run: print git/gh commands without executing them.
python -m pricing_updater --tokdash-repo ../tokdash auto --create-pr --dry-run
```

## Config reference

All config files live in `config/`. The scraper never modifies them — they are
the human-maintained source of truth for everything outside the scraper path.

### `tracked_providers.yaml`

Controls which OpenRouter provider slugs are scraped.

```yaml
providers:
  - slug: openai           # OpenRouter's provider slug
    tokdash_provider: openai   # "provider" field in pricing_db.json
  - slug: anthropic
    tokdash_provider: anthropic
  - slug: google
    tokdash_provider: google
  - slug: moonshotai
    tokdash_provider: moonshotai
  - slug: minimax
    tokdash_provider: minimax
  - slug: z-ai
    tokdash_provider: z-ai
  - slug: deepseek
    tokdash_provider: deepseek
  - slug: xiaomi
    tokdash_provider: xiaomi

skip_patterns:             # Regex patterns to skip matching model IDs.
  - ":free$"               # Community/free endpoints
  - ":beta$"
  - ":extended$"
```

**To add a new provider**, append a `{slug, tokdash_provider}` entry and add a
corresponding section in `pricing_policy.yaml`'s `provider_order`.

### `aliases.yaml`

Written verbatim into `pricing_db.json["aliases"]`. Maps variant model names to
canonical pricing_db keys so that tokdash's `PricingDatabase._resolve_pricing()`
can look them up at runtime.

```yaml
aliases:
  "kimi-2.5": "kimi-k2.5"
  "vol-engine/kimi-2.5": "kimi-k2.5"
```

The scraper never touches aliases. Edit this file when you learn of new naming
variants that tools use.

### `manual_models.yaml`

Models maintained by hand that do not exist on any scraped source. These are
**never auto-removed** and are merged into the final `pricing_db.json` during
rendering.

```yaml
models:
  gpt-5.3-codex:
    provider: openai
    input: 1.75
    output: 14.00
    cache_read: 0.175
    cache_write: 1.75
    unit: per_million_tokens
    note: "Uses GPT-5.2-Codex pricing (model doesn't exist on OpenRouter)"
```

Use this for:
- Models that a coding tool reports but that have no public pricing page.
- Explicit alias entries that must exist as separate pricing_db keys (e.g. `k2p5`).

### `derived_models.yaml`

Models whose pricing is copied from another model at render time. The `derive_from`
field references a canonical model ID.

```yaml
models:
  antigravity-claude-sonnet-4-6:
    derive_from: claude-sonnet-4.6
    provider: google
    note: "Antigravity not on OpenRouter. Using Claude Sonnet 4.6 pricing."
```

If the base model's pricing changes (via scraping), derived models automatically
pick up the new values on the next render. Derived models are **never flagged**
as stale or removed.

### `pricing_policy.yaml`

Controls the policy layer behavior.

| Section | Purpose | Status |
|---------|---------|--------|
| `cache_fallback` | Per-provider heuristics when a source omits cache pricing. `cache_read_factor` and `cache_write_factor` are multiplied by the input rate. | Active |
| `rounding` | Maximum decimal places for per-MTok prices (default: 6). | Active |
| `removal.min_consecutive_misses` | How many consecutive scrape runs a model must be absent before it is even flagged as stale (default: 4). Stale models are always `manual_only`. | Active |
| `provider_order` | Ordering of section comments and provider groups in the rendered `pricing_db.json`. | Active |
| `field_mapping` | Documents how OpenRouter pricing field names map to tokdash field names. Currently informational — the mapping is hardcoded in `sources/openrouter.py`. | Informational |
| `context_tier_preference` | Documents which pricing tier to prefer when a provider offers multiple (e.g. Anthropic <=200K vs >200K). Not yet enforced in code — the source returns whichever tier OpenRouter defaults to. | Planned |

#### Cache fallback heuristics

These apply **only** when OpenRouter does not provide `input_cache_read` or
`input_cache_write` for a model. Most models on OpenRouter now include cache
fields, so the fallback is rarely used.

| Provider | `cache_read_factor` | `cache_write_factor` | Rationale |
|----------|--------------------:|---------------------:|-----------|
| openai | 0.50 | 1.00 | OpenAI caches at 50% of input |
| anthropic | 0.10 | 1.25 | Anthropic: 10% read, 125% write |
| google | 0.25 | 1.00 | Gemini cache pricing |
| default | 0.10 | 1.00 | Conservative fallback (used by moonshotai, minimax, z-ai, and any provider without an explicit entry) |

## Change classification

Every detected change is assigned one of three classifications that determine
how it flows through the approval process:

| Classification | Meaning | `propose` behavior | `auto` behavior |
|---|---|---|---|
| `safe_add` | New model from a tracked provider | Default accept (Y), human confirms | Auto-accepted |
| `review_required` | Price changed on an existing model | Must explicitly accept | **Not applied** — flagged in PR body |
| `manual_only` | Model absent for N consecutive scrapes | Shown, auto-skipped | **Not applied** — flagged in PR body |

Deletions are **never automatic**. A model must be absent from all scraped
sources for at least `min_consecutive_misses` (default 4) consecutive runs
before it is even shown as stale, and even then the classification is
`manual_only`.

## Staleness gating

`state/last_seen.json` tracks per-model visibility across scraping runs:

```json
{
  "gpt-5.4": {
    "last_seen": "2026-03-15",
    "first_seen": "2026-03-01",
    "consecutive_misses": 0
  },
  "text-embedding-3-small": {
    "last_seen": "2026-02-01",
    "first_seen": "2025-12-15",
    "consecutive_misses": 6
  }
}
```

- Each successful scrape resets `consecutive_misses` to 0 and updates `last_seen`.
- Each scrape where a model is absent increments `consecutive_misses` by 1.
- The model is flagged as `manual_only` only when `consecutive_misses >= min_consecutive_misses`.
- Manual and derived models are never tracked for staleness.

This file is gitignored and accumulates state across runs on your machine.

## How-to guides

### Add a new provider to track

1. Find the provider's slug on OpenRouter (e.g. `deepseek`).
2. Add to `config/tracked_providers.yaml`:
   ```yaml
   - slug: deepseek
     tokdash_provider: deepseek
   ```
3. Add a section to `config/pricing_policy.yaml` `provider_order`:
   ```yaml
   - key: _comment_deepseek
     label: "=== DeepSeek Models ==="
     providers: [deepseek]
   ```
4. Optionally add cache fallback heuristics if DeepSeek models lack cache fields.
5. Run `python -m pricing_updater --tokdash-repo ../tokdash diff` to verify.

### Add a manual model

Edit `config/manual_models.yaml`:
```yaml
models:
  my-custom-model:
    provider: custom
    input: 1.00
    output: 5.00
    cache_read: 0.10
    cache_write: 1.00
    unit: per_million_tokens
    note: "Custom model, pricing set manually"
```

### Add a derived (antigravity-style) model

Edit `config/derived_models.yaml`:
```yaml
models:
  antigravity-new-model:
    derive_from: claude-sonnet-4.6    # must be a scraped or manual model ID
    provider: google
    note: "Antigravity not on OpenRouter. Using Claude Sonnet 4.6 pricing."
```

### Add or update an alias

Edit `config/aliases.yaml`:
```yaml
aliases:
  "new-variant-name": "canonical-model-id"
```

### Verify updates don't break tokdash

After copying a proposed `pricing_db.json` into tokdash, run the consumer
contract test:

```bash
cd ../tokdash
python -m pytest tests/test_pricing_db_contract.py -v
```

This test asserts that:
- Manual models resolve (`gpt-5.3-codex`, `k2p5`).
- Alias entries resolve and match the base model's pricing.
- Derived (antigravity) models resolve and match their base model's pricing.
- At least one model per tracked provider resolves.

## Directory structure

```
tokdash-pricing-updater/
├── pyproject.toml              # Package metadata (httpx, pyyaml)
├── README.md                   # This file
├── .gitignore
├── .api_keys/                  # Gitignored; for gh tokens if needed
├── config/
│   ├── tracked_providers.yaml  # Which OpenRouter providers to scrape
│   ├── aliases.yaml            # pricing_db.json alias section (manual)
│   ├── manual_models.yaml      # Models not on any source (manual)
│   ├── derived_models.yaml     # Models derived from base pricing (manual)
│   ├── pricing_policy.yaml     # Cache heuristics, rounding, removal gating,
│   │                           #   provider ordering, field mapping
│   ├── official_prices.yaml    # Official USD prices (YAML fallback)
│   ├── official_family_map.yaml # Versioned model → family price mapping
│   └── fx_rates.yaml           # CNY→USD exchange rate
├── src/pricing_updater/
│   ├── __init__.py
│   ├── __main__.py             # python -m pricing_updater
│   ├── cli.py                  # CLI entry point, pipeline orchestration
│   ├── sources/
│   │   ├── __init__.py
│   │   ├── _html_table.py      # Shared HTML <table> parser
│   │   ├── base.py             # ScrapedModel dataclass
│   │   ├── openrouter.py       # OpenRouter /api/v1/models scraper
│   │   ├── official_fetch.py   # Orchestrator for all official adapters
│   │   ├── official_zai.py     # Z.ai/GLM  (docs.z.ai — live)
│   │   ├── official_minimax.py # MiniMax    (platform.minimax.io — live)
│   │   ├── official_moonshot.py# Moonshot   (SPA — falls back to YAML)
│   │   ├── official_xiaomi.py  # Xiaomi     (SPA — falls back to YAML)
│   │   └── official_deepseek.py# DeepSeek   (api-docs.deepseek.com — live)
│   ├── canonicalize.py         # Source IDs → tokdash pricing_db keys
│   ├── policy.py               # Official overlay, FX, cache fallback, rounding
│   ├── render_tokdash_db.py    # Merge all layers → pricing_db.json
│   ├── diff.py                 # Diff engine, change classification
│   ├── approval.py             # Interactive + auto review
│   └── github_pr.py            # PR creation via gh CLI
├── state/
│   └── last_seen.json          # Gitignored; staleness tracking across runs
├── output/                     # Gitignored; all generated artifacts
│   ├── raw/                    # Raw API responses per scrape run
│   ├── canonical/              # Canonicalized snapshots per run
│   └── proposals/              # Diff reports + proposed pricing_db.json
└── tests/
    ├── __init__.py
    ├── test_canonicalize.py
    ├── test_diff.py
    ├── test_model_coverage.py  # Fixture tests for named models
    ├── test_policy.py
    └── test_render.py
```

## Testing

```bash
# Unit tests (no network, no tokdash dependency).
python -m pytest tests/ -v

# Consumer contract test (requires tokdash checkout at ../tokdash).
cd ../tokdash
python -m pytest tests/test_pricing_db_contract.py -v
```

## CI integration (GitHub Actions)

Example workflow for the updater's own repo, running weekly:

```yaml
name: Pricing Update Check
on:
  schedule:
    - cron: '0 8 * * 1'   # Every Monday 08:00 UTC
  workflow_dispatch:

jobs:
  check-pricing:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/checkout@v4
        with:
          repository: JingbiaoMei/tokdash
          path: tokdash

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - run: pip install -e .

      - name: Scrape, diff, and open PR if safe_add changes found
        env:
          GH_TOKEN: ${{ secrets.TOKDASH_PR_TOKEN }}
        run: python -m pricing_updater --tokdash-repo tokdash auto --create-pr
```

The `auto --create-pr` command only creates a PR when there are approved
`safe_add` changes. If there are none, it exits cleanly.

## Design decisions

| Decision | Rationale |
|----------|-----------|
| Separate private repo | Keeps scraping logic and API keys out of the open-source tokdash repo. Tokdash only consumes `pricing_db.json`. |
| Dual-source: OpenRouter + official sites | OpenRouter provides broad coverage (124 models, 8 providers) in one call. Official adapters (Z.ai, MiniMax, DeepSeek live; Moonshot, Xiaomi YAML fallback) provide authoritative first-party prices. `max(openrouter, official)` per field ensures conservative cost tracking. |
| FX normalization | Official prices may be in CNY or USD. `config/fx_rates.yaml` holds the exchange rate; `policy.py` converts to USD before the max() comparison. Currently all tracked providers have USD pricing pages, but the machinery is ready for CNY sources. |
| Family mapping | Official providers list fewer model IDs than OpenRouter (e.g. DeepSeek lists 2, OpenRouter lists 11). `config/official_family_map.yaml` maps versioned variants to their official pricing family via glob patterns. |
| YAML fallback for SPA pages | Moonshot and Xiaomi official pages are JavaScript SPAs that don't serve pricing in initial HTML. Live adapters attempt to fetch them each run (will start working if SSR is added); YAML config provides the fallback. |
| Decimal arithmetic | OpenRouter returns per-token prices as strings (`"0.000005"`). Parsing with `Decimal` avoids floating-point drift during the `* 1,000,000` conversion to per-MTok. |
| Manual/derived/alias configs | These represent tokdash's compatibility surface, not scraped data. Keeping them in YAML config files means the scraper can never accidentally remove or overwrite them. |
| Staleness gating (last_seen) | A model disappearing from one scrape could be a transient API issue. Requiring 4+ consecutive misses before even flagging prevents false removal proposals. |
| `manual_only` classification | Deletions are never auto-applied, not even in CI mode. A human must review and edit config files. |
| Consumer contract test in tokdash | After any update, `test_pricing_db_contract.py` in the tokdash repo verifies that aliases, manual models, derived models, and representative provider models still resolve through `PricingDatabase`. |
