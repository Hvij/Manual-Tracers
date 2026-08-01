-- =====================================================================
-- 05 · METRIC LAYER — the single source of truth for definitions
-- =====================================================================
-- Formulas are taken verbatim from InMobi/metrics_glossary.md.
-- Nothing downstream (baseline, detector, dashboard, alert, RCA agent)
-- is allowed to restate a formula: they all read v_metric_points.
-- HyperDX tiles and alerts are GENERATED from this registry, not
-- maintained separately — ClickHouse is the definition store, ClickStack
-- is a render target.

-- ---------------------------------------------------------------------
-- 5.1 Registry: what each metric is, and how to test it
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inmobi.metric_registry
(
    metric_id       String,
    level           UInt8,   -- 1 = business outcome, 2 = identity factor, 3 = context
    is_ratio        UInt8,
    numerator       String,  -- expression over metric_1h columns
    denominator     String,  -- '' for additive metrics
    scale           Float64, -- multiplier applied after the ratio (eCPM = *1000)
    direction       String,  -- down_is_bad | up_is_bad | both
    detector        String,  -- proportion | robust_z
    min_samples     UInt64,  -- minimum requests in the window to evaluate at all
    min_effect_rel  Float64, -- minimum relative move to be worth alerting
    min_effect_abs  Float64, -- minimum absolute move (pp for rates)
    invalid_dims    Array(String),
    description     String,
    updated_at      DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY metric_id;

INSERT INTO inmobi.metric_registry
(metric_id, level, is_ratio, numerator, denominator, scale, direction, detector,
 min_samples, min_effect_rel, min_effect_abs, invalid_dims, description) VALUES
-- min_samples is a guard against degenerate tiny slices ONLY. It must NOT
-- be used to buy statistical confidence — proportionsZTest already does
-- that correctly at small n. Calibrated against this stream's actual rate
-- of ~10.7K requests/hour globally: a 10%-of-traffic segment such as
-- os_version='Android 15' runs ~1,025 requests/hour, so a 5,000 threshold
-- would silently hide the single largest planted incident in the dataset.
-- Rule of thumb: min_samples ~= 5% of the global hourly request rate.
--
-- L1 — the outcome we actually care about
('revenue',     1, 0, 'revenue',     '',            1.0,    'both', 'robust_z',   2000, 0.03, 0.0,
 [], 'sum(revenue). The business outcome — decomposed via the revenue identity.'),
-- L2 — the identity factors: Revenue = Requests x FillRate x RenderRate x eCPM/1000
('requests',    2, 0, 'requests',    '',            1.0,    'both', 'robust_z',   2000, 0.05, 0.0,
 ['vertical','campaign_type'], 'count(*). Volume factor.'),
('fill_rate',   2, 1, 'fills',       'requests',    1.0,    'both', 'proportion',  500, 0.02, 0.01,
 ['vertical','campaign_type'], 'sum(is_filled)/count(*). Supply factor.'),
('render_rate', 2, 1, 'impressions', 'fills',       1.0,    'both', 'proportion',  500, 0.02, 0.01,
 ['vertical','campaign_type'], 'sum(is_impression)/sum(is_filled). Render factor.'),
('ecpm',        2, 1, 'revenue',     'impressions', 1000.0, 'both', 'robust_z',    500, 0.03, 0.0,
 [], 'sum(revenue)/sum(is_impression)*1000. Price factor.'),
-- L3 — context, not a direct revenue factor in this CPM model
('ctr',         3, 1, 'clicks',      'impressions', 1.0,    'both', 'proportion',  500, 0.05, 0.002,
 [], 'sum(is_click)/sum(is_impression). Engagement signal.'),
('rpr',         3, 1, 'revenue',     'requests',    1.0,    'both', 'robust_z',   2000, 0.03, 0.0,
 ['vertical','campaign_type'], 'sum(revenue)/count(*). All-in efficiency.');

-- ---------------------------------------------------------------------
-- 5.2 Dimension levelling: for a given metric, which cut to try first.
-- Encodes domain reasoning — a fill failure is supply-side, so OS and
-- country lead; a price move is demand-side, so tier and vertical lead.
-- The RCA agent walks these in priority order.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inmobi.metric_dim_priority
(
    metric_id  String,
    dim_name   String,
    priority   UInt8,
    rationale  String,
    updated_at DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (metric_id, dim_name);

INSERT INTO inmobi.metric_dim_priority (metric_id, dim_name, priority, rationale) VALUES
('fill_rate','os_version',1,'SDK/adapter faults track OS builds first'),
('fill_rate','country',2,'demand availability is bid per market'),
('fill_rate','ad_format',3,'format-specific inventory can go unsold'),
('fill_rate','publisher_tier',4,'tier drives which demand is eligible'),
('fill_rate','category',5,'category affects baseline fill materially'),
('fill_rate','device_model',6,'narrower proxy for the OS signal'),
('fill_rate','region',7,'usually dilutes a country-level fault'),
('ecpm','publisher_tier',1,'price is tiered by publisher quality'),
('ecpm','vertical',2,'advertiser vertical sets willingness to pay'),
('ecpm','campaign_type',3,'CPM vs CPC vs CPI price differently'),
('ecpm','country',4,'market-level price floors'),
('ecpm','ad_format',5,'format carries a strong price prior'),
('ecpm','category',6,'secondary to tier'),
('ecpm','region',7,'aggregate of country'),
('ecpm','os_version',8,'rarely a pricing driver'),
('requests','region',1,'traffic incidents are usually infra/geo shaped'),
('requests','country',2,'narrows a regional traffic move'),
('requests','category',3,'app-mix driven volume shifts'),
('requests','ad_format',4,'format demand changes'),
('requests','publisher_tier',5,'a large publisher leaving moves volume'),
('requests','os_version',6,'client rollout can change request rate'),
('requests','device_model',7,'narrow proxy for OS'),
('render_rate','os_version',1,'render failures are client-side'),
('render_rate','device_model',2,'hardware/webview specific'),
('render_rate','ad_format',3,'video and interstitial fail differently'),
('render_rate','country',4,'network conditions'),
('ctr','ad_format',1,'creative format dominates CTR'),
('ctr','os_version',2,'client rendering affects tappability'),
('ctr','device_model',3,'screen size effects'),
('ctr','category',4,'audience intent varies by app type'),
('ctr','country',5,'market-level engagement differences'),
('revenue','ALL',1,'decompose the identity before slicing any dimension');

-- ---------------------------------------------------------------------
-- 5.3 THE metric layer. Long/EAV shape: one row per
--     (hour, dim_name, dim_value, metric_id).
--
-- Ratios are computed sum/sum here and ONLY here. numerator and
-- denominator are carried alongside the value because the proportion
-- test needs the raw counts, not the rate.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW inmobi.v_metric_points AS
SELECT
    ts,
    dim_name,
    dim_value,
    m.1                                     AS metric_id,
    m.2                                     AS numerator,
    m.3                                     AS denominator,
    if(m.3 = 0, NULL, m.2 / m.3 * m.4)      AS value,
    requests                                AS sample_count
FROM inmobi.metric_1h
ARRAY JOIN [
    ('revenue',     revenue,                 1.0,                     1.0),
    ('requests',    toFloat64(requests),     1.0,                     1.0),
    ('fill_rate',   toFloat64(fills),        toFloat64(requests),     1.0),
    ('render_rate', toFloat64(impressions),  toFloat64(fills),        1.0),
    ('ecpm',        revenue,                 toFloat64(impressions),  1000.0),
    ('ctr',         toFloat64(clicks),       toFloat64(impressions),  1.0),
    ('rpr',         revenue,                 toFloat64(requests),     1.0)
] AS m;
