# HyperDX + InMobi on local ClickHouse (4 CPU / 8 GB)

## 1. Start stack

```bash
docker compose up -d
```

Services:

| Service | URL / port | Memory budget |
|---------|------------|---------------|
| ClickHouse | http://localhost:8123 | 5 GB / 3 CPU |
| HyperDX UI | http://localhost:8080 | 1.5 GB |
| HyperDX API | http://localhost:8000 | (same container) |
| MongoDB | internal | 512 MB |

Creds (from `.env`, also hashed in `config/users.xml`):

- user: `default`
- password: `asdfzxcv`

## 2. Load InMobi data

```bash
chmod +x scripts/load_inmobi.sh
./scripts/load_inmobi.sh
```

Expect **9,000,000** rows in `inmobi.ad_events` and `inmobi.ad_events_enriched`.

Quick check:

```bash
docker exec -i clickhouse clickhouse-client --user default --password asdfzxcv \
  --query "SELECT count() FROM inmobi.ad_events"
```

## 3. Connect HyperDX → ClickHouse

1. Open http://localhost:8080 and create an account (stored in Mongo).
2. Connection should already exist via `DEFAULT_CONNECTIONS`:
   - **Name:** Local ClickHouse
   - **Host:** `http://clickhouse:8123` (Docker DNS — correct for the HyperDX container)
   - **Username:** `default`
   - **Password:** `asdfzxcv`
3. If missing: **Team Settings → Connections → Add**, use the same values.  
   Do **not** use `localhost` here — HyperDX API runs inside Docker and must reach the `clickhouse` service name.

## 4. Create an InMobi source

**Team Settings → Sources → New source** (Logs / Events style):

| Field | Value |
|-------|--------|
| Name | InMobi Ad Events |
| Connection | Local ClickHouse |
| Database | `inmobi` |
| Table | `ad_events_enriched` |
| Timestamp Column | `event_time` |
| Default Select | `event_time, ad_format, region, country, os_version, category, is_filled, is_impression, is_click, revenue, message` |
| Body / Message | `message` |
| Severity (optional) | `multiIf(is_click = 1, 'info', is_filled = 0, 'warn', 'debug')` |
| Service (optional) | `ad_format` |
| Resource Attributes (optional) | `map('region', region, 'country', country, 'os_version', os_version, 'category', category)` |

Data window is **2026-06-01 → 2026-07-05** — set the HyperDX time picker to that range or charts look empty.

## 5. Dashboard + alerts (API)

Already provisioned for user `1@1.com` via `/api/v2`:

```bash
# Re-run anytime (logs in, updates dashboard + alerts)
python3 scripts/provision_hyperdx_inmobi.py
```

| Resource | Location |
|----------|----------|
| Dashboard | http://localhost:8080/dashboards/6a6db3bc79b527244478f148 |
| Alerts | Team Settings → Alerts (6 rules on hourly tiles) |
| Webhook sink | `https://httpbin.org/post` (generic; swap to Slack Incoming URL in UI) |

**Charts on the dashboard:** requests, revenue, fill rate, unfilled, daily revenue, daily requests/fills, fill rate by region/OS, eCPM by category, CTR by OS, revenue by format, funnel, plus 5 alertable hourly tiles.

**Alert thresholds** (from dataset baselines ≈ 257k req/day, fill 0.78, eCPM 2.47, CTR 0.011):

| Alert | Rule |
|-------|------|
| Requests drop | hourly count **below 5,000** |
| Requests spike | hourly count **above 20,000** |
| Fill rate drop | hourly fill **below 0.70** |
| Revenue drop | hourly revenue **below 8** |
| CTR spike | hourly clicks/impr **above 0.025** |
| eCPM drop | hourly rev/impr **below 0.0015** |

**Important:** HyperDX alerts evaluate **live “now”** windows. InMobi fact data ends **2026-07-05**, so “below” alerts may flip to `ALERT` when the current hour has little/no data (empty window looks like a drop). Charts still render if you set the UI time picker to **2026-06-01 → 2026-07-06**. Point the webhook at Slack Incoming URL in Team Settings → Webhooks when you want real notifications.

Personal API key: **Team Settings → API Keys**. External API base: `http://localhost:8000/api/v2` with `Authorization: Bearer <key>`.

## 6. Charts (Chart Explorer / manual SQL)

Use **Raw SQL** charts against `inmobi.ad_events_enriched`. Ratio metrics = **sum/sum**, never avg of ratios.

### Daily revenue

```sql
SELECT
  toStartOfDay(event_time) AS ts,
  sum(revenue) AS revenue
FROM inmobi.ad_events_enriched
WHERE event_time >= toDateTime64('2026-06-01', 3)
  AND event_time <  toDateTime64('2026-07-06', 3)
GROUP BY ts
ORDER BY ts
```

### Fill rate by region (hourly)

```sql
SELECT
  toStartOfHour(event_time) AS ts,
  region,
  sum(is_filled) / count() AS fill_rate
FROM inmobi.ad_events_enriched
WHERE event_time >= toDateTime64('2026-06-01', 3)
  AND event_time <  toDateTime64('2026-07-06', 3)
GROUP BY ts, region
ORDER BY ts
```

### eCPM by category

```sql
SELECT
  toStartOfDay(event_time) AS ts,
  category,
  sum(revenue) / nullIf(sum(is_impression), 0) * 1000 AS ecpm
FROM inmobi.ad_events_enriched
WHERE event_time >= toDateTime64('2026-06-01', 3)
  AND event_time <  toDateTime64('2026-07-06', 3)
GROUP BY ts, category
ORDER BY ts
```

### CTR by OS

```sql
SELECT
  toStartOfDay(event_time) AS ts,
  os_version,
  sum(is_click) / nullIf(sum(is_impression), 0) AS ctr
FROM inmobi.ad_events_enriched
WHERE event_time >= toDateTime64('2026-06-01', 3)
  AND event_time <  toDateTime64('2026-07-06', 3)
GROUP BY ts, os_version
ORDER BY ts
```

### Funnel snapshot

```sql
SELECT
  count() AS requests,
  sum(is_filled) AS fills,
  sum(is_impression) AS impressions,
  sum(is_click) AS clicks,
  sum(revenue) AS revenue,
  sum(is_filled) / count() AS fill_rate,
  sum(is_click) / nullIf(sum(is_impression), 0) AS ctr,
  sum(revenue) / nullIf(sum(is_impression), 0) * 1000 AS ecpm
FROM inmobi.ad_events_enriched
```

## 6. Metric cheat sheet

| Metric | SQL |
|--------|-----|
| Requests | `count(*)` |
| Fill rate | `sum(is_filled) / count(*)` |
| CTR | `sum(is_click) / sum(is_impression)` |
| eCPM | `sum(revenue) / sum(is_impression) * 1000` |
| RPR | `sum(revenue) / count(*)` |

Slice dims: `ad_format`, `category`, `publisher_tier`, `vertical`, `campaign_type`, `region`, `country`, `device_model`, `os_version`.

## 7. Resource notes (8 GB host)

Compose caps: ClickHouse **5g/3cpu**, HyperDX **1.5g**, Mongo **512m**. Leave ~1 GB for macOS/Docker VM. If OOM:

```bash
docker compose down
# lower clickhouse mem_limit to 4g in docker-compose.yml, then:
docker compose up -d
```
