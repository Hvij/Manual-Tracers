# Architecture — Automated Root-Cause Analyst

Registry-driven: detection and RCA hold no hardcoded metric or dimension
knowledge. Everything is read from tables in `inmobi`.

```
ad_events (bronze, replayed)
   ├─ MV1 ─▶ ad_events_enriched   silver · denormalised · RCA drill surface
   └─ MV2 ─▶ metric_1h            gold   · hourly marginals · ALERT surface
                  │
                  ├─ v_metric_points     metric layer (formulas live ONLY here)
                  ├─ v_metric_baseline   seasonal baseline
                  ├─ v_metric_deviation  scored, guard-railed
                  └─ v_incidents ─▶ anomaly_events ─▶ webhook ─▶ RCA agent
                                                                   │
                                            drills ad_events_enriched (depth 2)
                                                                   ▼
                                                    hypothesis ledger ─▶ LLM narrator
                                                                     └─▶ Langfuse trace
```

## The rule that makes this scale

**Alert on marginals, drill on combinations.**

A full dimension cross-product is 767,984 combos on 9M rows — measured, not
estimated. We never alert on that. `metric_1h` stores one row per
`(hour, dim_name, dim_value)`: 62 dimension values + 1 global bucket = 63 series
per hour, ~53K rows total. Alert queries read kilobytes and run in ~2s.

**Alerting cardinality budget:** only dimensions with ≤ ~50 distinct values are
alert series. `app_id` (2,000), `geo_device_id` (5,000) and `advertiser_id` (500)
are drill-down targets during RCA, never alert series. This is what holds at 100x.

## Registry tables

| Table | Purpose |
|---|---|
| `metric_registry` | metric_id, level, numerator/denominator, detector, guard rails, invalid_dims |
| `metric_dim_priority` | per-metric drill order with a written rationale |
| `anomaly_events` | fired incidents + RCA status + trace URL (audit and dedup) |

Metrics follow `InMobi/metrics_glossary.md` exactly, and are **levelled**:

- **L1 `revenue`** — the outcome.
- **L2 `requests`, `fill_rate`, `render_rate`, `ecpm`** — the identity factors:
  `Revenue = Requests × Fill rate × Render rate × eCPM/1000`
- **L3 `ctr`, `rpr`** — context. CTR is not a revenue factor in a CPM model.

Levelling matters: when revenue moves, the agent decomposes the identity **before**
touching any dimension. Without it the system reports "revenue down, eCPM down,
fill rate down" as three incidents instead of one causal chain.

Dimension priority encodes physics, not cardinality. Fill-rate faults are
supply-side, so `os_version` and `country` lead. eCPM moves are demand-side, so
`publisher_tier` and `vertical` lead. **Validated on real data:** the largest
planted incident is `os_version='Android 15'`, which a region-first drill order
would have reached last.

## Baseline and detection

Baseline: same **hour-of-day** and same **day-type** (weekday/weekend), trailing up
to 20 matching observations. A flat average flags every weekend; this does not.
Spread uses robust IQR, not stddev, so incidents inside the lookback don't inflate
the band and mask themselves.

Two detectors:

- **Ratio metrics** (`fill_rate`, `render_rate`, `ctr`) → `proportionsZTest` on raw
  numerator/denominator against the pooled baseline. Power comes from **sample size,
  not history**, which is what makes 5 weeks of data workable.
- **Continuous metrics** (`revenue`, `ecpm`, `requests`) → robust z against the
  seasonal median.

An alert requires **all** guard rails to pass: `min_samples`, ≥8 baseline points,
`|z| ≥ 4.0`, minimum relative effect, minimum absolute effect, and ≥3 anomalous
hours in the day. Loose thresholds (z≥1.5) would fire on ~13% of points by chance
across 62 series × 7 metrics × 840 hours — "crying wolf" is explicitly penalised.

**`min_samples` is a degenerate-slice guard, never a confidence substitute.** At
5,000 it silently hid Android 15 (~1,025 req/hr). Rule: ≈5% of the global hourly
request rate.

## What the RCA agent must do

One generic agent, not one per metric — splitting them makes identity
decomposition impossible.

1. **Decompose** — walk the revenue identity, find which factor moved.
2. **Localise** — rank depth-1 slices by *contribution to the absolute delta*, not
   by percentage change. Then depth-2 pairs against silver.
3. **Disambiguate correlated dimensions** — Android 15 also lights up
   `device_model` (Galaxy A54/S23, Redmi Note 12) and `region=EU`, because those
   devices run Android 15. The agent must test whether a child slice is fully
   explained by the parent before naming it.
4. **Mix vs rate** — `Δrate = Σ wᵢ·Δrateᵢ + Σ rateᵢ·Δwᵢ`. Fill rate varies by app
   category at baseline, so a pure composition shift moves the global number with
   no segment misbehaving.
5. **Uniformity** — if every slice moves together, say "global, no localising
   segment" rather than inventing a culprit.
6. **Ledger** — every candidate checked, with verdict `implicated | cleared |
   inconclusive`. You can only rule out what you enumerated. The ledger is
   simultaneously the LLM's only input and the Langfuse trace.

## Known artifact

A trailing baseline that includes the incident makes **recovery look like a spike**
(Android 15 on Jun 26 scores z=7.9 upward). Fix: exclude points already flagged
anomalous from future baselines.

## Confirmed detections (9M rows, ~2s)

| Day(s) | Segment | Actual | Expected | Peak z |
|---|---|---|---|---|
| Jun 23–25 | `os_version=Android 15` | 0.434 | 0.785 | 28.1 |
| Jun 29–30 | `os_version=iOS 18.1` | 0.683 | 0.780 | 10.6 |
| Jun 23–25 | global fill rate | 0.750 | 0.785 | 11.4 |

## Deliberately not doing

- **No per-metric agents** — one registry-driven agent.
- **No SQL templates stored in the registry** — injection-prone and untraceable.
  The agent composes queries from `v_metric_points`, so formulas live in one place.
- **No metric definitions in HyperDX** — ClickHouse is the definition store;
  ClickStack tiles and alerts are *generated* from the registry.
