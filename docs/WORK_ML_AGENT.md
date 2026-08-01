# ML/AI engineer — RCA agent track

You own everything from the webhook to the narrative and the trace. Harsh owns
everything up to the webhook. Your spec is
[`RCA_OUTPUT_CONTRACT.md`](RCA_OUTPUT_CONTRACT.md) — read it first, it is the
definition of done.

**You are not blocked.** The payload schema is fixed (contract §4). Hand-write one
fixture and build the entire loop against it before real alerts exist.

---

## The one rule

**ClickHouse computes. The LLM narrates.**

The LLM never sees a raw row, never does arithmetic, never decides what to check
next. It receives a finished ledger and turns it into prose. A single fabricated
number costs more than a missed anomaly — the problem statement says so explicitly,
and it is the easiest criterion to lose.

Practically: the only LLM call in the whole system is the final `narrate` span.

---

## Build order

### 1. Fixture + ledger schema (start here)

Take the payload in contract §4, write it to `fixtures/incident_android15.json`,
and build the loop against it. Everything downstream is testable offline.

### 2. Webhook receiver

FastAPI endpoint → validate → dedup on `fingerprint` → enqueue. Idempotent: the
same fingerprint must not launch two investigations.

### 3. The investigation loop

Fixed sequence, not an agent free-roaming with a SQL tool. Each step is a Langfuse
span and appends to the ledger:

| Step | Question | Reads |
|---|---|---|
| `decompose` | which identity factor moved? | `v_metric_points` |
| `scan_depth1` | which of the 62 slices? | `metric_1h` |
| `disambiguate` | is this slice just collateral of another? | `ad_events_enriched` |
| `scan_depth2` | does a dimension *pair* explain more? | `ad_events_enriched` |
| `rule_out` | seasonality, mix shift, uniformity | `v_metric_points` |
| `narrate` | write it up | ledger only |

Use LangChain for orchestration if you like, but **do not** give the model a
free-form SQL tool. Parameterised queries only. A judge inspecting a trace should
see deterministic SQL, not model-authored SQL.

### 4. Narration

Contract §2 has the exact four-section shape and the narration rules. The two that
matter most:

- If `implicated == 0`, output **"global movement, no localising segment."** The
  Jun 21 −44% volume collapse is uniform across every dimension. A system built to
  always name a culprit will invent one, and that is a hallucination.
- Report cleared candidates as a count plus notable near-misses. Never all 61.

Enforce grounding mechanically: after generation, extract every number from the
prose and assert each appears in the ledger. Fail the run if not. This is cheap and
it is the criterion most likely to sink us.

### 5. Langfuse

Contract §3 has the span shape. Non-negotiables:

- **One trace per incident**, `trace_id = fingerprint`, so a judge opens one thing.
- Every span carries: SQL text, ClickHouse `query_id`, `read_rows`, `elapsed`.
  That is the evidence for "ClickHouse is doing the real work."
- **Flush before exit.** Langfuse batches. An un-flushed trace on sealed-data night
  means zero on the highest-weighted criterion. Add an explicit flush and verify the
  trace is visible in the UI before declaring done.
- Write `trace_url` back to `inmobi.anomaly_events` so alert → investigation →
  diagnosis is followable in one table.

**No trace, no credit.** A perfect diagnosis with a broken trace scores nothing.

---

## The traps that will cost us

**Correlated dimensions.** Android 15 also lights up `device_model` (Galaxy A54/S23,
Redmi Note 12) and `region=EU` — those devices run Android 15. Naming all four is
wrong and reads as noise. Condition on the parent, check the residual, mark
collateral.

**Ranking by percentage change.** A tiny slice with a noisy 40% swing will outrank a
large slice with a real 3% move. Rank by `delta_contribution`
(`(actual − expected) × traffic_share`). Harsh is delivering this view.

**Simpson's paradox.** Fill rate varies by app category at baseline. A mix shift
moves the global number with no segment misbehaving. The `mix_shift` check is not
optional.

**Seasonality.** At least one planted movement is pure seasonality and must be
cleared, not alarmed on. The baseline handles it; your job is to *state* it was
checked, with the number.

**Token burn.** Never pull rows into context. Aggregates only. If a tool returns
more than ~50 rows, it is the wrong tool.

---

## Definition of done

- [ ] Runs end to end from a webhook with no human step
- [ ] Every number in the prose exists in the ledger (mechanically asserted)
- [ ] `implicated + cleared == candidates_tested`
- [ ] Handles the no-localising-segment case without inventing a culprit
- [ ] One Langfuse trace per incident, flushed, with SQL and `query_id` on spans
- [ ] `trace_url` written back to `anomaly_events`
- [ ] Dry-run rehearsed against a file nobody has seen

---

## Interfaces you can rely on

```sql
-- metric definitions (never restate a formula yourself)
SELECT * FROM inmobi.v_metric_points
WHERE metric_id = 'fill_rate' AND dim_name = 'os_version';

-- drill order, with rationale
SELECT dim_name, priority, rationale FROM inmobi.metric_dim_priority
WHERE metric_id = 'fill_rate' ORDER BY priority;

-- fired incidents
SELECT * FROM inmobi.v_incidents ORDER BY peak_abs_z DESC;

-- write results back
INSERT INTO inmobi.anomaly_events (fingerprint, rca_status, rca_summary, trace_url) VALUES (...);
```

Depth-2 drill-downs go against `inmobi.ad_events_enriched` (9M rows, ~2s). Never
against `ad_events` — the dimensions are not resolved there.
