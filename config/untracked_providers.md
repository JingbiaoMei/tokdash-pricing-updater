# Untracked OpenRouter providers

Last reviewed: 2026-08-04

OpenRouter serves provider slugs that `tracked_providers.yaml` deliberately does
**not** scrape. This file records them so the omission is intentional and
discoverable, not an oversight. Add one to `tracked_providers.yaml` (and a
matching section to `provider_order` in `pricing_policy.yaml`) only when tokdash
users actually route usage through it.

## Tilde (`~`) reroute variants

Slugs prefixed with `~` (e.g. `~anthropic`, `~openai`, `~google`, `~deepseek`,
`~x-ai`, `~moonshotai`) are OpenRouter internal reroutes of providers we already
track under their canonical slug. They are ignored on purpose - tracking them
would double-count the same models.

## Deliberately untracked slugs

Snapshot of OpenRouter model counts at last review:

| Slug | Models | Notes |
|------|-------|-------|
| openrouter | 6 | OpenRouter's own auto-routed/superset entries, not a real provider |
| nousresearch | 4 | Community models (Hermes, etc.) |
| thedrummer | 4 | Community roleplay finetunes |
| aion-labs | 4 | Niche |
| inclusionai | 4 | Niche |
| poolside | 4 | Niche |
| sao10k | 3 | Community finetunes |
| kwaipilot | 3 | Niche |
| arcee-ai | 2 | Niche |
| ibm-granite | 2 | IBM Granite |
| morph | 2 | Niche |
| nex-agi | 2 | Niche |
| relace | 2 | Niche |
| thinkingmachines | 2 | Niche |
| allenai | 1 | Research (OLMo) |
| anthracite-org | 1 | Community finetune |
| cognitivecomputations | 1 | Community GGUF host |
| deepcogito | 1 | Niche |
| gryphe | 1 | Community roleplay finetune |
| inception | 1 | Niche |
| mancer | 1 | Community host |
| perceptron | 1 | Niche |
| sakana | 1 | Research |
| undi95 | 1 | Community finetune |
| upstage | 1 | Niche |
| writer | 1 | Niche |

## Manually-maintained models not on OpenRouter

Some models are added directly to the consumer `pricing_db.json` because they are
not (yet) listed on OpenRouter, so the scraper cannot see them:

- `doubao-seed-2.1-turbo` (canonical `seed-2.1-turbo`) - added 2026-08-04 from the
  official Volcengine Ark price (CNY 3.0/15.0/0.6 in/out/cache_read, tier input
  length [0,256]) converted at 0.137 USD/CNY; cache_write=input. Tracked via
  `manual_models.yaml` (not on OpenRouter). NOTE: only the short-context [0,256]
  tier is priced; longer-context tiers may exist - update if surfaced.
- `longcat-2.0` - added 2026-08-04 from OpenRouter (`meituan/longcat-2.0`), now
  also tracked via the `meituan` provider slug.

When a manual model later appears on OpenRouter, prefer adding its provider slug
to `tracked_providers.yaml` so the scraper becomes the source of truth.
