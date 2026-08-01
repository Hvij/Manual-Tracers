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
-- Nested subqueries, not a flat WITH-alias chain: ClickHouse's analyzer does
-- not reliably accept a WITH alias (`expected`) being referenced by a later
-- WITH alias in the same list on CREATE OR REPLACE — each nesting level below
-- turns the previous level's aliases into plain columns, which is unambiguous.
CREATE OR REPLACE VIEW inmobi.v_metric_deviation AS
SELECT
    *,
    (pass_sample AND pass_history AND pass_effect_rel
       AND pass_effect_abs AND pass_significance AND pass_valid_dim) AS is_anomaly,
    if(delta_abs < 0, 'drop', 'spike')                      AS direction,
    multiIf(abs(z_score) >= 12, 'critical',
            abs(z_score) >= 8,  'major',
                                'minor')                    AS severity
FROM (
    SELECT
        ts, dim_name, dim_value, metric_id, metric_level, detector,
        actual, expected, delta_abs, delta_rel, z_score,
        sample_count, base_points,
        (sample_count >= min_samples)                           AS pass_sample,
        (base_points  >= 8)                                     AS pass_history,
        (abs(delta_rel) >= min_effect_rel)                      AS pass_effect_rel,
        (min_effect_abs = 0 OR abs(delta_abs) >= min_effect_abs) AS pass_effect_abs,
        (abs(z_score)   >= 4.0)                                 AS pass_significance,
        (NOT has(invalid_dims, dim_name))                       AS pass_valid_dim
    FROM (
        SELECT
            ts, dim_name, dim_value, metric_id, metric_level, detector,
            actual, expected,
            actual - expected AS delta_abs,
            if(expected IS NULL OR expected = 0, NULL,
               (actual - expected) / expected)               AS delta_rel,
            if(detector = 'proportion' AND denominator > 0 AND base_den > 0,
               proportionsZTest(toUInt64(numerator), toUInt64(base_num),
                                toUInt64(denominator), toUInt64(base_den),
                                0.999, 'unpooled').1,
               if(expected IS NULL, NULL, (actual - expected) / robust_sigma)
            )                                                  AS z_score,
            sample_count, base_points, min_samples, min_effect_rel,
            min_effect_abs, invalid_dims
        FROM (
            SELECT
                b.ts                AS ts,
                b.dim_name          AS dim_name,
                b.dim_value         AS dim_value,
                b.metric_id         AS metric_id,
                r.level             AS metric_level,
                r.detector          AS detector,
                b.value             AS actual,
                b.numerator         AS numerator,
                b.denominator       AS denominator,
                b.base_num          AS base_num,
                b.base_den          AS base_den,
                b.sample_count      AS sample_count,
                b.base_points       AS base_points,
                r.min_samples       AS min_samples,
                r.min_effect_rel    AS min_effect_rel,
                r.min_effect_abs    AS min_effect_abs,
                r.invalid_dims      AS invalid_dims,
                if(r.detector = 'proportion',
                   if(b.base_den = 0, NULL, b.base_num / b.base_den * 1.0),
                   b.base_median)                             AS expected,
                greatest((b.base_q75 - b.base_q25) / 1.349, 1e-9) AS robust_sigma
            FROM inmobi.v_metric_baseline AS b
            INNER JOIN inmobi.metric_registry AS r ON r.metric_id = b.metric_id
        )
    )
);

-- ---------------------------------------------------------------------
-- 6.3 v_incidents / anomaly_events / mv_anomaly_feed / v_alert_feed /
-- v_rca_queue — REMOVED. That whole day-level incident-ledger path is
-- superseded: the RCA agent now triggers off ClickStack alerts on
-- v_metric_deviation directly (metric_id carried in the alert body) and
-- queries metric_registry / metric_dim_priority / v_metric_deviation live
-- per investigation — see RCA/app/investigate.py. Keeping this view here
-- would just silently recreate a second, unused incident-tracking path on
-- every replay.
-- ---------------------------------------------------------------------
