# tokdash-pricing-updater

Layered pricing DB generator for [tokdash](https://github.com/JingbiaoMei/tokdash).
Scrapes model pricing from provider APIs, diffs against the current `pricing_db.json`,
and produces human-reviewed updates.

This is the generator behind Tokdash's `src/tokdash/pricing_db.json`. Tokdash is a
consumer: it ships the JSON and never the scraping logic.

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

CI mode. With `--create-pr` it fetches the base branch first and reads
`pricing_db.json` as of that commit, so the proposal is built against what is
actually published rather than against whatever the working tree happens to hold.
It then scrapes, diffs, applies the guard policy in
`config/anomaly_policy.yaml`, and publishes one PR. Everything the guards accept
goes into the PR diff, new models and repricings alike, so reviewing the diff and
merging is the whole job. Anything a guard distrusts is held out of the diff and
named in the PR body. A run whose guard findings point at the source rather than
at one price opens as a draft.

```bash
# Generate the proposal and its evidence, publish nothing.
python -m pricing_updater --tokdash-repo ../tokdash auto

# Plan the publication (fetch, branch, diff scope) without pushing.
python -m pricing_updater --tokdash-repo ../tokdash auto --create-pr --dry-run

# Publish to the bot branch and open or refresh one PR.
python -m pricing_updater --tokdash-repo ../tokdash auto --create-pr \
  --branch bot/pricing-db --base main --labels pricing,automated
```

Publication failure exits non-zero, so a run that could not open its PR does not
look green. `--dry-run` changes nothing at all, including the checked-out branch.

The four publication artifacts (`proposed_pricing_db.json`, `pr_body.md`,
`publish_plan.json`, `base_sha.txt`) are removed before generation rather than
written only on the success path, so a run that ends with nothing to propose leaves
nothing for a later job to publish.

Artifacts land in `output/proposals/`: the proposed database, the diff report,
`coverage_*.json`, `provenance_*.json`, `source_health_*.json` (which official
adapters answered and which fell back to YAML), `base_sha.txt` and
`publish_plan.json` (the commit the proposal was computed against, plus its title
and draft state, which publication refuses to move away from or reinvent) and the
rendered `pr_body.md`.

## Config reference

All config files live in `config/`. The scraper never modifies them — they are
the human-maintained source of truth for everything outside the scraper path.

### `anomaly_policy.yaml`

Guard thresholds for unattended runs. See [Change classification](#change-classification).
Every key is optional; the defaults in `guards.GuardPolicy` apply.

```yaml
max_changes_per_run: 25
max_single_field_change_pct: 50
min_openrouter_rows: 300
hold_explicit_zero: true
```

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

`diff.py` classifies each change for a human reader, and `guards.py` decides
whether it may enter the PR diff. Two vocabularies, because they answer different
questions.

| Classification | Meaning | `propose` behavior | `auto` behavior |
|---|---|---|---|
| `safe_add` | New model from a tracked provider | Default accept (Y), human confirms | Proposition candidate |
| `review_required` | Price changed on an existing model | Must explicitly accept | In the diff, reviewed in the PR |
| `manual_only` | Model absent for N consecutive scrapes | Shown, auto-skipped | Held; removals are never automatic |

Guards override classification. A change is **proposable** unless a guard holds it,
and the reason is always printed:

| Guard | Config key | Holds when |
|---|---|---|
| Volume | `max_changes_per_run` | The run wants more source-derived changes than this. A mass move is a scraper signal |
| Magnitude | `max_single_field_change_pct` | One field moves further than this |
| Source floor | `min_openrouter_rows` | OpenRouter returns fewer rows than this; the run is fatal |
| Explicit zero | `hold_explicit_zero` | A new entry prices input or output at zero |

Alias retargeting is always held: it changes what a name means. Alias additions are
exempt from the volume gate because they come from `config/aliases.yaml`, so their
count says nothing about whether a scraper broke.

Manual and derived entries are protected upstream and never appear as changes.

## Coverage

A run writes `output/proposals/coverage_<date>.json` with one row per
`pricing_db.json` key and its state: `add_proposed`, `reprice_proposed`,
`unchanged`, `held`, `protected_manual`, `protected_derived`, `unpriceable`, or
`not_discovered`.

That last state matters more than it looks. Official prices only overlay models
OpenRouter discovered, so a key no source lists can never receive a repricing
proposal, and nothing said so out loud before this file existed.

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

To get the same split CI makes, run the checker from this repo instead. It prints
which failures a repricing is allowed to cause and exits non-zero only for the
structural ones:

```bash
PYTHONPATH=src python -m pricing_updater.consumer_checks \
  --tokdash-repo ../tokdash \
  --proposed output/proposals/proposed_pricing_db.json \
  --json-out /tmp/consumer_checks.json
```

## Directory structure

```
tokdash-pricing-updater/
├── pyproject.toml              # Package metadata (httpx, pyyaml)
├── README.md                   # This file
├── .gitignore
├── .api_keys/                  # Gitignored local habits; use GH_TOKEN
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
│   ├── official_stepfun.py     # StepFun    (platform.stepfun.ai — live)
│   ├── canonicalize.py         # Source IDs → tokdash pricing_db keys
│   ├── policy.py               # Official overlay, FX, cache fallback, rounding
│   ├── render_tokdash_db.py    # Merge all layers → pricing_db.json
│   ├── diff.py                 # Diff engine, classification, PR body
│   ├── guards.py               # Proposable vs held, threshold policy
│   ├── coverage.py             # Which keys the bot can actually move
│   ├── approval.py             # Interactive + auto review
│   ├── github_pr.py            # Publish to one bot branch, open or refresh one PR
│   └── consumer_checks.py      # Split tokdash's checks into structural and price-expectation
├── state/
│   └── last_seen.json          # Gitignored; staleness tracking across runs
├── output/                     # Gitignored; all generated artifacts
│   ├── raw/                    # Raw API responses per scrape run
│   ├── canonical/              # Canonicalized snapshots per run
│   └── proposals/              # Proposed DB, diff report, coverage, provenance, pr_body
│                               # (override with PRICING_UPDATER_OUTPUT_DIR)
└── tests/
    ├── __init__.py
    ├── test_canonicalize.py
    ├── test_diff.py
    ├── test_model_coverage.py  # Fixture tests for named models
    ├── test_policy.py
    ├── test_render.py
    ├── test_guards.py
    ├── test_coverage.py
    ├── test_publish.py
    ├── test_price_integrity.py
    ├── test_auto_pipeline.py
    └── data/
        └── openrouter_fixture.json   # tracked fixture; no network, no skips
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

Two workflows live in `.github/workflows/`:

- `ci.yml` runs the suite on 3.10 and 3.12 and **fails on any skipped test**, because
  a clean clone used to skip ten of them silently when their fixture lived in
  gitignored `output/`.
- `pricing-update.yml` runs Tuesday and Thursday, plus on demand, as three jobs:
  **generate**, **validate** (tokdash's own pricing checks run against the proposed
  database before anything is pushed), and **publish**. All three work on one base
  commit: generate records the SHA it read, and validate and publish check that SHA
  out rather than a `main` that may have moved since. `publish` re-fetches and
  refuses outright if the base has moved by then, and refuses an artifact whose own
  plan names a different commit, so a proposal from another run cannot ride along.
  The `dry_run` dispatch input stops the run after validation and publishes nothing.

  Validation separates two kinds of consumer failure, because only one of them is a
  defect. `pricing_updater.consumer_checks` runs tokdash's checks twice: once with
  every price in the current database multiplied, once with the proposal. Failures
  the multiplication explains are price expectations, and those publish as a **draft**
  with the failing test names in the body, so the price change and the test update
  get reviewed together. Failures it does not explain are structural (an alias that
  stops resolving, a relationship between two fields that inverts) and nothing is
  published. Which test is which is measured rather than listed, so a new consumer
  test needs no edit here; a renamed or missing check file is an error, not a pass.

Publishing needs a credential that is authorized on the tokdash repo. The updater's
own `GITHUB_TOKEN` cannot write to another repository, so set `PRICING_BOT_TOKEN`
to a GitHub App installation token or a fine-grained PAT with `contents: write` and
`pull-requests: write` on `JingbiaoMei/Tokdash`. Scheduled runs only execute from the
default branch, can be delayed or coalesced, and are disabled after 60 days of repo
inactivity, which is why `workflow_dispatch` is a first-class path.

## What is not finished

Honest state as of 2026-10-11, measured by running `auto --create-pr --dry-run`
against the real tokdash checkout.

1. **The guard thresholds currently silence the bot.** The last run computed 91
   changes and proposed none of them: 45 were held only because the run exceeded
   `max_changes_per_run=25`, which holds the whole set, and 46 tripped the 50%
   single-field guard. On these settings a scheduled run is green and publishes
   nothing. Either the shipped `pricing_db.json` gets one manual rebalance first,
   or the thresholds get retuned. Deciding that is a policy call, not a bug.
2. **The bot cannot update tokdash's own price expectations.** A repricing that
   moves a number a consumer test pins now reaches a draft PR instead of being
   blocked, but the test edit is still a human's: the bot publishes one file,
   `pricing_db.json`, and refuses a wider diff by design.
3. **Source precedence is undecided (issue #202, D1).** The overlay takes the
   larger of OpenRouter and the official page per field, so a model can end up
   with fields from both sources and an aggregator price can beat the provider's
   own page. Nothing here pretends that is settled; the tests pin today's
   behaviour, not a ratified rule.
4. **Coverage is narrower than it looks.** Of 364 keys, 189 are movable by the
   bot, 71 were not discovered by the scraper at all and 13 are protected manual
   or derived entries. Official-only models and protected entries never receive a
   repricing proposal, so a price change on one of them still needs a human.
5. **Three of six official adapters are not answering.** The last run recorded
   DeepSeek parsing nothing and Moonshot and Xiaomi returning nothing, so those
   numbers came from `config/official_prices.yaml`. The run says so in
   `source_health_*.json` and in the PR body now, which is the fix for the
   invisibility; the adapters themselves still need work.
6. **No dependency lock.** CI resolves `httpx` and `pyyaml` from unpinned ranges
   on every run. `uv.lock` is gitignored, so nothing pins a transitive version.
7. **No watch-issue automation.** Held findings, unresolved evidence and scraper
   failures are reported in the PR body and in `output/proposals/`, and a run with
   nothing proposable leaves no trace on GitHub beyond its artifacts. Opening an
   issue for those is not built.
8. **Publication is inert until a token exists.** `PRICING_BOT_TOKEN` must be a
   credential authorized on the tokdash repo; without it the publish step skips
   with a warning by design.

## Design decisions

| Decision | Rationale |
|----------|-----------|
| Separate repo | Scraping, third-party-site adapters and guard policy belong to a generator, not to a consumer package. Tokdash ships only `pricing_db.json`. This repo commits no credentials; the publication token lives in CI secrets. |
| Dual-source: OpenRouter + official sites | OpenRouter provides broad coverage in one call. Official adapters (Z.ai, MiniMax, DeepSeek, StepFun live; Moonshot, Xiaomi YAML fallback) provide authoritative first-party prices. Per field, the official value wins when it is larger or when the scrape never settled the field. Whether official should simply win whenever both sources have a number is an open policy question (issue #202); today's `max()` is the status quo, not a ratified rule. |
| Publish only against a pinned commit | The base is fetched and read before generation, and publication is refused once that commit is no longer the tip. A changed-files guard cannot catch this: the stale content sits inside the one file this bot is allowed to touch, so publishing it would silently revert a merged edit. |
| Lease the force push against an observed commit | The bot branch is fetched and its SHA named in `--force-with-lease`, because a fresh CI clone has no tracking ref for it and git refuses the push otherwise with "stale info". |
| Reject prices that cannot be prices | Negative, NaN, infinite and unparseable rates never become a number. Guards ask whether a field is missing, so a value that merely parses looks valid, and the renderer is the last gate before JSON. |
| FX normalization | Official prices may be in CNY or USD. `config/fx_rates.yaml` holds the exchange rate; `policy.py` converts to USD before the max() comparison. Currently all tracked providers have USD pricing pages, but the machinery is ready for CNY sources. |
| Family mapping | Official providers list fewer model IDs than OpenRouter (e.g. DeepSeek lists 2, OpenRouter lists 11). `config/official_family_map.yaml` maps versioned variants to their official pricing family via glob patterns. |
| YAML fallback for SPA pages | Moonshot and Xiaomi official pages are JavaScript SPAs that don't serve pricing in initial HTML. Live adapters attempt to fetch them each run (will start working if SSR is added); YAML config provides the fallback. |
| Decimal arithmetic | OpenRouter returns per-token prices as strings (`"0.000005"`). Parsing with `Decimal` avoids floating-point drift during the `* 1,000,000` conversion to per-MTok. |
| Consumer checks are measured, not listed | Tokdash's consumer suite mixes "does this resolve" with "does this cost exactly 2.0". Multiplying every price and seeing what breaks identifies the second group, so a repricing publishes as a draft rather than being blocked forever by a list of test names that would rot. |
| One run owns the publication artifacts | `proposed_pricing_db.json`, `pr_body.md`, `publish_plan.json` and `base_sha.txt` are cleared before generation. A run that finds nothing to propose otherwise leaves yesterday's proposal next to today's pin, and the publish job reads those files by name. |
| A rerun can retract a PR's review status | Guards tripping on a rerun put an already-open PR back into draft with `gh pr ready --undo`, and a clean rerun takes a draft out. A proposal the bot no longer trusts should not sit one merge from main. |
| Adapters publish only what the page printed | The DeepSeek adapter used to fill an absent cache column with 10% of input, which put an invented number in the field reserved for first-party prices and read as a published zero on a free model. An absent field is left absent so the cache heuristic guesses visibly instead. |
| Official 0 is data, not a gap | A provider that publishes a free cache read means it. The overlay settles the field and leaves it flagged, so the cache heuristic never stamps a guessed factor over a real zero. |
| Manual/derived/alias configs | These represent tokdash's compatibility surface, not scraped data. Keeping them in YAML config files means the scraper can never accidentally remove or overwrite them. |
| Staleness gating (last_seen) | A model disappearing from one scrape could be a transient API issue. Requiring 4+ consecutive misses before even flagging prevents false removal proposals. |
| `manual_only` classification | Deletions are never auto-applied, not even in CI mode. A human must review and edit config files. |
| Consumer contract test in tokdash | After any update, `test_pricing_db_contract.py` in the tokdash repo verifies that aliases, manual models, derived models, and representative provider models still resolve through `PricingDatabase`. |
