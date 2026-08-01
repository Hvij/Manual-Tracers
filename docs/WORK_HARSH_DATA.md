# Harsh — data engineering track

You own everything from raw file to a scored anomaly. The partner owns everything
from the webhook onward. The contract between you is
[`RCA_OUTPUT_CONTRACT.md`](RCA_OUTPUT_CONTRACT.md) §4 (webhook payload).

---

## Done already

- `sql/01`–`06`: bronze → silver → gold, metric layer, registry, detection
- `scripts/replay.sh`: env-driven, `AD_EVENTS_FILE` at top, optional week shift
- `scripts/suggest_shift.sh`
- Detector validated: Android 15 z=28.1, iOS 18.1 z=10.6, global fill z=11.4

---

## 1. Run it (blocking — nothing else can start)

Neither ClickHouse MCP can execute DDL (both are SELECT-only), so this must run
from your machine.

```bash
git push -u origin alerting        # the sandbox has no SSH key
./scripts/replay.sh --schema       # creates dicts, MVs, registry, views
```

Then decide the shift:

```bash
./scripts/suggest_shift.sh         # prints TIME_SHIFT_WEEKS=4 today
```

Set `TIME_SHIFT_WEEKS=4` in `replay.sh`, then:

```sql
TRUNCATE TABLE inmobi.ad_events;
TRUNCATE TABLE inmobi.ad_events_enriched;
TRUNCATE TABLE inmobi.metric_1h;
```

```bash
./scripts/replay.sh                # full replay through the MVs
```

**Why the shift is not optional for the demo:** ClickStack evaluates alert rules
against wall-clock time. The data ends 2026-07-05. A "last 1 hour" rule sees an
empty window and can never fire. Shifting by whole weeks preserves day-of-week and
hour-of-day, so the seasonal baseline stays valid.

Expected after replay: 9,000,000 rows in bronze and silver, ~53K in `metric_1h`,
zero `unknown` in the enrichment health check.

## 2. Verify the MVs actually fired

The one failure mode that silently ruins everything: MVs only fire on **new**
inserts. If `metric_1h` is empty after a replay, the MV was created *after* the
data landed.

```sql
SELECT 'bronze' t, count() FROM inmobi.ad_events
UNION ALL SELECT 'silver', count() FROM inmobi.ad_events_enriched
UNION ALL SELECT 'gold',   count() FROM inmobi.metric_1h;
```

Bronze and silver must match exactly. If gold is 0, drop and recreate `mv_metric_1h`,
truncate, replay.

## 3. Baseline hygiene (real bug, ~30 min)

A trailing baseline that includes the incident makes recovery look like a spike —
Android 15 scores z=7.9 *upward* on Jun 26 purely because the three bad days
dragged its own baseline down.

Fix: exclude points already flagged anomalous from future baseline windows. Either
persist flags to a table and anti-join, or winsorise the window at p10/p90. This is
a visible correctness win a judge will notice.

## 4. Contribution ranking (blocks the partner — do early)

Detection finds *which slices moved*. RCA needs *which slice explains the delta*.
Add a view returning, per incident:

```
delta_contribution = (actual_seg - expected_seg) * traffic_share_seg
```

Ranked descending, this makes Android 15 rank above `device_model=Galaxy A54`
even though both are significant. Without it the agent picks whichever has the
largest percentage change, which is usually a small noisy slice.

## 5. Correlated-dimension conditioning

Android 15 drags `device_model` (Galaxy A54/S23, Redmi Note 12) and `region=EU`
along with it — those devices run Android 15. Give the agent a query that computes
a child slice's residual *after* excluding the parent. If the residual is within
band, the child is collateral. Ledger verdict `cleared_as_collateral`.

Without this the diagnosis names four culprits for one fault, and precision is a
scored criterion.

## 6. Mix vs rate

```
Δrate_total = Σ wᵢ·Δrateᵢ   (rate effect)
            + Σ rateᵢ·Δwᵢ   (mix effect)
```

Fill rate varies structurally by app category (utility ~0.75 → gaming ~0.82), so a
traffic composition shift moves the global number with no segment misbehaving.
Emit both terms per incident; the RCA contract requires the number.

## 7. Alert provisioning from the registry

Generate ClickStack tiles and alerts **from** `metric_registry` via the ClickStack
API — do not hand-maintain them. `scripts/provision_clickstack_cloud.py` has the
auth plumbing; it currently reads a deleted JSON, so repoint it at the registry.
Needs `RCA_WEBHOOK_URL` in `.env` (no webhook exists on the service yet).

One definition, HyperDX as a render target. Also the honest answer to "can we read
metric definitions out of HyperDX": no — invert the dependency.

## 8. Sealed-dataset rehearsal (do NOT skip)

Before the drop, do a full timed dry run: truncate → change `AD_EVENTS_FILE` →
replay → confirm incidents. Know the wall-clock cost. If the sealed file arrives
with no history, the baseline has nowhere to come from — decide now whether you
baseline from the current data or require the new file to carry its own history.

---

## Open decisions

| Decision | Why it matters |
|---|---|
| Baseline source if the sealed slice has no history | Blocks detection entirely; different code path |
| Detect at hourly or daily grain | Hourly now; daily has 24× the sample per point |
| Depth-2 pair scan in gold or on-demand in silver | Gold stays tiny; silver is ~2s per scan |

## Don't

- Don't alert on `app_id` / `geo_device_id` / `advertiser_id`. That is the
  767K-combo cross-product. Drill-down only.
- Don't raise `min_samples` to suppress noise — it hides real incidents
  (that is exactly how Android 15 went invisible at 5,000). Tighten `z` or the
  effect-size floors instead.
- Don't average a ratio. Everything is sum/sum via `v_metric_points`.
