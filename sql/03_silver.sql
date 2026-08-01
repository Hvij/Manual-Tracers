-- =====================================================================
-- 03 · SILVER — denormalised, cleaned event stream
-- =====================================================================
-- Purpose: THE surface. Every metric, every baseline, every drill-down and
-- every alert is computed from this table — there is no rollup and no metric
-- view, because metric_def.sql runs directly against these columns.
--   * Every dimension resolved at ingest, so nothing downstream joins.
--   * dictGetOrDefault -> 'unknown' so a dimension key that appears in
--     the sealed dataset but not in our dim tables degrades gracefully
--     instead of silently dropping the row.
--
-- ORDER BY: time first — every query is time-bounded, and both the hourly
-- bucketing and the baseline lookback scan by event_time — then the dimensions
-- most often filtered on during a drill-down.

CREATE TABLE IF NOT EXISTS inmobi.ad_events_enriched
(
    event_time      DateTime64(3),
    app_id          LowCardinality(String),
    geo_device_id   LowCardinality(String),
    advertiser_id   LowCardinality(String),
    ad_format       LowCardinality(String),
    is_filled       UInt8,
    is_impression   UInt8,
    is_click        UInt8,
    revenue         Float64,
    category        LowCardinality(String),
    publisher_tier  LowCardinality(String),
    region          LowCardinality(String),
    country         LowCardinality(String),
    device_model    LowCardinality(String),
    os_version      LowCardinality(String),
    vertical        LowCardinality(String),
    campaign_type   LowCardinality(String),
    -- ALIAS => computed on read, costs zero storage. Keeps the HyperDX
    -- Search UI usable without paying ~100MB for a redundant string.
    message         String ALIAS concat(
                        ad_format, ' ', country, ' ', os_version,
                        ' filled=', toString(is_filled),
                        ' imp=', toString(is_impression),
                        ' clk=', toString(is_click),
                        ' rev=', toString(revenue))
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(event_time)
ORDER BY (event_time, region, country, os_version, category, ad_format)
SETTINGS index_granularity = 8192;

-- ---------------------------------------------------------------------
-- MV1: bronze -> silver. Fires on every insert into ad_events, so a
-- replay of the sealed file populates this with no extra step.
-- ---------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS inmobi.mv_ad_events_enriched
TO inmobi.ad_events_enriched AS
SELECT
    event_time,
    app_id,
    geo_device_id,
    advertiser_id,
    ad_format,
    is_filled,
    is_impression,
    is_click,
    revenue,
    dictGetOrDefault('inmobi.dict_apps', 'category',
                     tuple(toString(app_id)), 'unknown')        AS category,
    dictGetOrDefault('inmobi.dict_apps', 'publisher_tier',
                     tuple(toString(app_id)), 'unknown')        AS publisher_tier,
    dictGetOrDefault('inmobi.dict_geo_device', 'region',
                     tuple(toString(geo_device_id)), 'unknown') AS region,
    dictGetOrDefault('inmobi.dict_geo_device', 'country',
                     tuple(toString(geo_device_id)), 'unknown') AS country,
    dictGetOrDefault('inmobi.dict_geo_device', 'device_model',
                     tuple(toString(geo_device_id)), 'unknown') AS device_model,
    dictGetOrDefault('inmobi.dict_geo_device', 'os_version',
                     tuple(toString(geo_device_id)), 'unknown') AS os_version,
    -- '' (not 'unknown') when unfilled: absence of an advertiser is
    -- meaningful here, and must not be confused with a lookup miss.
    if(advertiser_id = '', '',
       dictGetOrDefault('inmobi.dict_advertisers', 'vertical',
                        tuple(toString(advertiser_id)), 'unknown'))      AS vertical,
    if(advertiser_id = '', '',
       dictGetOrDefault('inmobi.dict_advertisers', 'campaign_type',
                        tuple(toString(advertiser_id)), 'unknown'))      AS campaign_type
FROM inmobi.ad_events;
