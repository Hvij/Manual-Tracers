# Metric Deviation System with ClickHouse and HyperDX

## Conversation Transcript

---

## User

ok, i have some tables in clickhouse. Now i have decided to make some metrics on those features which we will use the data from clickhouse. Now, i want to make a metric deviation system. If a metric deviates, i should be able to basically make trigger out of it so that an ai agnet or anyone can be invoked to the RCA for that metric deviation. For this What can be useful for this ?

---

## Assistant

The best design is to keep **deviation detection deterministic** and use the **AI agent only after an anomaly has been confirmed**, mainly for root-cause analysis.

## Recommended architecture

```text
ClickHouse source tables
        │
        ▼
Metric computation
(Materialized Views / scheduled SQL / dbt)
        │
        ▼
metric_points table
        │
        ▼
Deviation detector
        │
        ▼
anomaly_events table
        │
        ▼
Grafana Alerting / Alertmanager
        │ webhook
        ▼
Workflow orchestrator
(Temporal or queue worker)
        │
        ▼
RCA agent
        │
        ├── ClickHouse read-only queries
        ├── Related metrics
        ├── Logs and traces
        ├── Deployments/config changes
        └── Runbooks
        │
        ▼
Slack / PagerDuty / Jira / human approval
```

## 1. Create a standard metric layer

Do not let every alert contain its own arbitrary SQL. Maintain a metric registry such as:

```yaml
metric_id: checkout_success_rate
owner: payments
interval: 5m
dimensions:
  - country
  - payment_provider
  - app_version
direction: lower_is_bad

query: |
  SELECT
      toStartOfFiveMinutes(event_time) AS ts,
      countIf(status = 'success') / count() AS value
  FROM payments
  WHERE event_time >= {start}
    AND event_time < {end}
  GROUP BY ts

detector:
  type: seasonal_mad
  lookback_days: 28
  warning_score: 3
  critical_score: 5

minimum_sample_size: 500
consecutive_failures: 3
cooldown_minutes: 30
```

Store metric results in a common table:

```sql
CREATE TABLE metric_points
(
    metric_id LowCardinality(String),
    ts DateTime,
    dimensions Map(String, String),
    value Float64,
    sample_count UInt64,
    calculated_at DateTime,
    metric_version String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (metric_id, dimensions, ts);
```

Useful supporting tables are:

- `metric_definitions`
- `metric_points`
- `metric_evaluations`
- `anomaly_events`
- `metric_dependencies`
- `deployment_events`

You can manage the SQL models with `dbt-clickhouse`, which is supported as a ClickHouse integration. MetricFlow itself should not currently be assumed to work directly with ClickHouse, so a version-controlled YAML registry or database table is safer.

## 2. Compute metrics inside ClickHouse

For high-volume append-only event data, use incremental materialized views to pre-aggregate data into one-minute, five-minute or hourly metric tables. ClickHouse materialized views operate on newly inserted blocks and write their output into a target table.

Be careful with corrections and backfills: changes to existing source data do not automatically recalculate an incremental materialized view. For metrics affected by late-arriving data, updates or historical corrections, use scheduled recomputation or refreshable materialized views instead.

## 3. Use several deviation detectors

One detector will not work well for every metric.

### Start with these

**Static threshold**

```text
error_rate > 5%
queue_depth > 10,000
success_rate < 97%
```

Best for metrics with clear operational limits.

**Relative change**

```text
current value differs from previous comparable period by more than 20%
```

Useful for revenue, traffic, conversion and volume.

**Rolling z-score**

```text
score = (current - rolling_mean) / rolling_stddev
```

Simple, but sensitive to outliers.

**Median and MAD**

```text
score = abs(current - historical_median) / historical_MAD
```

Usually better than standard deviation for noisy business metrics.

**Seasonality-aware baseline**

Compare:

- Monday 10:00 with previous Mondays at 10:00.
- This hour with the same hour over the previous four weeks.
- Current holiday traffic with comparable holidays.

This is important for traffic, orders and user activity.

**EWMA or CUSUM**

Useful for detecting gradual shifts that may never cross a large one-time threshold.

ClickHouse window functions can calculate rolling averages, standard deviation baselines and SQL-based anomaly conditions directly in the database.

## 4. Add alert-noise controls

A production system needs more than `score > 3`.

Use:

```text
minimum sample size
AND data is fresh
AND deviation exists for 3 consecutive windows
AND alert is not already open
AND alert is outside its cooldown
```

Include:

- **Pending period:** anomaly must persist for several evaluations.
- **Hysteresis:** alert at one threshold and recover at a less strict threshold.
- **Deduplication:** fingerprint using metric, dimensions and anomaly direction.
- **Cooldown:** avoid repeatedly invoking the RCA agent.
- **Missing-data alert:** distinguish “metric is bad” from “pipeline stopped”.
- **Severity:** based on deviation, duration and business impact.
- **Dimension limits:** do not produce thousands of alerts for every customer or device.

Store every evaluation, including non-anomalous ones. This makes detector debugging and backtesting much easier.

## 5. Use Grafana as the first alert engine

For an MVP, this is likely the fastest route:

```text
ClickHouse → Grafana ClickHouse datasource → Grafana alert → webhook
```

Grafana’s ClickHouse data source supports alert rules that execute ClickHouse SQL and fire when the result meets a condition.

Grafana contact points can send alerts to Slack, PagerDuty, Kafka REST Proxy, MQTT, email or a generic webhook. A webhook delivers a JSON payload that can start your RCA workflow.

A useful alert payload would contain:

```json
{
  "event_id": "anom_82917",
  "metric_id": "checkout_success_rate",
  "status": "firing",
  "severity": "critical",
  "actual": 0.81,
  "expected": 0.96,
  "lower_bound": 0.93,
  "score": 6.7,
  "window_start": "2026-08-01T08:00:00Z",
  "dimensions": {
    "country": "IN",
    "payment_provider": "provider_a"
  },
  "dashboard_url": "...",
  "runbook_id": "payment-success-drop"
}
```

## 6. Invoke an RCA workflow, not just an open-ended agent

A reliable RCA process should be a workflow:

```text
1. Validate that the alert is still active.
2. Load metric definition and ownership.
3. Compare current and historical windows.
4. Find the dimensions contributing most to the change.
5. Check related upstream and downstream metrics.
6. Check recent deployments and configuration changes.
7. Search relevant logs and traces.
8. Generate ranked hypotheses.
9. Produce an evidence-backed RCA report.
10. Ask for human approval before remediation.
```

Temporal is useful when RCA runs need durable execution, retries, timeouts or human-in-the-loop pauses.

For a small MVP, you could initially use:

```text
Grafana webhook → FastAPI endpoint → queue → RCA worker
```

Move to Temporal once you need retries, parallel investigations, approval steps and long-running incident state. Do not introduce Kafka and Temporal immediately unless your existing infrastructure already uses them.

## 7. Give the RCA agent controlled tools

The agent should not receive unrestricted production credentials. Give it tools such as:

```text
get_metric_definition(metric_id)
get_metric_history(metric_id, dimensions, start, end)
compare_time_windows(metric_id, current_window, baseline_window)
get_top_contributors(metric_id, dimension)
get_correlated_metrics(metric_id)
get_recent_deployments(service, start, end)
search_logs(service, filters, start, end)
get_trace_summary(service, start, end)
get_runbook(metric_id)
create_incident_report(event_id, report)
```

For ClickHouse access:

- Use a read-only user.
- Allow only approved databases and tables.
- Apply query time and memory limits.
- Limit historical ranges and returned rows.
- Prefer parameterized query templates.
- Record every query executed by the agent.
- Prevent arbitrary `INSERT`, `ALTER`, `DROP` and remediation commands.

The agent should return evidence, not merely an answer:

```json
{
  "summary": "Checkout success rate dropped primarily for provider_a in India.",
  "confidence": 0.84,
  "likely_causes": [
    {
      "hypothesis": "Provider timeout increase",
      "evidence": [
        "Timeout errors increased 4.8x",
        "Provider latency p95 increased from 1.2s to 6.4s",
        "No equivalent change for other providers"
      ]
    }
  ],
  "recent_changes": [
    "payments-service deployment at 13:22 IST"
  ],
  "recommended_actions": [
    "Check provider_a status",
    "Consider temporarily routing traffic to provider_b"
  ],
  "queries_executed": ["query-id-123", "query-id-124"]
}
```

## 8. Correlate metrics, logs, traces and deployments

RCA will be much stronger when all signals share attributes such as:

```text
service.name
environment
region
cluster
deployment.version
customer_tier
trace_id
```

OpenTelemetry supports consistent resource attributes across logs, metrics and traces, and logs can be correlated with active traces through trace and span identifiers.

Also insert deployments and configuration changes into ClickHouse:

```sql
CREATE TABLE deployment_events
(
    ts DateTime,
    service LowCardinality(String),
    environment LowCardinality(String),
    version String,
    commit_sha String,
    deployed_by String,
    metadata Map(String, String)
)
ENGINE = MergeTree
ORDER BY (service, environment, ts);
```

This lets the RCA agent directly ask:

> What changed within 30 minutes before the deviation started?

## Recommended stack

### Fast MVP

```text
ClickHouse
+ Materialized views or scheduled SQL
+ metric_points and anomaly_events tables
+ Grafana ClickHouse datasource
+ Grafana Alerting
+ Webhook receiver
+ Python RCA worker
+ Slack/Jira output
```

### Production-grade version

```text
ClickHouse
+ dbt-clickhouse and versioned metric registry
+ Dedicated deviation evaluation service
+ Grafana/Alertmanager
+ Kafka or Redpanda, when event volume requires it
+ Temporal
+ Read-only RCA agent tools
+ OpenTelemetry logs and traces
+ PagerDuty/Slack/Jira
+ Human approval for remediation
```

## My main recommendation

Build the first version with:

```text
ClickHouse → metric_points → SQL deviation query
→ Grafana alert → webhook → RCA workflow
```

Start with three detector types:

1. Static thresholds.
2. Percentage change against a comparable period.
3. Seasonal median/MAD.

Then implement the RCA agent around **top-contributor analysis, related metrics, deployment correlation and log evidence**. This gives you a useful system without depending on an LLM to decide whether something is anomalous.

---

## User

1. can we use hyperdx for alerting? compariion with same day last week? some statistical checks using clickhouse functions?
2. what if want the alerts near realtime and a generalised solution that can be scaled

---

## Assistant

Yes—**HyperDX, now the ClickStack UI, can be used for this alerting layer**. Current ClickStack SQL-based alerts can run arbitrary ClickHouse SQL, including window functions, rolling baselines and statistical checks. The query can return `1/0`, and the alert fires when the result is `1`. Alerts can deliver to webhooks, Slack and incident-management systems, which is suitable for triggering your RCA workflow or AI agent.

## 1. Same time last week

For metrics with weekly seasonality, comparing the current five-minute bucket with the equivalent bucket seven days ago is a good first detector.

Assume an aggregated table:

```sql
CREATE TABLE metric_5m
(
    metric_id LowCardinality(String),
    bucket DateTime,
    dimensions Map(String, String),
    value Float64,
    sample_count UInt64
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(bucket)
ORDER BY (metric_id, dimensions, bucket);
```

A ClickStack/HyperDX alert query could be:

```sql
WITH
    toStartOfInterval(now(), INTERVAL 5 MINUTE) AS current_bucket,

    values AS
    (
        SELECT
            metric_id,
            dimensions,

            sumIf(value, bucket = current_bucket - INTERVAL 5 MINUTE)
                AS current_value,

            sumIf(value, bucket = current_bucket - INTERVAL 7 DAY - INTERVAL 5 MINUTE)
                AS previous_week_value,

            sumIf(sample_count, bucket = current_bucket - INTERVAL 5 MINUTE)
                AS current_samples,

            sumIf(sample_count, bucket = current_bucket - INTERVAL 7 DAY - INTERVAL 5 MINUTE)
                AS previous_week_samples

        FROM metric_5m
        WHERE metric_id = 'checkout_count'
          AND bucket IN
          (
              current_bucket - INTERVAL 5 MINUTE,
              current_bucket - INTERVAL 7 DAY - INTERVAL 5 MINUTE
          )
        GROUP BY
            metric_id,
            dimensions
    )

SELECT
    current_bucket AS ts,
    metric_id,
    dimensions,

    current_value,
    previous_week_value,

    round(
        100 * (current_value - previous_week_value)
        / nullIf(previous_week_value, 0),
        2
    ) AS percentage_change,

    if(
        current_samples >= 500
        AND previous_week_samples >= 500
        AND abs(current_value - previous_week_value)
            / greatest(abs(previous_week_value), 1) > 0.20,
        1,
        0
    ) AS should_alert

FROM values;
```

This triggers when the metric differs by more than 20%, provided both periods have sufficient volume.

### Important improvement

Comparing only to **one previous week** is fragile. Last week itself might have been abnormal.

A better baseline is the same bucket from the previous four to eight weeks:

```text
Current Monday 14:00
compared with:
- Previous Monday 14:00
- Monday two weeks ago 14:00
- Monday three weeks ago 14:00
- Monday four weeks ago 14:00
```

That gives you a seasonal baseline rather than a single comparison.

## 2. Seasonal statistical check in ClickHouse

Here is a generalized weekly z-score detector.

```sql
WITH
    toStartOfInterval(now(), INTERVAL 5 MINUTE) AS current_bucket,

    history AS
    (
        SELECT
            metric_id,
            dimensions,
            bucket,
            value,
            sample_count,

            dateDiff(
                'week',
                bucket,
                current_bucket - INTERVAL 5 MINUTE
            ) AS weeks_ago

        FROM metric_5m

        WHERE metric_id = 'checkout_success_rate'
          AND bucket >= current_bucket - INTERVAL 8 WEEK
          AND bucket <= current_bucket - INTERVAL 5 MINUTE

          -- Match approximately the same time-of-week.
          AND toDayOfWeek(bucket)
                = toDayOfWeek(current_bucket - INTERVAL 5 MINUTE)

          AND abs(
                dateDiff(
                    'minute',
                    toStartOfDay(bucket),
                    bucket
                )
                -
                dateDiff(
                    'minute',
                    toStartOfDay(current_bucket - INTERVAL 5 MINUTE),
                    current_bucket - INTERVAL 5 MINUTE
                )
          ) <= 5
    ),

    evaluated AS
    (
        SELECT
            metric_id,
            dimensions,

            anyIf(value, weeks_ago = 0) AS current_value,
            anyIf(sample_count, weeks_ago = 0) AS current_samples,

            avgIf(value, weeks_ago BETWEEN 1 AND 8) AS baseline_mean,

            stddevPopIf(value, weeks_ago BETWEEN 1 AND 8)
                AS baseline_stddev,

            countIf(weeks_ago BETWEEN 1 AND 8)
                AS baseline_points

        FROM history
        GROUP BY
            metric_id,
            dimensions
    )

SELECT
    current_bucket AS ts,
    metric_id,
    dimensions,
    current_value,
    baseline_mean,
    baseline_stddev,

    abs(current_value - baseline_mean)
        / nullIf(baseline_stddev, 0) AS deviation_score,

    if(
        current_samples >= 500
        AND baseline_points >= 4
        AND baseline_stddev > 0
        AND abs(current_value - baseline_mean)
            / baseline_stddev >= 3,
        1,
        0
    ) AS should_alert

FROM evaluated;
```

ClickHouse supports window functions, standard deviations, percentiles and lag calculations that can be used to build such baselines directly in SQL. ClickStack’s SQL alerting was specifically designed to evaluate these query-computed anomaly conditions.

### For percentage metrics

For conversion rates, error rates and success rates, do not average already-calculated percentages unless every bucket has similar volume.

Store numerator and denominator:

```text
successful_checkouts = 950
total_checkouts = 1000
success_rate = 950 / 1000
```

Then aggregate using:

```sql
sum(successful_checkouts) / nullIf(sum(total_checkouts), 0)
```

This produces a properly weighted rate.

## 3. Statistical detectors worth supporting

Build a small set of reusable detector types.

### Relative seasonal change

```text
abs(current - last_week) / last_week > threshold
```

Good first detector, easy to explain.

### Seasonal z-score

```text
abs(current - historical_mean) / historical_stddev
```

Useful for reasonably stable, approximately symmetric metric distributions.

### Median and MAD

```text
median = median(previous comparable values)

MAD = median(abs(previous value - median))

score = abs(current - median) / MAD
```

This is usually more robust when historical periods contain spikes.

### Quantile range

```text
alert when current < historical p05
or current > historical p95
```

ClickHouse has several quantile implementations, including approximate variants appropriate for large datasets. Be selective when precomputing many quantile states because they can add CPU overhead to inserts.

### Rate-of-change

```text
current_5m versus previous_5m
```

Useful for sudden drops but prone to false alerts during normal traffic ramps.

### EWMA

```text
baseline_t =
    alpha * current_t
    + (1 - alpha) * baseline_t-1
```

Good for gradual metric shifts.

### Business threshold plus anomaly

Often the strongest rule combines both:

```text
success_rate < 95%
AND seasonal_z_score < -3
AND sample_count > 500
```

This avoids alerting on statistically unusual but operationally harmless changes.

# Near-real-time scalable design

For near-real-time alerting, do not let every HyperDX alert repeatedly scan raw feature tables.

Use this flow:

```text
Application / Kafka / CDC
        │
        ▼
Raw ClickHouse tables
        │
        ▼
Incremental Materialized Views
        │
        ▼
10-second / 1-minute metric rollups
        │
        ▼
HyperDX SQL alerts
        │
        ▼
Webhook
        │
        ▼
Incident/RCA orchestrator
        │
        ├── RCA agent
        ├── Slack/PagerDuty
        └── anomaly_events table
```

Incremental materialized views execute on newly inserted blocks, so they can maintain metric rollups continuously without rescanning historical raw data. ClickHouse recommends incremental views when real-time results are needed, while refreshable views are better for scheduled full recomputation.

## Recommended latency tiers

| Requirement | Suggested design |
|---|---|
| 1–5 minutes | HyperDX SQL alert directly over minute rollups |
| 15–60 seconds | Incremental MV plus dedicated evaluator polling ClickHouse |
| Below 10 seconds | Stream processor or event-driven evaluator before/alongside ClickHouse |
| Per-event trigger | Kafka consumer/Flink service, not dashboard-style SQL polling |

HyperDX is a good fit for **minute-level or scheduled near-real-time alerts**. The ClickStack API example supports a one-minute alert interval, and alerts can be provisioned programmatically. I would not make HyperDX the sole detection engine when you need strict sub-minute SLAs, because scheduled alert evaluation is still query-driven rather than a per-event streaming trigger.

## Scalable generalized schema

### Metric definition

```sql
CREATE TABLE metric_definitions
(
    metric_id String,
    metric_version UInt32,

    bucket_seconds UInt32,
    detector_type LowCardinality(String),

    warning_threshold Float64,
    critical_threshold Float64,

    lookback_weeks UInt16,
    minimum_sample_size UInt64,
    required_failures UInt16,
    recovery_successes UInt16,
    cooldown_seconds UInt32,

    enabled Bool,
    owner String,
    runbook_url String,

    updated_at DateTime
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY metric_id;
```

### Metric values

For maximum flexibility, use normal dimensions rather than putting every possible dimension into separate columns:

```sql
CREATE TABLE metric_points
(
    metric_id LowCardinality(String),
    bucket DateTime,

    dimension_hash UInt64,
    dimensions Map(String, String),

    numerator Float64,
    denominator Float64,
    value Float64,
    sample_count UInt64,

    ingested_at DateTime
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(bucket)
ORDER BY
(
    metric_id,
    dimension_hash,
    bucket
)
TTL bucket + INTERVAL 1 YEAR;
```

### Evaluations

```sql
CREATE TABLE metric_evaluations
(
    evaluation_id UUID,
    metric_id LowCardinality(String),
    bucket DateTime,

    dimension_hash UInt64,
    dimensions Map(String, String),

    actual Float64,
    expected Float64,
    lower_bound Float64,
    upper_bound Float64,
    deviation_score Float64,

    detector_type LowCardinality(String),
    is_anomaly Bool,
    severity LowCardinality(String),

    evaluated_at DateTime
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(evaluated_at)
ORDER BY
(
    metric_id,
    dimension_hash,
    evaluated_at
);
```

### Incident state

```sql
CREATE TABLE anomaly_events
(
    anomaly_id UUID,
    fingerprint UInt64,

    metric_id String,
    dimensions Map(String, String),

    started_at DateTime,
    last_seen_at DateTime,
    resolved_at Nullable(DateTime),

    status LowCardinality(String),
    severity LowCardinality(String),

    actual Float64,
    expected Float64,
    deviation_score Float64,

    trigger_count UInt32,
    rca_status LowCardinality(String),

    updated_at DateTime
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY
(
    fingerprint,
    started_at
);
```

## Avoid one materialized view per metric

This is an important scaling consideration.

A design like this will eventually become problematic:

```text
Metric 1 → MV 1
Metric 2 → MV 2
Metric 3 → MV 3
...
Metric 500 → MV 500
```

Each incremental view executes on every inserted block. ClickHouse warns that excessive materialized views can slow ingestion and create too many parts; its current guidance says that more than roughly 50 views attached to one table is typically excessive. Consolidate metrics that use similar source data and aggregation patterns.

Prefer:

```text
orders_raw
   │
   └── one orders_metrics_mv
            ├── order_count
            ├── failed_order_count
            ├── total_revenue
            └── checkout_latency states
```

Or construct a normalized stream:

```sql
SELECT
    bucket,
    arrayJoin([
        tuple('order_count', toFloat64(count()), toUInt64(count())),
        tuple('failed_count', toFloat64(countIf(status = 'failed')), toUInt64(count()))
    ]) AS metric
```

Then store the results as:

```text
metric_id
value
sample_count
dimensions
bucket
```

## Separate baseline computation from alert evaluation

For a large number of metrics, create two layers:

```text
metric_points
      │
      ▼
metric_baselines
      │
      ▼
current metric joined with baseline
      │
      ▼
metric_evaluations
```

The baseline table might contain:

```text
metric_id
dimension_hash
day_of_week
time_bucket
historical_mean
historical_stddev
historical_median
p05
p95
sample_count
updated_at
```

Refresh it hourly or daily because historical expectations do not need to be recomputed every minute.

The real-time evaluation then becomes cheap:

```sql
SELECT
    current.value,
    baseline.historical_mean,
    baseline.historical_stddev,
    abs(current.value - baseline.historical_mean)
        / baseline.historical_stddev AS score
FROM current_metric AS current
INNER JOIN metric_baselines AS baseline
    USING
    (
        metric_id,
        dimension_hash,
        day_of_week,
        time_bucket
    );
```

## HyperDX at scale

HyperDX/ClickStack can remain the user-facing rule and notification layer. ClickStack alerts automatically benefit from registered materialized views, and ClickHouse reports that this allows hundreds or thousands of alerts to run with relatively low query impact because aggregation work is shifted to ingestion time.

Also provision alerts through the ClickStack API rather than creating them manually. Current APIs support programmatic management of alerts, dashboards, sources and webhook destinations, which makes configuration-as-code possible.

## My recommended implementation

### Phase 1

```text
Raw ClickHouse tables
→ 1-minute incremental metric rollups
→ HyperDX SQL alerts every minute
→ generic webhook
→ RCA worker
```

Support:

```text
static threshold
week-over-week percentage
four-week seasonal z-score
minimum sample count
three consecutive failures
30-minute cooldown
```

### Phase 2

```text
Kafka/CDC
→ ClickHouse
→ unified metric rollups
→ centralized metric evaluator
→ anomaly_events
→ HyperDX for visualization and notifications
→ Temporal or queue-backed RCA workflow
```

The centralized evaluator should own:

```text
alert state
deduplication
consecutive-window logic
cooldowns
recovery detection
severity
detector versions
backtesting
```

HyperDX then becomes the operator interface and an additional notification mechanism—not the only place where anomaly state exists.

### Final recommendation

Use HyperDX for alert creation, visualization and webhook delivery, particularly for one-minute detection. Put your metric calculations and statistical logic in ClickHouse SQL, backed by consolidated incremental materialized views.

For a solution expected to reach thousands of metrics, high-cardinality dimensions or sub-minute detection, introduce a **general metric-evaluation service**. Let that service evaluate registry-defined rules and persist anomaly state, while HyperDX remains the dashboard and incident-entry layer.

---

## User

give me an md file for this complete chat.
