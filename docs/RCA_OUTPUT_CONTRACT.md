# RCA Output Contract

What the agent must produce, and what backs every sentence. Written against
`InMobi/PROBLEM_STATEMENT.md` §"What great looks like" and the five judging criteria.

**The rule:** ClickHouse computes every number. The LLM only narrates. If a figure
appears in the report and cannot be traced to a row in the ledger, that is a
scoring failure worse than missing the anomaly entirely.

**Status:** §1–3 are the target shape; the narrator and Langfuse trace are not
yet built. `RCA/app/investigate.py` today produces a ledger with the same
*intent* (candidates ranked by contribution, an exhaustive dim scan, a holdout
verdict) but different field names — see `docs/RCA_AGENT_DESIGN.md` §3.2 for
what actually runs. §4 below reflects the real webhook payload.

---

## 1. The hypothesis ledger (the only thing the LLM ever sees)

The agent builds this object. It is simultaneously the LLM's input, the Langfuse
trace payload, and the audit artifact. One object, three uses.

```jsonc
{
  "incident_id": "2026-06-23|fill_rate|os_version|Android 15|drop",
  "window":   { "from": "2026-06-23T00:00:00Z", "to": "2026-06-25T23:59:59Z", "grain": "hour" },
  "baseline": { "method": "hour_of_day + day_type, trailing 20 obs", "points": 20 },

  "trigger": {
    "metric": "fill_rate", "level": 2,
    "actual": 0.4338, "expected": 0.7849,
    "delta_abs": -0.3511, "delta_rel": -0.4473,
    "z": -28.1, "detector": "proportionsZTest",
    "sample_count": 27370
  },

  // Step 1 — which identity factor moved. Contributions MUST sum to the total.
  "factor_decomposition": {
    "identity": "Revenue = Requests x FillRate x RenderRate x eCPM/1000",
    "total_revenue_delta_rel": -0.046,
    "factors": [
      { "factor": "fill_rate",   "contribution_rel": -0.045, "verdict": "implicated" },
      { "factor": "requests",    "contribution_rel":  0.003, "verdict": "cleared" },
      { "factor": "render_rate", "contribution_rel": -0.000, "verdict": "cleared" },
      { "factor": "ecpm",        "contribution_rel":  0.001, "verdict": "cleared" }
    ]
  },

  // Step 2 — EXHAUSTIVE depth-1. Every one of the 62 slices gets a verdict.
  "segment_scan": {
    "depth": 1, "candidates_tested": 62, "implicated": 1, "cleared": 61,
    "ranked_by": "contribution_to_absolute_delta",
    "top": [
      { "dim": "os_version", "value": "Android 15", "actual": 0.4338, "expected": 0.7849,
        "traffic_share": 0.112, "delta_contribution": -0.0393, "z": -28.1,
        "verdict": "implicated" },
      { "dim": "device_model", "value": "Galaxy A54", "actual": 0.7297, "expected": 0.7852,
        "z": -7.8, "verdict": "cleared_as_collateral",
        "reason": "fully explained by parent os_version=Android 15; residual z=0.4 after conditioning" }
    ]
  },

  // Step 3 — the checks that stop confident wrong answers
  "ruled_out": [
    { "check": "seasonality",  "verdict": "cleared",
      "evidence": "Jun 23-25 are Tue-Thu, compared against Tue-Thu of prior 3 weeks at matching hour-of-day" },
    { "check": "mix_shift",    "verdict": "cleared",
      "evidence": "Android 15 traffic share 11.2% vs 11.1% baseline; rate effect -0.0393, mix effect -0.0001" },
    { "check": "uniformity",   "verdict": "localised",
      "evidence": "34 of 35 non-Android-15 slices within band; drop is not global" },
    { "check": "correlated_dims", "verdict": "resolved",
      "evidence": "device_model and region=EU moves vanish after conditioning on os_version" }
  ],

  "queries": [
    { "step": "segment_scan_depth1", "query_id": "…", "read_rows": 9000000, "elapsed_s": 2.03 }
  ],
  "unexplained_residual_rel": 0.002
}
```

**Every numeric field above is written by SQL. The LLM writes none of them.**

---

## 2. The narrative (what the judge reads)

Four sections, in this order. Nothing else.

> **What happened.** Fill rate fell to 0.434 over Jun 23–25 against a same-weekday,
> same-hour baseline of 0.785 — a 35.1pp drop (−44.7% relative), z = −28.1 on
> 27,370 requests/day.
>
> **Which factor.** Walking `Revenue = Requests × Fill rate × Render rate × eCPM/1000`,
> fill rate accounts for −4.5pp of the −4.6% revenue move. Requests (+0.3%),
> render rate (−0.0pp) and eCPM (+0.1%) together contribute +0.1pp and are ruled out.
>
> **Which segment.** `os_version = Android 15`, on 11.2% of requests. This slice
> contributes −3.93pp of the −3.5pp global drop — more than all of it; the remaining
> traffic ran slightly above baseline.
>
> **Checked and ruled out.** All 62 single-dimension slices were tested; 61 fell within
> band. `device_model` (Galaxy A54/S23, Redmi Note 12) and `region=EU` also moved, but
> those devices run Android 15 — conditioning on OS leaves a residual z of 0.4, so they
> are collateral, not causes. Traffic mix by OS was stable (11.2% vs 11.1%), so this is
> a rate change, not a composition change. Jun 23–25 are Tue–Thu compared against Tue–Thu
> of the prior three weeks, so seasonality is excluded.

### Narration rules

1. Never compute. Every number is copied verbatim from the ledger.
2. Never round beyond what the ledger provides.
3. If `implicated == 0`, say **"global movement, no localising segment"**. Do not
   name a culprit. (The Jun 21 −44% volume collapse is uniform across every
   dimension — a system that always names a segment will invent one.)
4. If `unexplained_residual_rel > 0.25`, state that the diagnosis is partial.
5. Report cleared candidates as a count plus the notable near-misses, never all 61.

---

## 3. Langfuse trace shape

One trace per incident. Spans mirror the ledger sections exactly, so the trace *is*
the investigation log rather than a description of it.

```
trace: incident 2026-06-23|fill_rate|os_version|Android 15
├── span detect          input: trigger        output: metric, z, window
├── span decompose       input: identity       output: 4 factor verdicts     + SQL
├── span scan_depth1     input: 62 candidates  output: 1 implicated, 61 cleared + SQL
├── span disambiguate    input: correlated dims output: collateral verdicts   + SQL
├── span rule_out        input: 4 checks       output: 4 verdicts             + SQL
└── span narrate         input: full ledger    output: prose  (LLM call — the only one)
```

Attach to every span: the SQL text, ClickHouse `query_id`, `read_rows`, `elapsed`.
That is the evidence for "analytical depth in ClickHouse is doing the real work."

**No trace, no credit** — the sealed-incident diagnosis must come out of this pipeline.

---

## 4. Webhook payload (alert → agent) — as built

ClickStack's alert webhook body template supports only `{{title}}`, `{{body}}`,
`{{link}}` — no per-row or group-by variables (verified against the ClickStack
alerts docs). So the payload is not a data row at all: it's a static
`metric_id=<x>` string baked into each alert's message at config time, one
alert per alertable metric (`fill_rate`, `requests`, `ecpm`, `revenue`).

```json
{
  "title": "RCA: fill_rate anomaly",
  "body": "metric_id=fill_rate",
  "link": "https://hyperdx.clickhouse.cloud/dashboards/…"
}
```

`RCA/app/main.py` regex-extracts `metric_id`, validates it against
`metric_registry`, and hands off to `run_investigation(metric_id)` — which
re-derives the window, the trigger, and every candidate live from
`v_metric_deviation` / `ad_events_enriched`. Nothing about *which segment* or
*what value* ever travels over the wire; that's the investigation's job, not
the alert's. This is a stronger version of the "deliberately thin" principle
the original design called for — thinner than a `day`/`dim_name`/`dim_value`
row, because ClickStack cannot template one.

---

## 5. Definition of done

- [ ] Every number in the narrative appears in the ledger
- [ ] `implicated + cleared == candidates_tested` (the scan was exhaustive)
- [ ] Factor contributions sum to the total delta within tolerance
- [ ] Seasonality, mix-shift, uniformity and correlated-dims checks all present
- [ ] Trace opens and reads as an investigation, in order
- [ ] Runs end to end on an unseen file with no code change
