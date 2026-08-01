-- =====================================================================
-- 02 · DICTIONARIES — dimension lookup for the enrichment MV
-- =====================================================================
-- Why dictionaries and not JOIN:
--   A materialized view fires per inserted block. A JOIN inside an MV
--   re-reads the right-hand table for every block and is not guaranteed
--   to see a consistent snapshot. dictGet is an in-memory hash probe,
--   deterministic and O(1), which is what we want on the ingest path.
-- Keys are String, so COMPLEX_KEY_HASHED (HASHED requires UInt64 keys).
-- LIFETIME(0) = never auto-reload; we reload explicitly after a dim load.

CREATE DICTIONARY IF NOT EXISTS inmobi.dict_apps
(
    app_id         String,
    category       String,
    publisher_tier String
)
PRIMARY KEY app_id
SOURCE(CLICKHOUSE(TABLE 'apps' DB 'inmobi'))
LAYOUT(COMPLEX_KEY_HASHED())
LIFETIME(0);

CREATE DICTIONARY IF NOT EXISTS inmobi.dict_advertisers
(
    advertiser_id String,
    vertical      String,
    campaign_type String
)
PRIMARY KEY advertiser_id
SOURCE(CLICKHOUSE(TABLE 'advertisers' DB 'inmobi'))
LAYOUT(COMPLEX_KEY_HASHED())
LIFETIME(0);

CREATE DICTIONARY IF NOT EXISTS inmobi.dict_geo_device
(
    geo_device_id String,
    region        String,
    country       String,
    device_model  String,
    os_version    String
)
PRIMARY KEY geo_device_id
SOURCE(CLICKHOUSE(TABLE 'geo_device' DB 'inmobi'))
LAYOUT(COMPLEX_KEY_HASHED())
LIFETIME(0);
