# haql-qc: Pricing Analysis

Written 1 October 2026. Every market figure below is sourced. Every recommendation is labelled as a recommendation, because a recommendation is not a fact.

---

## The finding that shapes everything else

**No oil and gas production data quality tool publishes its pricing.** Not one.

Enverus, TGS, Schlumberger Seequent, Geosoftware, C&C Reservoirs, QRI Group, Dynagraph, Black Gold Analytics — every one is "request a demo" or "contact sales." The closest direct comparable, Black Gold Analytics, sells a focused tool for validating oil and gas production revenue against check stubs, markets ROI statistics, and shows no price anywhere.

That is not evidence that a good price exists. It is evidence that this market buys through conversations, not shopping carts.

So the honest position: **there is no verifiable market price for this product.** Anyone who quotes you one is guessing, including me. What follows is anchoring, not verification.

## Market anchors

### The labour substitution price — the one that matters

This is the number a buyer actually compares against, because the alternative to buying software is doing the work by hand.

| Reference | Price | Source |
| --- | --- | --- |
| US Reservoir Engineer III | $182,149/yr, $87.57/hr | Salary.com, July 2026 |
| US Reservoir Engineer V (senior) | $268,070/yr, $128.88/hr | Salary.com, July 2026 |
| Petroleum Engineer, Canada | $173,866/yr, $83.59/hr | ERI, Sept 2026 |
| Subsurface data consulting | $125–150/hr data loading | GeoTech published pricelist |
| Subsurface training | $180/hr | GeoTech published pricelist |
| Support hours (maintenance) | $150/hr ($1,500 per 10 hrs) | GeoTech published pricelist |

**This is the anchor.** An engineer at $87–130/hr who spends four hours a month eyeballing production exports for anomalies is costing $350–520/month. That is the budget haql-qc competes for, and it is a much more defensible number than any SaaS tier.

### The enterprise floor

| Reference | Price | Source |
| --- | --- | --- |
| Enverus — small-account billing threshold | Accounts under $10,000/yr | Enverus Main Subscription Agreement |

Enverus's own contract treats anything under $10,000/year as a separate, administratively awkward population. That is the closest thing to a published small-deal floor in E&P subsurface software. It implies a real budget exists somewhere in the low five figures for small operators.

### Adjacent data-quality SaaS

Different market, different buyer, but the only public price points that exist for "data quality tool as software."

| Product | Price | Source |
| --- | --- | --- |
| Sohovi Pro / Team | $39 / $99 per month | sohovi.com/pricing |
| DQOps Personal / Team | $600 / $2,000 per month | dqops.com/pricing |
| QuerySurge full user | $6,006/yr subscription ($501/mo) | querysurge.com pricing |
| EarthSoft EQuIS Data Processor | $75/mo + $1,500 perpetual, per concurrent user | EarthSoft pricelist PDF |
| EarthSoft EQuIS Data Processor, distribution | $300/mo + $8,000 perpetual | EarthSoft pricelist PDF |
| EarthSoft EQuIS Collect | $30/user/month | EarthSoft pricelist PDF |
| EnergiQ Explorer | $5/month | getenergiq.com |

Two observations. First, EarthSoft is the closest structural analog — a decades-old data-processing tool for environmental compliance that charges per concurrent user plus a distribution tier. Second, EnergiQ at $5/month shows what happens when a production-data tool prices itself like a consumer app: it becomes a research tool, not a procurement line item.

### What nobody publishes

Reserves audit and technical due diligence fees. Netherland Sewell, Ryder Scott, Cegal, Integra Subsurface, BRE Subsurface — all quote privately. Wood Mackenzie sells its upstream briefing report at $1,350, which is the only hard number I found for "an independent expert tells you something about an upstream asset."

---

## Recommended monthly pricing

Recommendations only. No customer has been asked, so these are unvalidated.

### Tier 1 — Evaluation

**$0**, unlimited wells, full ruleset, watermarked output.

The purpose is not revenue. It is getting a reservoir engineer to run it against a real export and hit one finding they did not know about. That moment is the entire sale. Once they have seen it, a price is easy; before, no price registers.

Do not put a free tier on a seat-count limit. The engineer's first run is seven wells; seat limits solve a problem you do not have.

### Tier 2 — Team

**$349/month**, annual commitment, up to 10 users, unlimited fields.

Rationale: sits below the Enverus $10K/yr ($833/mo) floor, well below DQOps at $2,000/mo, and roughly one engineer's day of loaded cost. Cheap enough that procurement does not need a committee, expensive enough that a serious operator takes the commitment seriously.

Volume discount only above ~50 users, and only if it ever happens. Chasing seat volume before you have one paying logo is how you end up with pricing nobody will pay.

### Tier 3 — On-premise with support

**$1,750/month**, annual, per site. Unlimited users on that site.

On-premise is not a tier feature, it is the whole premise. Reservoir data under most operators' data-residency and confidentiality policies cannot go to a vendor's cloud. Enverus sells on-prem; Seequent sells on-prem; the enterprise tier must as well.

The monthly number is deliberately modest because the support commitment is what costs you, not the license. Scope support explicitly — hours included, response time, what happens when they need a new rule written.

### Service layer — the real margin

**$175/hour**, separate from any license, for field-specific rule configuration and audit support.

GeoTech's published subsurface rate is $125–180/hr. $175 is inside that band and reflects that you know production data specifically.

This is where the durable revenue is. A generic rule engine is commodity. Knowing that this operator's export names the uptime column `ON_STREAM_HRS`, that a zero in it means the field is wrong rather than the well being shut in, and that values under five percent of a well's own median are rounding artefacts — that knowledge is per-basin, per-vendor, per-operator, and it accumulates. It cannot be read off a README.

Sell the engine as the wedge. Sell the knowledge as the product.

### What I would not do

- **Do not price per well.** It punishes exactly the buyer with the most wells and the least willingness to pay, and it turns a tool into a tax on diligence.
- **Do not publish a free tier capped at 5 assets, Sohovi-style.** You are not competing on self-service volume; that race is lost before it starts.
- **Do not discount for a pilot.** A pilot that does not convert is unpaid consulting. If you want it cheap, sell it as a paid fixed-scope engagement.
- **Do not price below $300/month for team tier.** You will attract support load, not customers.

---

## What is not yet true about these prices

These figures are unvalidated in the sense that matters: no buyer has been asked. Specifically unproven:

- That an operator will pay anything at all for a standalone QC tool rather than doing it in-house.
- That $349/month is below the threshold of indifference and above the threshold of resentment.
- That the service layer is sellable without a track record of doing it.
- That anyone wants on-premise at $1,750 rather than a one-off engagement.

The fastest way to invalidate or confirm all four is not more research. It is five conversations with reservoir engineers and data managers, asking what they currently spend on production data QC and what they would have to justify to their boss.

## Blockers before any of this can be sold

Not pricing problems. Existence problems.

1. **No LICENSE file.** `pyproject.toml` declares Apache-2.0 and no LICENSE file was ever written. You currently hold the default all-rights-reserved position while shipping metadata that says otherwise. Fix before publishing.
2. **No payment or license enforcement wired into haql-qc.** `haql-license-win.exe` implements signed `HAQL1.<payload>.<sig>` tokens with expiry, but nothing in the package checks a license. Every tier above is currently unenforced.
3. **Volve data terms unresolved.** You can describe Volve as validation evidence. You cannot bundle it, redistribute it, or ship it as a sample dataset.
4. **One field validated.** Volve, 15,634 rows, 7 wells. Every "proven in production" claim rests on a single public dataset.
5. **Monthly ingestion incomplete.** The raw monthly export carries an embedded units row the loader does not read. The clean monthly result of zero findings was produced on a hand-cleaned file, not the real input.
6. **No signup path.** No PyPI release, no git remote, no `github.com/haql` organisation confirmed.

## Recommended sequence

1. Fix the LICENSE and `pyproject.toml` metadata this week. Half an hour, removes the worst inconsistency.
2. Write a plain `LICENSE` decision for each `haql-*` project rather than leaving all sixteen ambiguous.
3. Wire the existing license verifier into haql-qc so tiers are enforceable.
4. Get the monthly loader onto raw input, then re-verify.
5. Run the five conversations. Use them to set the real numbers.
6. Only then publish anything with a price attached.