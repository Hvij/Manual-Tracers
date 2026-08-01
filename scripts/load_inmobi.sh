#!/usr/bin/env bash
# Load InMobi star-schema data into local ClickHouse, then build enriched table.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
[[ -f "$ROOT/.env" ]] && source "$ROOT/.env"

CH_USER="${CLICKHOUSE_USER:-default}"
CH_PASS="${CLICKHOUSE_PASSWORD:-asdfzxcv}"
CONTAINER="${CLICKHOUSE_CONTAINER:-clickhouse}"

ch() {
  docker exec -i "$CONTAINER" clickhouse-client \
    --user "$CH_USER" \
    --password "$CH_PASS" \
    "$@"
}

echo "==> Waiting for ClickHouse..."
for i in $(seq 1 30); do
  if ch --query "SELECT 1" >/dev/null 2>&1; then
    break
  fi
  sleep 2
  if [[ "$i" -eq 30 ]]; then
    echo "ClickHouse not ready. Run: docker compose up -d" >&2
    exit 1
  fi
done

echo "==> Creating schema..."
ch --multiquery < "$ROOT/sql/inmobi_schema.sql"

EXISTING="$(ch --query "SELECT count() FROM inmobi.ad_events")"
if [[ "$EXISTING" != "0" ]]; then
  echo "==> inmobi.ad_events already has $EXISTING rows — skip base load."
else
  echo "==> Loading ad_events.parquet (9M rows, ~1–3 min on 4c/8g)..."
  ch --query "
INSERT INTO inmobi.ad_events
SELECT *
FROM file('inmobi/ad_events.parquet', Parquet)
"

  echo "==> Loading dimension CSVs..."
  ch --query "
INSERT INTO inmobi.apps
SELECT * FROM file('inmobi/apps.csv', CSVWithNames)
"
  ch --query "
INSERT INTO inmobi.advertisers
SELECT * FROM file('inmobi/advertisers.csv', CSVWithNames)
"
  ch --query "
INSERT INTO inmobi.geo_device
SELECT * FROM file('inmobi/geo_device.csv', CSVWithNames)
"
fi

ENRICHED="$(ch --query "SELECT count() FROM inmobi.ad_events_enriched")"
if [[ "$ENRICHED" != "0" ]]; then
  echo "==> inmobi.ad_events_enriched already has $ENRICHED rows — skip enrich."
else
  echo "==> Building inmobi.ad_events_enriched..."
  ch --query "
INSERT INTO inmobi.ad_events_enriched
SELECT
    e.event_time,
    e.app_id,
    e.geo_device_id,
    e.advertiser_id,
    e.ad_format,
    e.is_filled,
    e.is_impression,
    e.is_click,
    e.revenue,
    a.category,
    a.publisher_tier,
    d.region,
    d.country,
    d.device_model,
    d.os_version,
    adv.vertical,
    adv.campaign_type,
    concat(
      'ad_format=', e.ad_format,
      ' region=', ifNull(d.region, ''),
      ' country=', ifNull(d.country, ''),
      ' os=', ifNull(d.os_version, ''),
      ' category=', ifNull(a.category, ''),
      ' filled=', toString(e.is_filled),
      ' revenue=', toString(e.revenue)
    ) AS message
FROM inmobi.ad_events AS e
LEFT JOIN inmobi.apps AS a ON e.app_id = a.app_id
LEFT JOIN inmobi.geo_device AS d ON e.geo_device_id = d.geo_device_id
LEFT JOIN inmobi.advertisers AS adv ON e.advertiser_id = adv.advertiser_id
SETTINGS max_memory_usage = 3000000000,
         max_bytes_before_external_group_by = 1000000000,
         join_algorithm = 'full_sorting_merge'
"
fi

echo "==> Row counts:"
ch --query "
SELECT 'ad_events' AS table, count() AS rows FROM inmobi.ad_events
UNION ALL
SELECT 'apps', count() FROM inmobi.apps
UNION ALL
SELECT 'advertisers', count() FROM inmobi.advertisers
UNION ALL
SELECT 'geo_device', count() FROM inmobi.geo_device
UNION ALL
SELECT 'ad_events_enriched', count() FROM inmobi.ad_events_enriched
FORMAT PrettyCompact
"

echo
echo "Done. HyperDX: http://localhost:8080  |  ClickHouse play: http://localhost:8123/play"
echo "See docs/HYPERDX_INMOBI.md for source + chart setup."
