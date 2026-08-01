# CLAUDE.md — InMobi Click-a-thon 2026

Automated root-cause analyst. A metric moves → detect it → drill down in ClickHouse
to isolate the responsible segment → emit an evidence-backed diagnosis where every
number is computed, not narrated into existence.

24-hour hackathon. Two people: Harsh (data engineering, through the webhook) and one
ML/AI engineer (webhook onward). Deadline pressure is real — prefer working over
elegant, but never trade away correctness or traceability, which are what is scored.

## Read first

| File | What it is |
|---|---|
| `InMobi/PROBLEM_STATEMENT.md` | The brief and the five judging criteria |
| `InMobi/metrics_glossary.md` | **Authoritative** metric formulas — never restate them elsewhere |
| `architecture.md` | System design and the reasoning behind it |
| `docs/RCA_AGENT_DESIGN.md` | **As-built.** What actually runs today (`RCA/app/`), vs. what's still to build |
| `docs/RCA_OUTPUT_CONTRACT.md` | The ledger, the narrative, the trace, the webhook payload (target shape; §4 matches reality) |
| `docs/WORK_HARSH_DATA.md` | Harsh's queue |
| `docs/WORK_ML_AGENT.md` | Partner's queue |

## Hard constraints

- **ClickHouse is the primary datastore and the analytical engine.** The drill-down
  must run as ClickHouse queries. Judges explicitly check that the engine is doing
  the real work, not the LLM.
- **ClickHouse computes, the LLM narrates.** The model never sees a raw row, never
  does arithmetic, never picks the next step. One fabricated number costs more than
  a missed anomaly.
- **No trace, no credit.** The sealed-dataset diagnosis must demonstrably come out
  of the pipeline. Langfuse trace per incident, flushed.
- **Build for the unseen incident**, not the four anomalies visible in this data.
  No hardcoded dates, segments, or thresholds tuned to what we found.
- All repo code must be written inside the 24-hour window.

## Layers

```
ad_events                bronze · raw, replayed, the only table reloaded
  ├─ MV1 ─▶ ad_events_enriched   silver · dictGet-denormalised · RCA drill surface
  └─ MV2 ─▶ metric_1h            gold   · hourly marginals · ALERT surface (~53K rows)
                 ├─ v_metric_points     THE metric layer — formulas live only here
                 ├─ v_metric_baseline   seasonal baseline
                 └─ v_metric_deviation  scored + guard-railed
                        │  ClickStack tile alert (is_anomaly count, metric_id in message)
                        ▼
                   webhook ─▶ RCA agent (RCA/app/) ─▶ [narrator + trace, not yet built]
```

Files: `sql/01_schema` → `02_dictionaries` → `03_silver` → `04_gold` →
`05_metric_layer` → `06_detection`. Apply in order; `scripts/replay.sh` does it.
`06_detection.sql` ends at `v_metric_deviation` — there is no persisted
incident table; the RCA agent re-derives everything live per alert (see
`docs/RCA_AGENT_DESIGN.md` §3.3 for what was removed and why).

The RCA agent itself (webhook receiver + investigation ladder) is its own `uv`
project in `RCA/` — `RCA/app/main.py` (webhook), `RCA/app/investigate.py`
(reproduce → decompose → scan → holdout), `RCA/app/registry.py`
(`metric_registry` / `metric_dim_priority` lookups).

## Non-negotiable design rules

1. **Alert on marginals, drill on combinations.** The full dimension cross-product
   is 767,984 combos (measured). `metric_1h` stores one row per
   `(hour, dim_name, dim_value)` — 63 series/hour.
2. **Alerting cardinality budget:** only dimensions with ≤ ~50 distinct values are
   alert series. `app_id` (2,000), `geo_device_id` (5,000), `advertiser_id` (500)
   are drill-down targets, never alert series.
3. **Ratios are sum/sum, always.** Never average a ratio. `metric_1h` stores only
   additive base quantities so this is structurally enforced.
4. **`min_samples` guards degenerate slices — it is not a confidence knob.**
   `proportionsZTest` supplies confidence. At 5,000 it silently hid the largest
   planted incident (Android 15, ~1,025 req/hr). To cut noise, tighten `z` or the
   effect-size floors instead.
5. **Metric levelling.** L1 `revenue` decomposes through the identity
   (`Requests × Fill rate × Render rate × eCPM/1000`) *before* any dimension is
   sliced. Otherwise one fault reports as three independent incidents.
6. **You can only rule out what you enumerated.** Every candidate gets a verdict:
   `implicated | cleared | inconclusive`. The ledger is simultaneously the LLM's
   input and the Langfuse trace.

## Environment gotchas

- **Both ClickHouse MCPs are SELECT-only.** DDL must run from the local machine via
  `scripts/replay.sh`. Do not try to create objects through an MCP.
- **`mcp__clickhouse__query` points at the HealthKart *work* cluster**, not the
  hackathon service. Never write there. The hackathon service is reached through
  the ClickStack connection (`Manual Tracers`) or `.env` credentials.
- **git-lfs is not installed in the Cowork sandbox.** `InMobi/data/*.csv` are
  LFS-tracked. A bare `git add -A` from the sandbox replaces the 130-byte pointers
  with full file content and corrupts LFS. **Always `git restore --staged
  InMobi/data/` before committing from the sandbox.** (This already happened once
  and was repaired in commit `6a9c7a4`.)
- **The sandbox cannot reach ClickHouse Cloud** (proxy 403). Validate SQL by running
  it as a SELECT through the ClickStack MCP; execute DDL locally.
- Pushing needs Harsh's SSH key — the sandbox has none.
- **`curl --data-binary @path` can fail to open large files in some sandboxed
  shells** (`curl: option --data-binary: error encountered when reading a file`,
  even though the file exists and is readable). `replay.sh` streams both the
  dimension CSVs and `AD_EVENTS_FILE` via stdin redirection (`--data-binary @- <
  "$file"`) instead — functionally identical, just avoids curl's own file-open
  path. Don't revert this to `@"$file"` on the sealed-dataset run.
- **ClickStack alert message templates only support `{{title}}`/`{{body}}`/
  `{{link}}`** — no group-by value, row column, or window timestamp (checked
  against the ClickStack alerts docs). A per-metric `metric_id=<x>` has to be a
  static string baked into each alert at config time, one alert per metric —
  it cannot be templated from the firing row.

## Data facts worth knowing

- 9,000,000 events, 2026-06-01 → 2026-07-05 (35 days, 840 hours), ~10.7K req/hour.
- `advertiser_id` is `''` (empty string, **not NULL**) on unfilled requests.
- Region is `NAM`, never `NA`.
- Dimensions: 5 ad_format, 7 category, 3 publisher_tier, 5 region, 16 country,
  8 device_model, 8 os_version, 7 vertical, 3 campaign_type = **62 slices at depth 1**.
  Exhaustive depth-1 enumeration is cheap — do not build clever pruning.
- `vertical` / `campaign_type` exist only on filled requests, so `fill_rate` is
  meaningless for them (`invalid_dims` in the registry).

## Confirmed detections (~2s over 9M rows)

| Day(s) | Segment | Actual | Expected | Peak z |
|---|---|---|---|---|
| Jun 23–25 | `os_version=Android 15` | 0.434 | 0.785 | 28.1 |
| Jun 29–30 | `os_version=iOS 18.1` | 0.683 | 0.780 | 10.6 |
| Jun 23–25 | global fill rate | 0.750 | 0.785 | 11.4 |

## Known traps

- **Correlated dimensions.** Android 15 drags `device_model` (Galaxy A54/S23,
  Redmi Note 12) and `region=EU` with it — those devices run Android 15. Condition
  on the parent and check the residual before naming a child slice.
- **Rank by contribution, not percentage change.**
  `delta_contribution = (actual − expected) × traffic_share`. Otherwise a tiny noisy
  slice outranks a large real one.
- **Simpson's paradox.** Fill rate varies by app category at baseline; a mix shift
  moves the global number with no segment misbehaving. Emit rate effect and mix
  effect separately.
- **Baseline contamination.** A trailing baseline containing the incident makes
  recovery look like a spike (Android 15 scores z=7.9 *upward* on Jun 26). Exclude
  already-flagged points from future baselines.
- **Global incidents have no culprit.** The Jun 21 −44% volume collapse is uniform
  across every dimension. Output "global movement, no localising segment" — do not
  invent one.
- **ClickStack alerts evaluate on wall clock.** The data ends 2026-07-05, so an
  unshifted load can never fire an alert. `TIME_SHIFT_WEEKS` in `replay.sh` fixes it,
  and must be **whole weeks** or day-of-week alignment breaks and the seasonal
  baseline silently corrupts.

## Commands

```bash
./scripts/replay.sh --schema    # DDL only
./scripts/replay.sh             # DDL + replay AD_EVENTS_FILE through the MVs
./scripts/replay.sh --data      # replay only
./scripts/replay.sh --dims      # also reload the dimension CSVs
./scripts/suggest_shift.sh      # compute TIME_SHIFT_WEEKS
```

Sealed dataset: change `AD_EVENTS_FILE` at the top of `replay.sh`, truncate manually
(helper at the bottom of the script), re-run. The script never truncates by itself.

## Style

- Comment the *why*, not the *what* — especially any non-obvious statistical or
  schema choice, since judges read for design reasoning.
- Prefer one clear SQL file over a clever abstraction.
- Do not add a UI. Polished frontends are explicitly out of scope and unscored.
