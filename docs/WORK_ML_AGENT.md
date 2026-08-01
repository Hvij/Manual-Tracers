# ML/AI engineer — RCA agent track

You own everything from the webhook to the narrative and the trace. Harsh owns
everything up to the webhook. Your spec is
[`RCA_OUTPUT_CONTRACT.md`](RCA_OUTPUT_CONTRACT.md) — read it first, it is the
definition of done.

**Status:** webhook receiver + investigation ladder through `holdout` are built
and running in `RCA/app/` — see `docs/RCA_AGENT_DESIGN.md` §3 for what actually
runs. What's left is narration (§4 below) and Langfuse tracing (§5 below).

**You are not blocked.** The payload schema is fixed (contract §4, now just
`metric_id=<x>` in the alert body — ClickStack cannot template a data row, see
`docs/RCA_AGENT_DESIGN.md` §3.1).

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

### 1–3. Webhook + investigation ladder — done

No fixture file was needed in the end; the loop was built and verified directly
against the live (later replayed) dataset. `RCA/app/main.py` dedups on
`hash(title + body)` rather than a `fingerprint` field (ClickStack doesn't
supply one — see contract §4), same idempotency intent. The ladder is fixed
sequence, no free-form SQL tool given to any model — every query in
`investigate.py` is parameterised:

| Step | Question | Reads | Status |
|---|---|---|---|
| `reproduce_global` | is the alerted metric still anomalous, right now? | `v_metric_deviation` | done |
| `decompose` | which identity factor moved? (revenue only) | `v_metric_deviation` | done |
| `scan_dims` | which segment, ranked by contribution? | `v_metric_deviation` | done |
| `holdout_check` | is the top candidate the sole cause? | `ad_events_enriched` | done |
| interaction (cross 2 dims on a near-100% tie) | — | `ad_events_enriched` | not built |
| `narrate` | write it up | ledger only | not built |

Depth-2 interaction crossing (two dimensions each near 100% of contribution) is
the one ladder stage from the original design not yet implemented.

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
- Write `trace_url` into a new `rca_reports` table (there is no `anomaly_events`
  to write back into anymore — see `docs/RCA_AGENT_DESIGN.md` §3.3 / §5) so
  alert → investigation → diagnosis is followable in one place.

**No trace, no credit.** A perfect diagnosis with a broken trace scores nothing.

---

## The traps that will cost us

**Correlated dimensions.** Android 15 also lights up `device_model` (Galaxy A54/S23,
Redmi Note 12) and `region=EU` — those devices run Android 15. Naming all four is
wrong and reads as noise. Condition on the parent, check the residual, mark
collateral.

**Ranking by percentage change.** A tiny slice with a noisy 40% swing will outrank a
large slice with a real 3% move. Rank by contribution
(`Σ |delta_abs| × sample_count`) — `investigate.scan_dims` already does this.

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
- [ ] Diagnosis persisted (a `rca_reports` table — `anomaly_events` was removed,
      see `docs/RCA_AGENT_DESIGN.md` §3.3, so this needs to be added fresh)
- [ ] Dry-run rehearsed against a file nobody has seen

---

## Interfaces you can rely on

```sql
-- metric definition + guard rails (never restate a formula yourself)
SELECT * FROM inmobi.metric_registry FINAL WHERE metric_id = 'fill_rate';

-- drill order, with rationale
SELECT dim_name, priority, rationale FROM inmobi.metric_dim_priority FINAL
WHERE metric_id = 'fill_rate' ORDER BY priority;

-- reproduce the global move: is it still anomalous, right now?
SELECT ts, actual, expected, z_score, is_anomaly FROM inmobi.v_metric_deviation
WHERE metric_id = 'fill_rate' AND dim_name = 'ALL'
  AND ts > {start:DateTime} AND ts <= {end:DateTime} ORDER BY ts;

-- per-segment scan, ranked by contribution not percentage change
SELECT dim_name, dim_value, sum(abs(delta_abs) * sample_count) AS contribution
FROM inmobi.v_metric_deviation
WHERE metric_id = 'fill_rate' AND dim_name IN {dims:Array(String)}
  AND ts > {start:DateTime} AND ts <= {end:DateTime} AND is_anomaly = 1
GROUP BY dim_name, dim_value ORDER BY contribution DESC;
```

All three are implemented in `RCA/app/registry.py` / `RCA/app/investigate.py` —
`get_metric`, `get_dim_priority`, `reproduce_global`, `scan_dims`.

Holdout / depth-2 drill-downs go against `inmobi.ad_events_enriched` (9M rows,
~2s) — see `investigate.holdout_check`. Never against `ad_events` — the
dimensions are not resolved there.
