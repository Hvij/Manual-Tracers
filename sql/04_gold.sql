-- =====================================================================
-- 04 · GOLD — hourly marginal rollup. THE ALERTING SURFACE.
-- =====================================================================
-- Design decision that makes this scale:
--
--   A full dimension cross-product cube is not viable — measured at
--   767,984 distinct dimension combinations on 9M rows. At 840 hours
--   that is a ~645M-row cube to maintain for a 9M-row fact table.
--
--   Instead we store MARGINALS: one row per (hour, dim_name, dim_value).
--   62 low-cardinality dimension values + 1 global bucket = 63 series
--   per hour, ~53K rows total. Alert queries read kilobytes.
--
-- The alerting cardinality budget: only dimensions with <= ~50 distinct
-- values are alerted on. app_id (2,000), geo_device_id (5,000) and
-- advertiser_id (500) are deliberately EXCLUDED — they are drill-down
-- targets during RCA (against silver), never alert series. This is the
-- rule that keeps the design honest at 100x volume.
--
-- Only additive base quantities are stored. Every ratio in the glossary
-- is sum/sum at query time, so rollups stay correct by construction and
-- it is structurally impossible to average a ratio.

CREATE TABLE IF NOT EXISTS inmobi.metric_1h
(
    ts          DateTime,
    dim_name    LowCardinality(String),  -- 'ALL' | 'ad_format' | 'region' | ...
    dim_value   LowCardinality(String),  -- '' for the ALL bucket
    requests    UInt64,
    fills       UInt64,
    impressions UInt64,
    clicks      UInt64,
    revenue     Float64
)
ENGINE = SummingMergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (dim_name, dim_value, ts);

-- ---------------------------------------------------------------------
-- MV2: bronze -> gold, in one pass, fanned out with arrayJoin.
--
-- Attached to ad_events directly (not chained off the silver MV) so the
-- two paths are independent: if one MV is dropped or rebuilt, the other
-- is unaffected. Costs a second set of dictGet probes; at this volume
-- that is free, and it removes a failure mode on sealed-data night.
--
-- vertical / campaign_type are only emitted for FILLED requests, because
-- advertiser_id is empty when nothing was served. Consequence: fill_rate
-- is meaningless for those two dimensions (it is 1.0 by construction) —
-- the registry in 05 marks that explicitly.
-- ---------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS inmobi.mv_metric_1h
TO inmobi.metric_1h AS
SELECT
    toStartOfHour(event_time) AS ts,
    dim.1                     AS dim_name,
    dim.2                     AS dim_value,
    count()                   AS requests,
    sum(is_filled)            AS fills,
    sum(is_impression)        AS impressions,
    sum(is_click)             AS clicks,
    sum(revenue)              AS revenue
FROM inmobi.ad_events
ARRAY JOIN arrayConcat(
    [
        ('ALL',            ''),
        ('ad_format',      toString(ad_format)),
        ('category',       dictGetOrDefault('inmobi.dict_apps', 'category',
                               tuple(toString(app_id)), 'unknown')),
        ('publisher_tier', dictGetOrDefault('inmobi.dict_apps', 'publisher_tier',
                               tuple(toString(app_id)), 'unknown')),
        ('region',         dictGetOrDefault('inmobi.dict_geo_device', 'region',
                               tuple(toString(geo_device_id)), 'unknown')),
        ('country',        dictGetOrDefault('inmobi.dict_geo_device', 'country',
                               tuple(toString(geo_device_id)), 'unknown')),
        ('device_model',   dictGetOrDefault('inmobi.dict_geo_device', 'device_model',
                               tuple(toString(geo_device_id)), 'unknown')),
        ('os_version',     dictGetOrDefault('inmobi.dict_geo_device', 'os_version',
                               tuple(toString(geo_device_id)), 'unknown'))
    ],
    if(advertiser_id != '',
       [
        ('vertical',       dictGetOrDefault('inmobi.dict_advertisers', 'vertical',
                               tuple(toString(advertiser_id)), 'unknown')),
        ('campaign_type',  dictGetOrDefault('inmobi.dict_advertisers', 'campaign_type',
                               tuple(toString(advertiser_id)), 'unknown'))
       ],
       CAST([], 'Array(Tuple(String, String))'))
) AS dim
GROUP BY ts, dim_name, dim_value;
