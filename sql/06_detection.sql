-- =====================================================================
-- 06 · BASELINE + DEVIATION DETECTION
-- =====================================================================
-- Baseline design, and why:
--
--   Seasonality is real (hour-of-day + weekend) and the history is thin
--   (5 weeks). So the baseline partitions on hour-of-day AND day-type
--   (weekday vs weekend) and looks back over up to 20 prior matching
--   observations. A flat average would flag every weekend; this does not.
--
--   Spread is measured with a robust IQR (not stddev) because a handful
--   of planted incidents inside the lookback would inflate stddev and
--   mask the very anomalies we are hunting.
--
--   For RATIO metrics we do not lean on historical variance at all.
--   proportionsZTest compares this window's numerator/denominator against
--   the pooled baseline numerator/denominator directly. At ~250K requests
--   an hour the binomial standard error on fill rate is tiny, so power
--   comes from SAMPLE SIZE rather than from history. That is what makes
--   thin history survivable.
--
-- NOTE ON TIME: this data is replayed history, so "current" means the
-- latest hour present in the data, never now(). Every view below is
-- data-time driven.

-- ---------------------------------------------------------------------
-- 6.1 Baseline: trailing same-hour, same-day-type statistics
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW inmobi.v_metric_baseline AS
SELECT
    ts,
    dim_name,
    dim_value,
    metric_id,
    numerator,
    denominator,
    value,
    sample_count,
    quantileExact(0.5)(value)  OVER w AS base_median,
    quantileExact(0.25)(value) OVER w AS base_q25,
    quantileExact(0.75)(value) OVER w AS base_q75,
    sum(numerator)             OVER w AS base_num,
    sum(denominator)           OVER w AS base_den,
    count()                    OVER w AS base_points
FROM inmobi.v_metric_points
WINDOW w AS (
    PARTITION BY metric_id, dim_name, dim_value, toHour(ts), toDayOfWeek(ts) >= 6
    ORDER BY ts
    ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING
);

-- ---------------------------------------------------------------------
-- 6.2 Deviation: score every point against its baseline + registry rules
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW inmobi.v_metric_deviation AS
WITH
    -- expected value under the baseline
    if(r.detector = 'proportion',
       if(b.base_den = 0, NULL, b.base_num / b.base_den * 1.0),
       b.base_median)                                   AS expected,
    -- robust spread -> normal-equivalent sigma
    greatest((b.base_q75 - b.base_q25) / 1.349, 1e-9)   AS robust_sigma,
    b.value - expected                                  AS delta_abs,
    if(expected IS NULL OR expected = 0, NULL,
       (b.value - expected) / expected)                 AS delta_rel,
    -- test statistic, per detector
    if(r.detector = 'proportion'
         AND b.denominator > 0 AND b.base_den > 0,
       proportionsZTest(toUInt64(b.numerator), toUInt64(b.base_num),
                        toUInt64(b.denominator), toUInt64(b.base_den),
                        0.999, 'unpooled').1,
       if(expected IS NULL, NULL, (b.value - expected) / robust_sigma)
    )                                                   AS z_score
SELECT
    b.ts                AS ts,
    b.dim_name          AS dim_name,
    b.dim_value         AS dim_value,
    b.metric_id         AS metric_id,
    r.level             AS metric_level,
    r.detector          AS detector,
    b.value             AS actual,
    expected            AS expected,
    delta_abs           AS delta_abs,
    delta_rel           AS delta_rel,
    z_score             AS z_score,
    b.sample_count      AS sample_count,
    b.base_points       AS base_points,
    -- ---- guard rails: all must pass before anything is called an anomaly
    (b.sample_count >= r.min_samples)                       AS pass_sample,
    (b.base_points  >= 8)                                   AS pass_history,
    (abs(delta_rel) >= r.min_effect_rel)                    AS pass_effect_rel,
    (r.min_effect_abs = 0 OR abs(delta_abs) >= r.min_effect_abs) AS pass_effect_abs,
    (abs(z_score)   >= 4.0)                                 AS pass_significance,
    (NOT has(r.invalid_dims, b.dim_name))                   AS pass_valid_dim,
    (pass_sample AND pass_history AND pass_effect_rel
       AND pass_effect_abs AND pass_significance AND pass_valid_dim) AS is_anomaly,
    if(delta_abs < 0, 'drop', 'spike')                      AS direction,
    multiIf(abs(z_score) >= 12, 'critical',
            abs(z_score) >= 8,  'major',
                                'minor')                    AS severity
FROM inmobi.v_metric_baseline AS b
INNER JOIN inmobi.metric_registry AS r ON r.metric_id = b.metric_id;

-- ---------------------------------------------------------------------
-- 6.3 Incident roll-up: an incident is a metric+segment that stays
-- anomalous for several hours on a day. Single-hour blips do not page.
-- This is the table the alert fires on and the RCA agent consumes.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW inmobi.v_incidents AS
SELECT
    toDate(ts)                        AS day,
    metric_id,
    metric_level,
    dim_name,
    dim_value,
    direction,
    countIf(is_anomaly)               AS anomalous_hours,
    count()                           AS observed_hours,
    round(avgIf(actual,   is_anomaly), 6) AS avg_actual,
    round(avgIf(expected, is_anomaly), 6) AS avg_expected,
    round(avgIf(delta_rel, is_anomaly), 6) AS avg_delta_rel,
    round(maxIf(abs(z_score), is_anomaly), 2) AS peak_abs_z,
    sum(sample_count)                 AS day_requests,
    max(severity)                     AS severity
FROM inmobi.v_metric_deviation
GROUP BY day, metric_id, metric_level, dim_name, dim_value, direction
HAVING anomalous_hours >= 3          -- persistence requirement
ORDER BY peak_abs_z DESC;

-- ---------------------------------------------------------------------
-- 6.4 Persisted incident state. The RCA agent writes back here, so a
-- judge can trace alert -> investigation -> diagnosis in one table.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inmobi.anomaly_events
(
    anomaly_id      UUID DEFAULT generateUUIDv4(),
    fingerprint     String,   -- day|metric|dim_name|dim_value|direction
    day             Date,
    metric_id       String,
    dim_name        String,
    dim_value       String,
    direction       String,
    severity        String,
    anomalous_hours UInt16,
    actual          Float64,
    expected        Float64,
    delta_rel       Float64,
    peak_abs_z      Float64,
    rca_status      String DEFAULT 'pending',   -- pending|running|done|failed
    rca_summary     String DEFAULT '',
    trace_url       String DEFAULT '',
    detected_at     DateTime DEFAULT now(),
    updated_at      DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY fingerprint;
